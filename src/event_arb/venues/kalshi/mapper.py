"""Translate Kalshi API payloads into the normalized domain model.

Two Kalshi-specific facts drive this module.

**Bids only.** Kalshi's orderbook endpoint returns ``yes_dollars`` and ``no_dollars``,
both of which are *bid* arrays. There are no asks. In a binary market the two sides are
reciprocal: a NO bid at $0.40 is exactly a YES ask at $0.60, for the same contract count.
The mapper synthesizes the missing asks here, once, so that the opportunity engine can
treat both venues identically.

**Dollar strings and fixed-point counts.** Prices arrive as ``"0.4200"`` and quantities as
``"13.00"``. They are parsed straight into ``Decimal`` — never through ``float``.

Arrays arrive sorted ascending (best bid last); the normalized model sorts bids
descending and asks ascending so that "index 0 is the best price" holds everywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from event_arb.domain.enums import Outcome, SettlementAsset, Venue, VenueProduct
from event_arb.domain.models import (
    BookLevel,
    FeeConfig,
    OrderBook,
    OrderBookSide,
    VenueMarket,
)
from event_arb.domain.money import ONE_DOLLAR
from event_arb.venues.base import MarketDataError
from event_arb.venues.kalshi.fees import default_kalshi_fee_config

__all__ = ["parse_market", "parse_orderbook", "parse_ts"]

_LevelPair = tuple[Decimal, Decimal]


def _parse_levels(raw: Any, field_name: str) -> list[_LevelPair]:
    """Parse ``[["0.4200", "13.00"], ...]`` into Decimal pairs."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MarketDataError(
            f"Kalshi {field_name} must be a list, got {type(raw).__name__}", Venue.KALSHI
        )
    levels: list[_LevelPair] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise MarketDataError(f"Kalshi {field_name} entry must be [price, count]", Venue.KALSHI)
        try:
            price = Decimal(str(entry[0]))
            qty = Decimal(str(entry[1]))
        except (InvalidOperation, ValueError) as exc:
            raise MarketDataError(
                f"unparseable Kalshi {field_name} entry: {entry!r}", Venue.KALSHI
            ) from exc
        if qty <= 0:
            continue
        levels.append((price, qty))
    return levels


