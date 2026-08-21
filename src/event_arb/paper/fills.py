"""Fill simulation against a live book.

The simulator's job is to be *pessimistic in the right places*. Between detecting an
opportunity and an order arriving at the venue, the book moves. Three effects are modeled:

- **latency**: the book is re-read at arrival time, not detection time;
- **adverse selection**: a configurable fraction of each level is assumed to have been
  taken by someone faster;
- **partial fills**: whatever remains is filled, and the shortfall is reported rather
  than quietly rounded away.

There is no optimistic path here. A simulator that fills everything at the detected price
would validate strategies that lose money in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from event_arb.domain.enums import Outcome, Venue
from event_arb.domain.models import BookLevel, OrderBook
from event_arb.domain.money import ZERO, quantize_price, quantize_qty
from event_arb.opportunities.depth import Fill, walk_asks

__all__ = ["SimulatedOrder", "simulate_buy"]


@dataclass(frozen=True)
class SimulatedOrder:
    """The result of one simulated venue order."""

    venue: Venue
    market_id: str
    outcome: Outcome
    requested_quantity: Decimal
    filled_quantity: Decimal
    fills: tuple[Fill, ...]
    gross_cost: Decimal
    limit_price: Decimal | None
    client_order_id: str

    @property
    def is_complete(self) -> bool:
        return self.filled_quantity >= self.requested_quantity

    @property
    def is_empty(self) -> bool:
        return self.filled_quantity <= ZERO

    @property
    def shortfall(self) -> Decimal:
        return quantize_qty(self.requested_quantity - self.filled_quantity)

    @property
    def vwap(self) -> Decimal:
        if self.filled_quantity <= ZERO:
            return ZERO
        return quantize_price(self.gross_cost / self.filled_quantity)


def _apply_adverse_selection(
    asks: tuple[BookLevel, ...], adverse_pct: Decimal
) -> tuple[BookLevel, ...]:
    """Remove a fraction of each level, simulating faster traders arriving first."""
    if adverse_pct <= ZERO:
        return asks
    factor = (Decimal("100") - adverse_pct) / Decimal("100")
    reduced: list[BookLevel] = []
    for level in asks:
        remaining = quantize_qty(level.quantity * factor)
        if remaining > ZERO:
            reduced.append(BookLevel(price=level.price, quantity=remaining))
    return tuple(reduced)


def simulate_buy(
    book: OrderBook,
    outcome: Outcome,
    quantity: Decimal,
    client_order_id: str,
    limit_price: Decimal | None = None,
    adverse_pct: Decimal = ZERO,
) -> SimulatedOrder:
    """Simulate a marketable buy order against ``book``.

    ``limit_price`` is treated as a hard ceiling: the simulator will take a partial fill
    rather than pay above it, which is how the real order would behave.
    """
    asks = _apply_adverse_selection(book.side(outcome).asks, adverse_pct)
    walk = walk_asks(asks, quantity, max_price=limit_price)
    return SimulatedOrder(
        venue=book.venue,
        market_id=book.market_id,
        outcome=outcome,
        requested_quantity=quantize_qty(quantity),
        filled_quantity=walk.filled_quantity,
        fills=walk.fills,
        gross_cost=walk.gross_cost,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )
