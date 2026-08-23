"""Shared venue-adapter contract, error taxonomy, rate limiting, and backoff."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from event_arb.domain.enums import Venue
from event_arb.domain.models import OrderBook, VenueMarket

__all__ = [
    "AdapterHealth",
    "AuthenticationError",
    "BackoffPolicy",
    "MarketDataError",
    "RateLimitError",
    "RateLimiter",
    "StreamEvent",
    "TransientError",
    "VenueAdapter",
    "VenueError",
]


# ------------------------------------------------------------------- error taxonomy


class VenueError(Exception):
    """Base class for all normalized venue errors."""

    def __init__(self, message: str, venue: Venue | None = None) -> None:
        super().__init__(message)
        self.venue = venue


class TransientError(VenueError):
    """Retryable: timeout, 5xx, connection reset."""


class RateLimitError(TransientError):
    """429 or a documented rate-limit rejection.

    ``retry_after`` carries the server's own guidance; it is always honoured rather than
    replaced by our own backoff, so we never argue with a venue about its limits.
    """

    def __init__(
        self, message: str, venue: Venue | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message, venue)
        self.retry_after = retry_after


class AuthenticationError(VenueError):
    """401/403, bad signature, or missing credentials. Not retryable."""


class MarketDataError(VenueError):
    """Malformed or unparseable payload. Not retryable — the schema likely changed."""


# ---------------------------------------------------------------------- rate limits


class RateLimiter:
    """Simple async token bucket.

    Deliberately conservative. Venue rate limits are a term of use, not a performance
    knob: the scanner slows itself down rather than probing for the real ceiling.
    """

    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = float(burst if burst is not None else max(1, int(rate_per_second)))
        self._tokens = self._capacity
        self._updated = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._updated == 0.0:
                self._updated = now
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._updated = loop.time()
            else:
                self._tokens -= 1.0


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential backoff with full jitter.

    Full jitter (``uniform(0, capped)``) rather than a fixed multiplier prevents every
    reconnecting client from retrying in lockstep after a venue outage.
    """

    base_seconds: float = 0.5
    max_seconds: float = 60.0
    factor: float = 2.0

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        capped = min(self.max_seconds, self.base_seconds * (self.factor ** (attempt - 1)))
        generator = rng or random
        return generator.uniform(0.0, capped)


# --------------------------------------------------------------------------- health


@dataclass
class AdapterHealth:
    """Rolling health and freshness state for one adapter."""

    venue: Venue
    connected: bool = False
    last_message_at: datetime | None = None
    last_error: str | None = None
    reconnect_count: int = 0
    resync_count: int = 0
    sequence_gaps: int = 0
    messages_received: int = 0
    subscribed_markets: set[str] = field(default_factory=set)

    def record_message(self, at: datetime | None = None) -> None:
        self.last_message_at = at or datetime.now(UTC)
        self.messages_received += 1

    def record_error(self, error: str) -> None:
        self.last_error = error

    def is_fresh(self, max_age: timedelta, now: datetime | None = None) -> bool:
        if self.last_message_at is None:
            return False
        return (now or datetime.now(UTC)) - self.last_message_at <= max_age

    def as_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue.value,
            "connected": self.connected,
            "last_message_at": (self.last_message_at.isoformat() if self.last_message_at else None),
            "last_error": self.last_error,
            "reconnect_count": self.reconnect_count,
            "resync_count": self.resync_count,
            "sequence_gaps": self.sequence_gaps,
            "messages_received": self.messages_received,
            "subscribed_markets": sorted(self.subscribed_markets),
        }


@dataclass(frozen=True)
class StreamEvent:
    """A normalized book update emitted by an adapter's stream."""

    venue: Venue
    market_id: str
    book: OrderBook
    is_snapshot: bool


# -------------------------------------------------------------------------- adapter


@runtime_checkable
class SupportsClose(Protocol):
    async def close(self) -> None: ...


class VenueAdapter(ABC):
    """Read-only market-data interface implemented by every venue and fixture adapter.

    There is intentionally no ``submit_order`` on this interface. Order submission is a
    separate, disabled subsystem (``event_arb.execution``), so no amount of misuse of a
    market-data adapter can place a trade.
    """

    venue: Venue

    def __init__(self, venue: Venue) -> None:
        self.venue = venue
        self.health = AdapterHealth(venue=venue)

    @abstractmethod
    async def discover_markets(
        self, limit: int | None = None, market_ids: list[str] | None = None
    ) -> list[VenueMarket]:
        """List tradable markets with normalized metadata.

        ``market_ids`` restricts retrieval to specific markets. A trial names the handful
        of markets it watches, so fetching only those keeps startup bounded and keeps API
        access narrowly tied to the markets under trial.
        """

    @abstractmethod
    async def fetch_orderbook(self, market_id: str) -> OrderBook:
        """Fetch a point-in-time normalized book snapshot."""

    @abstractmethod
    def stream(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        """Yield normalized book updates until cancelled."""

    async def close(self) -> None:
        """Release network resources. Default is a no-op."""
        return None
