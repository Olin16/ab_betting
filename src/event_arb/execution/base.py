"""Execution interface definitions (Phase 7 scaffolding — nothing here submits orders).

These types exist so that the safety controls, audit log, and dashboard can be written
and tested *before* any real submission path exists. No implementation in this build can
reach a venue's order endpoint.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.domain.enums import Outcome, Venue

__all__ = ["ExecutionAdapter", "OrderRequest", "OrderResult", "OrderState", "Signer"]


class OrderState:
    """Terminal and non-terminal order states.

    ``UNKNOWN`` exists because it is the most dangerous real state: an API timeout leaves
    the order possibly filled, possibly not. Any future implementation must resolve
    ``UNKNOWN`` by querying the venue before acting again, never by assuming failure.
    """

    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderRequest:
    """A prospective order. Constructing one does not send anything."""

    venue: Venue
    market_id: str
    outcome: Outcome
    quantity: Decimal
    limit_price: Decimal
    #: Idempotency key. A retry must reuse it so a timeout cannot double-fill.
    client_order_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class OrderResult:
    venue: Venue
    client_order_id: str
    venue_order_id: str | None
    state: str
    filled_quantity: Decimal
    average_price: Decimal
    message: str = ""


class Signer(ABC):
    """Isolates wallet/key material behind an interface.

    Private keys must never be passed around as data. A future implementation holds the
    key inside the signer (or delegates to an HSM/remote signer) and exposes only the
    ability to sign a specific payload.
    """

    @abstractmethod
    def sign_payload(self, payload: bytes) -> bytes:
        """Sign a payload. Implementations must never log the payload or the signature."""

    @abstractmethod
    def public_identifier(self) -> str:
        """A non-secret identifier (address or key id) safe to log."""


class ExecutionAdapter(ABC):
    """The interface a live trading adapter would implement.

    Deliberately unimplemented in this build.
    """

    @abstractmethod
    async def submit(self, request: OrderRequest) -> OrderResult:
        """Submit an order."""

    @abstractmethod
    async def cancel(self, venue: Venue, venue_order_id: str) -> OrderResult:
        """Cancel a resting order."""

    @abstractmethod
    async def get_order(self, venue: Venue, client_order_id: str) -> OrderResult:
        """Resolve an order's true state — required after any timeout."""