def parse_ts(value: Any) -> datetime | None:
    """Parse a Kalshi timestamp (ISO-8601 string or epoch milliseconds)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def parse_orderbook(
    market_id: str,
    payload: dict[str, Any],
    received_at: datetime | None = None,
    sequence: int | None = None,
) -> OrderBook:
    """Build a normalized two-sided book from a Kalshi orderbook payload.

    Accepts either the full response (``{"orderbook_fp": {...}}``) or the inner object,
    which is the shape the WebSocket snapshot message carries.
    """
    inner = payload.get("orderbook_fp", payload)
    if not isinstance(inner, dict):
        raise MarketDataError("Kalshi orderbook payload missing orderbook_fp", Venue.KALSHI)

    # The WebSocket snapshot uses *_dollars_fp; REST uses *_dollars.
    yes_raw = inner.get("yes_dollars", inner.get("yes_dollars_fp"))
    no_raw = inner.get("no_dollars", inner.get("no_dollars_fp"))
    if yes_raw is None and no_raw is None:
        raise MarketDataError(
            "Kalshi orderbook has neither yes_dollars nor no_dollars", Venue.KALSHI
        )

    yes_bids = _parse_levels(yes_raw, "yes_dollars")
    no_bids = _parse_levels(no_raw, "no_dollars")

    return build_book(
        market_id=market_id,
        yes_bids=yes_bids,
        no_bids=no_bids,
        received_at=received_at,
        sequence=sequence,
    )


def build_book(
    market_id: str,
    yes_bids: list[_LevelPair],
    no_bids: list[_LevelPair],
    received_at: datetime | None = None,
    sequence: int | None = None,
    exchange_timestamp: datetime | None = None,
) -> OrderBook:
    """Assemble a normalized book, synthesizing asks from the opposite side's bids.

    A NO bid at price ``p`` is a YES ask at ``1 - p`` for the same quantity, and vice
    versa. This is an identity of binary contracts, not an approximation.
    """
    yes_asks = [(ONE_DOLLAR - price, qty) for price, qty in no_bids]
    no_asks = [(ONE_DOLLAR - price, qty) for price, qty in yes_bids]

    def levels(pairs: list[_LevelPair], descending: bool) -> tuple[BookLevel, ...]:
        ordered = sorted(pairs, key=lambda pair: pair[0], reverse=descending)
        return tuple(BookLevel(price=p, quantity=q) for p, q in ordered)

    now = received_at or datetime.now(UTC)
    return OrderBook(
        venue=Venue.KALSHI,
        product=VenueProduct.NONE,
        market_id=market_id,
        yes=OrderBookSide(
            outcome=Outcome.YES,
            bids=levels(yes_bids, descending=True),
            asks=levels(yes_asks, descending=False),
        ),
        no=OrderBookSide(
            outcome=Outcome.NO,
            bids=levels(no_bids, descending=True),
            asks=levels(no_asks, descending=False),
        ),
        exchange_timestamp=exchange_timestamp,
        received_at=now,
        sequence=sequence,
        is_complete=bool(yes_bids or no_bids),
    )


def _tick_size(payload: dict[str, Any]) -> Decimal:
    """Resolve the price increment.

    Live payloads carry no ``tick_size_dollars``. The increment comes from
    ``price_ranges[].step`` alongside ``price_level_structure`` (observed ``deci_cent``,
    step ``0.0010``). Defaulting to a penny would be too coarse and would make the
    depth walker skip legitimate sub-penny levels as off-grid — suppressing real
    liquidity rather than inventing it, but wrong either way.
    """
    ranges = payload.get("price_ranges")
    if isinstance(ranges, list):
        steps: list[Decimal] = []
        for entry in ranges:
            if not isinstance(entry, dict) or entry.get("step") is None:
                continue
            try:
                step = Decimal(str(entry["step"]))
            except (InvalidOperation, ValueError):
                continue
            if step > 0:
                steps.append(step)
        if steps:
            # Finest step across ranges: a coarser value would reject valid levels.
            return min(steps)

    legacy = payload.get("tick_size_dollars", payload.get("tick_size"))
    if legacy is not None:
        try:
            return Decimal(str(legacy))
        except (InvalidOperation, ValueError):
            pass
    return Decimal("0.01")


def parse_market(payload: dict[str, Any], fee_config: FeeConfig | None = None) -> VenueMarket:
    """Normalize a Kalshi market object.

    Field presence varies across Kalshi's ``*_dollars`` / ``*_fp`` migration, so missing
    optional fields degrade gracefully rather than raising: losing a tick size should not
    take the scanner down.
    """
    ticker = payload.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        raise MarketDataError("Kalshi market payload missing 'ticker'", Venue.KALSHI)

    status = str(payload.get("status", "")).lower()
    tick = _tick_size(payload)

    rules = " ".join(
        str(payload.get(key, ""))
        for key in ("rules_primary", "rules_secondary")
        if payload.get(key)
    ).strip()

    return VenueMarket(
        venue=Venue.KALSHI,
        venue_market_id=ticker,
        title=str(payload.get("title", ticker)),
        rules_text=rules,
        outcomes=("YES", "NO"),
        close_time=parse_ts(payload.get("close_time")),
        settlement_source=str(payload.get("settlement_source", "")),
        tick_size=tick,
        fee_config=fee_config or default_kalshi_fee_config(),
        is_active=status in {"active", "open", ""},
        is_tradable=status in {"active", "open"},
        settlement_asset=SettlementAsset.USD,
        raw_metadata=dict(payload),
    )
