"""Translate Polymarket CLOB payloads into the normalized domain model.

Polymarket's CLOB is genuinely two-sided: ``/book`` returns both ``bids`` and ``asks``
with ``price`` and ``size`` string fields, per outcome **token**. A binary market has two
token IDs (YES and NO), so a normalized :class:`OrderBook` is assembled from two token
books.

Prices and sizes are parsed as ``Decimal`` from their string form; they never touch a
binary float.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from event_arb.domain.enums import Outcome, SettlementAsset, Venue
from event_arb.domain.models import (
    BookLevel,
    FeeConfig,
    OrderBook,
    OrderBookSide,
    VenueMarket,
)
from event_arb.venues.base import MarketDataError
from event_arb.venues.polymarket.fees import polymarket_fee_config

__all__ = ["build_book", "parse_market", "parse_token_book", "parse_ts_ms"]


def parse_ts_ms(value: Any) -> datetime | None:
    """Polymarket timestamps are Unix milliseconds, sometimes delivered as strings."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(str(value)) / 1000.0, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def _levels(raw: Any, field_name: str, descending: bool) -> tuple[BookLevel, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MarketDataError(
            f"Polymarket {field_name} must be a list, got {type(raw).__name__}", Venue.POLYMARKET
        )
    parsed: list[tuple[Decimal, Decimal]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise MarketDataError(
                f"Polymarket {field_name} entry must be an object", Venue.POLYMARKET
            )
        try:
            price = Decimal(str(entry["price"]))
            size = Decimal(str(entry["size"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise MarketDataError(
                f"unparseable Polymarket {field_name} entry: {entry!r}", Venue.POLYMARKET
            ) from exc
        if size <= 0:
            continue
        parsed.append((price, size))
    parsed.sort(key=lambda pair: pair[0], reverse=descending)
    return tuple(BookLevel(price=p, quantity=q) for p, q in parsed)


def parse_token_book(payload: dict[str, Any], outcome: Outcome) -> OrderBookSide:
    """Parse one token's ``/book`` response into a single normalized side."""
    return OrderBookSide(
        outcome=outcome,
        bids=_levels(payload.get("bids"), "bids", descending=True),
        asks=_levels(payload.get("asks"), "asks", descending=False),
    )


def build_book(
    market_id: str,
    yes_payload: dict[str, Any],
    no_payload: dict[str, Any] | None = None,
    received_at: datetime | None = None,
) -> OrderBook:
    """Assemble a normalized book from one or both outcome-token books.

    When only the YES token book is available, the NO side is derived by reciprocity
    (a YES bid at $0.60 is a NO ask at $0.40). Deriving is correct for a binary market,
    but the real NO book is preferred when supplied because it reflects genuine resting
    NO liquidity rather than a mirror of the YES side.
    """
    yes_side = parse_token_book(yes_payload, Outcome.YES)
    if no_payload is not None:
        no_side = parse_token_book(no_payload, Outcome.NO)
    else:
        one = Decimal("1")
        no_side = OrderBookSide(
            outcome=Outcome.NO,
            bids=tuple(
                BookLevel(price=one - level.price, quantity=level.quantity)
                for level in yes_side.asks
            ),
            asks=tuple(
                BookLevel(price=one - level.price, quantity=level.quantity)
                for level in yes_side.bids
            ),
        )

    return OrderBook(
        venue=Venue.POLYMARKET,
        market_id=market_id,
        yes=yes_side,
        no=no_side,
        exchange_timestamp=parse_ts_ms(yes_payload.get("timestamp")),
        received_at=received_at or datetime.now(UTC),
        # ASSUMPTION (api-assumptions B3): the market channel exposes no monotonic
        # sequence number, only a hash. Sequence-based gap detection is unavailable.
        sequence=None,
        is_complete=bool(yes_side.bids or yes_side.asks),
    )


def parse_market(payload: dict[str, Any], fee_config: FeeConfig | None = None) -> VenueMarket:
    """Normalize a Polymarket market object.

    The identifier used throughout this project is the condition/market id; the per-token
    asset ids are retained in ``raw_metadata`` because the order-book endpoints key on
    them.
    """
    market_id = payload.get("condition_id") or payload.get("id") or payload.get("market")
    if not isinstance(market_id, str) or not market_id:
        raise MarketDataError("Polymarket market payload missing an id", Venue.POLYMARKET)

    tokens = payload.get("tokens")
    outcomes: tuple[str, ...] = ("YES", "NO")
    if isinstance(tokens, list) and tokens:
        names = [str(t.get("outcome", "")) for t in tokens if isinstance(t, dict)]
        if all(names):
            outcomes = tuple(names)

    tick_raw = payload.get("minimum_tick_size", payload.get("tick_size"))
    try:
        tick = Decimal(str(tick_raw)) if tick_raw is not None else Decimal("0.01")
    except (InvalidOperation, ValueError):
        tick = Decimal("0.01")

    active = bool(payload.get("active", True))
    closed = bool(payload.get("closed", False))
    accepting = bool(payload.get("accepting_orders", active and not closed))

    end_iso = payload.get("end_date_iso") or payload.get("game_start_time")
    close_time: datetime | None = None
    if isinstance(end_iso, str):
        try:
            close_time = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except ValueError:
            close_time = None

    return VenueMarket(
        venue=Venue.POLYMARKET,
        venue_market_id=market_id,
        title=str(payload.get("question", payload.get("title", market_id))),
        rules_text=str(payload.get("description", "")),
        outcomes=outcomes,
        close_time=close_time,
        settlement_source=str(payload.get("resolution_source", "")),
        tick_size=tick,
        fee_config=fee_config or polymarket_fee_config(payload.get("category")),
        is_active=active and not closed,
        is_tradable=accepting and active and not closed,
        # Polymarket settles in USDC on Polygon, not USD. This distinction is material.
        settlement_asset=SettlementAsset.USDC,
        raw_metadata=dict(payload),
    )
