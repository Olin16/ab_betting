"""The odds-feed adapter contract.

Deliberately parallel to :class:`~event_arb.venues.base.VenueAdapter` but separate from it,
because the two describe different things: a venue is somewhere you can transact, a data
provider is somewhere you can read. There is no ``place_bet`` on this interface — not a
disabled one, not a private one. Sportsbook execution is a manual human action outside this
system entirely.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from event_arb.sportsbook.enums import DataProvider
from event_arb.sportsbook.models import SportsbookEvent, SportsbookQuote

__all__ = [
    "FeedHealth",
    "OddsFeedAdapter",
    "OddsFeedError",
    "ProviderAuthError",
    "ProviderDataError",
    "ProviderRateLimitError",
    "ProviderTransientError",
    "QuoteFreshnessPolicy",
    "QuoteUpdate",
]


class OddsFeedError(Exception):
    """Base class for normalized odds-provider errors."""

    def __init__(self, message: str, provider: DataProvider | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderTransientError(OddsFeedError):
    """Retryable: timeout, 5xx, dropped stream."""


class ProviderRateLimitError(ProviderTransientError):
    """429 or a documented rate-limit rejection.

    Provider rate limits are a licence term, not a performance knob.
    """

    def __init__(
        self,
        message: str,
        provider: DataProvider | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider)
        self.retry_after = retry_after


class ProviderAuthError(OddsFeedError):
    """401/403, missing or rejected API key. Not retryable."""


class ProviderDataError(OddsFeedError):
    """Malformed or unparseable payload. Not retryable — the schema likely changed."""


@dataclass(frozen=True)
class QuoteUpdate:
    """One normalized update emitted by a feed."""

    provider: DataProvider
    quote: SportsbookQuote
    #: Provider continuity token (OpticOdds ``entry_id``), when the feed supplies one.
    entry_id: str | None = None
    #: True when this update *removes or suspends* the quote rather than repricing it.
    is_suspension: bool = False


@dataclass
class FeedHealth:
    """Rolling health for one provider connection."""

    provider: DataProvider
    connected: bool = False
    last_message_at: datetime | None = None
    last_error: str | None = None
    reconnect_count: int = 0
    messages_received: int = 0
    malformed_messages: int = 0
    #: Latest continuity token, so a reconnect can resume where the stream stopped.
    last_entry_id: str | None = None
    subscribed_sports: set[str] = field(default_factory=set)

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
            "provider": self.provider.value,
            "connected": self.connected,
            "last_message_at": (self.last_message_at.isoformat() if self.last_message_at else None),
            "last_error": self.last_error,
            "reconnect_count": self.reconnect_count,
            "messages_received": self.messages_received,
            "malformed_messages": self.malformed_messages,
            "subscribed_sports": sorted(self.subscribed_sports),
            # Deliberately omitted: last_entry_id. It is provider state, not an operator
            # metric, and it can appear in logs that get shared.
        }


@dataclass(frozen=True)
class QuoteFreshnessPolicy:
    """Decides whether a quote is current enough to reason about.

    Judged from local receipt time, never the provider timestamp: a provider clock running
    fast would otherwise make an old price look actionable, which is the direction of error
    that manufactures an opportunity.

    The default window is far wider than the exchange one. Sportsbook prices move on a
    different cadence, and execution is manual anyway, so a two-second bound would reject
    everything without making anything safer.
    """

    max_age_ms: int = 30_000

    def is_fresh(self, quote: SportsbookQuote, now: datetime | None = None) -> bool:
        if quote.is_stale:
            return False
        return quote.age_ms(now) <= self.max_age_ms

    def apply(self, quote: SportsbookQuote, now: datetime | None = None) -> SportsbookQuote:
        """Return the quote, marked stale if it has aged out."""
        if quote.is_stale or self.is_fresh(quote, now):
            return quote
        return quote.model_copy(update={"is_stale": True})


class OddsFeedAdapter(ABC):
    """Read-only interface to a sportsbook-odds data provider."""

    provider: DataProvider

    def __init__(self, provider: DataProvider) -> None:
        self.provider = provider
        self.health = FeedHealth(provider=provider)

    @abstractmethod
    async def discover_events(
        self, sport: str | None = None, league: str | None = None
    ) -> list[SportsbookEvent]:
        """List fixtures with normalized metadata."""

    @abstractmethod
    async def fetch_quotes(
        self, event_ids: list[str], sportsbooks: list[str] | None = None
    ) -> list[SportsbookQuote]:
        """Fetch current quotes for the named events."""

    @abstractmethod
    def stream(
        self, sports: list[str], sportsbooks: list[str] | None = None
    ) -> AsyncIterator[QuoteUpdate]:
        """Yield normalized quote updates until cancelled."""

    async def close(self) -> None:
        """Release resources. Default is a no-op."""
        return None
