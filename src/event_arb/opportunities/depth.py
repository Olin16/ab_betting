"""Order-book depth walking.

The single most common way to invent an arbitrage that does not exist is to price it off
the top of book or off a midpoint. A 3-cent spread on 5 contracts of displayed size is
not a 3-cent spread on 500 contracts, and a midpoint is a price at which *nobody has
agreed to trade*.

Everything in this module therefore works on **executable** liquidity only: it consumes
real ask levels in price order and reports the volume-weighted average price actually
achievable for a given quantity, together with the individual fills so that venue fee
models can charge per fill (which Kalshi's rounding requires).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from event_arb.domain.models import BookLevel
from event_arb.domain.money import ZERO, floor_to, quantize_price, quantize_qty

__all__ = ["DepthWalk", "Fill", "available_depth", "walk_asks"]


@dataclass(frozen=True)
class Fill:
    """One simulated fill against a single price level."""

    price: Decimal
    quantity: Decimal

    @property
    def cost(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True)
class DepthWalk:
    """The executable result of consuming a book to a requested quantity."""

    requested_quantity: Decimal
    filled_quantity: Decimal
    fills: tuple[Fill, ...]
    gross_cost: Decimal

    @property
    def is_complete(self) -> bool:
        return self.filled_quantity >= self.requested_quantity

    @property
    def levels_consumed(self) -> int:
        return len(self.fills)

    @property
    def vwap(self) -> Decimal:
        """Volume-weighted average executable price, or zero if nothing filled."""
        if self.filled_quantity <= ZERO:
            return ZERO
        return quantize_price(self.gross_cost / self.filled_quantity)

    @property
    def worst_price(self) -> Decimal:
        """The deepest price touched — what the last contract actually costs."""
        return self.fills[-1].price if self.fills else ZERO


def available_depth(levels: tuple[BookLevel, ...], max_price: Decimal | None = None) -> Decimal:
    """Total quantity resting at or better than ``max_price``."""
    total = ZERO
    for level in levels:
        if max_price is not None and level.price > max_price:
            break
        total += level.quantity
    return quantize_qty(total)


def walk_asks(
    asks: tuple[BookLevel, ...],
    quantity: Decimal,
    tick_size: Decimal | None = None,
    max_price: Decimal | None = None,
    min_quantity_increment: Decimal | None = None,
) -> DepthWalk:
    """Consume ask levels in price order to fill ``quantity``.

    Ask levels are assumed sorted best (lowest) first, which the normalized
    :class:`~event_arb.domain.models.OrderBook` guarantees.

    ``max_price`` stops the walk once prices exceed a limit — used to avoid "filling" at
    prices that would already have destroyed the edge. Partial fills are reported
    honestly via :attr:`DepthWalk.is_complete` rather than being padded out.
    """
    if quantity <= ZERO:
        return DepthWalk(ZERO, ZERO, (), ZERO)

    remaining = quantity
    fills: list[Fill] = []
    gross = ZERO

    for level in asks:
        if remaining <= ZERO:
            break
        if max_price is not None and level.price > max_price:
            break
        if tick_size is not None and tick_size > ZERO:
            # A price off the venue's tick grid means our book model disagrees with the
            # venue; refusing to fill there is the conservative choice.
            remainder = (level.price / tick_size) % 1
            if remainder != 0:
                continue

        take = min(remaining, level.quantity)
        if take <= ZERO:
            continue
        fills.append(Fill(price=level.price, quantity=quantize_qty(take)))
        gross += level.price * take
        remaining -= take

    filled = quantize_qty(quantity - remaining)

    if min_quantity_increment is not None and min_quantity_increment > ZERO:
        # Round the executable size *down* to a tradable increment. Rounding up would
        # claim a fill the book cannot support.
        increments = floor_to(filled / min_quantity_increment, Decimal("1"))
        adjusted = increments * min_quantity_increment
        if adjusted != filled:
            return _retake(fills, adjusted)

    return DepthWalk(
        requested_quantity=quantity,
        filled_quantity=filled,
        fills=tuple(fills),
        gross_cost=gross,
    )


def _retake(fills: list[Fill], target: Decimal) -> DepthWalk:
    """Rebuild a walk truncated to ``target`` quantity, cheapest fills first."""
    remaining = target
    trimmed: list[Fill] = []
    gross = ZERO
    for fill in fills:
        if remaining <= ZERO:
            break
        take = min(remaining, fill.quantity)
        trimmed.append(Fill(price=fill.price, quantity=quantize_qty(take)))
        gross += fill.price * take
        remaining -= take
    filled = quantize_qty(target - remaining)
    return DepthWalk(
        requested_quantity=target,
        filled_quantity=filled,
        fills=tuple(trimmed),
        gross_cost=gross,
    )
