"""Polymarket US payload mapping.

Schema read from <https://docs.polymarket.us/api-reference/market/overview> on 2026-08-23.
**No API calls have been made.**

Identifiers are **slugs and market ids with named sides**, not condition/token ids. Books
use ``bids``/``offers`` with ``px``/``qty``. Settlement is **USD**, so none of the Global
adapter's USDC, Polygon, wallet or token-id machinery applies — and none of it is imported.

## The rules-completeness problem

The documented market object exposes ``description``, and the documentation does **not**
state that this contains the complete enforceable resolution rules. That distinction
decides whether a pair may be approved at all: an approval is a human statement that two
contracts settle identically, and it cannot be made against a marketing blurb.

So :func:`rules_completeness` classifies what we actually received, and a market whose
rules look insufficient is marked not-approvable rather than being quietly approved against
partial text. Confirming this is an explicit item for the supervised smoke test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
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
from event_arb.venues.polymarket_us.fees import polymarket_us_fee_config

__all__ = [
    "PRODUCT",
    "RulesCompleteness",
    "parse_book",
    "parse_market",
    "parse_payload",
    "rules_completeness",
]

#: Product discriminator stored alongside the venue, so a Global approval can never be
#: restored against a US market.
PRODUCT = "polymarket_us"

#: Below this, a description cannot plausibly carry enforceable resolution rules.
_MIN_RULES_CHARS = 120

#: Phrases that indicate actual settlement conditions rather than a summary.
_RULES_MARKERS = (
    "resolve",
    "settle",
    "if the",
    "will be deemed",
    "official",
    "source",
    "deadline",
)


class RulesCompleteness(StrEnum):
    """How much confidence we have that we hold the enforceable rules."""

    #: Long text with settlement language: plausibly complete, still human-reviewed.
    LIKELY_COMPLETE = "likely_complete"
    #: Present but short or lacking settlement language.
    INSUFFICIENT = "insufficient"
    #: No description at all.
    MISSING = "missing"


def rules_completeness(text: str) -> RulesCompleteness:
    """Classify whether a description plausibly contains enforceable rules."""
    stripped = (text or "").strip()
    if not stripped:
        return RulesCompleteness.MISSING
    lowered = stripped.lower()
    has_markers = sum(marker in lowered for marker in _RULES_MARKERS) >= 2
    if len(stripped) >= _MIN_RULES_CHARS and has_markers:
        return RulesCompleteness.LIKELY_COMPLETE
    return RulesCompleteness.INSUFFICIENT


def parse_payload(text: str) -> Any:
    """Decode a Polymarket US response keeping every number exact.

    ``parse_float=Decimal`` matters here as much as it does for odds feeds: prices and
    sizes arrive as JSON numbers, and a stock decoder would turn them into binary floats
    before any of our code could object.
    """
    try:
        return json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        raise MarketDataError("Polymarket US returned non-JSON", Venue.POLYMARKET) from exc


def _decimal(value: Any, field: str, default: Decimal | None = None) -> Decimal:
    if value is None:
        if default is not None:
            return default
        raise MarketDataError(f"Polymarket US {field} is missing", Venue.POLYMARKET)
    if isinstance(value, float):
        raise MarketDataError(
            f"Polymarket US {field} arrived as a float; decode with parse_float=Decimal",
            Venue.POLYMARKET,
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise MarketDataError(
                f"unparseable Polymarket US {field}: {value!r}", Venue.POLYMARKET
            ) from exc
    raise MarketDataError(
        f"unsupported Polymarket US {field} type {type(value).__name__}", Venue.POLYMARKET
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _levels(raw: Any, field: str, descending: bool) -> tuple[BookLevel, ...]:
    """Parse a ``bids`` or ``offers`` array of ``{px, qty}`` entries."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MarketDataError(
            f"Polymarket US {field} must be a list, got {type(raw).__name__}", Venue.POLYMARKET
        )
    parsed: list[tuple[Decimal, Decimal]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise MarketDataError(
                f"Polymarket US {field} entry must be an object", Venue.POLYMARKET
            )
        price = _decimal(entry.get("px", entry.get("price")), f"{field}.px")
        size = _decimal(entry.get("qty", entry.get("size")), f"{field}.qty")
        if size <= 0:
            continue
        parsed.append((price, size))
    parsed.sort(key=lambda pair: pair[0], reverse=descending)
    return tuple(BookLevel(price=p, quantity=q) for p, q in parsed)


def parse_book(
    market_id: str,
    payload: dict[str, Any],
    no_payload: dict[str, Any] | None = None,
    received_at: datetime | None = None,
) -> OrderBook:
    """Build a normalized two-sided book from ``/v1/markets/{slug}/book`` responses.

    Polymarket US publishes ``bids`` and ``offers`` per market side. When only one side's
    book is supplied the other is derived by binary reciprocity (a YES bid at $0.60 is a NO
    ask at $0.40), which is an identity rather than an approximation — but the real
    opposite-side book is preferred when available, because it reflects genuine resting
    liquidity instead of a mirror.
    """
    if not isinstance(payload, dict):
        raise MarketDataError("Polymarket US book payload must be an object", Venue.POLYMARKET)

    body = payload.get("book", payload)
    if not isinstance(body, dict):
        raise MarketDataError("Polymarket US book body must be an object", Venue.POLYMARKET)

    yes = OrderBookSide(
        outcome=Outcome.YES,
        bids=_levels(body.get("bids"), "bids", descending=True),
        asks=_levels(body.get("offers", body.get("asks")), "offers", descending=False),
    )

    if no_payload is not None:
        no_body = no_payload.get("book", no_payload)
        no = OrderBookSide(
            outcome=Outcome.NO,
            bids=_levels(no_body.get("bids"), "bids", descending=True),
            asks=_levels(no_body.get("offers", no_body.get("asks")), "offers", descending=False),
        )
    else:
        one = Decimal("1")
        no = OrderBookSide(
            outcome=Outcome.NO,
            bids=tuple(
                BookLevel(price=one - level.price, quantity=level.quantity) for level in yes.asks
            ),
            asks=tuple(
                BookLevel(price=one - level.price, quantity=level.quantity) for level in yes.bids
            ),
        )

    return OrderBook(
        venue=Venue.POLYMARKET,
        market_id=market_id,
        yes=yes,
        no=no,
        exchange_timestamp=_parse_time(body.get("timestamp")),
        received_at=received_at or datetime.now(UTC),
        # ASSUMPTION: the public book carries no monotonic sequence number. Continuity is
        # therefore unproven and every book is re-fetched rather than incrementally trusted.
        sequence=None,
        is_complete=bool(yes.bids or yes.asks),
    )


def _json_string_list(value: Any) -> tuple[str, ...]:
    """Parse a field that arrives as a JSON-encoded *string*.

    ``outcomes`` is delivered as ``'["Yes","No"]'`` rather than a list. Reading it as a
    sequence without decoding would iterate the characters of a string.
    """
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except ValueError:
            return ()
        if isinstance(decoded, list):
            return tuple(str(item) for item in decoded)
    return ()


def _outcomes(payload: dict[str, Any]) -> tuple[str, ...]:
    """Prefer ``marketSides`` for outcome identity, falling back to ``outcomes``.

    ``marketSides`` carries the authoritative side identity — ``identifier``, a
    ``description`` such as "Yes", and a ``long`` boolean — while ``outcomes`` is a
    display-oriented JSON string.
    """
    sides = payload.get("marketSides")
    if isinstance(sides, list) and sides:
        names = [
            str(side.get("description", "")).strip()
            for side in sides
            if isinstance(side, dict) and side.get("description")
        ]
        if names:
            return tuple(names)
    decoded = _json_string_list(payload.get("outcomes"))
    return decoded or ("YES", "NO")


def _is_open(payload: dict[str, Any]) -> bool:
    """Interpret the enum-style status alongside the boolean flags."""
    status = str(payload.get("status", "")).upper()
    if status and status != "MARKET_STATUS_OPEN":
        return False
    return (
        bool(payload.get("active", True))
        and not bool(payload.get("closed", False))
        and not bool(payload.get("archived", False))
    )


def parse_market(payload: dict[str, Any], fee_config: FeeConfig | None = None) -> VenueMarket:
    """Normalize a Polymarket US market object."""
    if not isinstance(payload, dict):
        raise MarketDataError("Polymarket US market must be an object", Venue.POLYMARKET)

    market_id = payload.get("slug") or payload.get("id")
    if not isinstance(market_id, str) or not market_id:
        raise MarketDataError("Polymarket US market missing 'slug' and 'id'", Venue.POLYMARKET)

    description = str(payload.get("description", "") or "")
    completeness = rules_completeness(description)

    is_open = _is_open(payload)

    tick = _decimal(payload.get("orderPriceMinTickSize"), "orderPriceMinTickSize", Decimal("0.01"))
    min_qty = _decimal(payload.get("minimumTradeQty"), "minimumTradeQty", Decimal("1"))

    # The market object carries its own fee coefficient, which is exactly the per-market
    # fee setting the design requires. Prefer it over any configured default; fall back
    # only when the venue omits it.
    resolved_fees = fee_config
    if resolved_fees is None:
        coefficient = payload.get("feeCoefficient")
        resolved_fees = (
            polymarket_us_fee_config(
                taker_theta=_decimal(coefficient, "feeCoefficient"),
                source="market.feeCoefficient",
            )
            if coefficient is not None
            else polymarket_us_fee_config()
        )

    metadata = dict(payload)
    metadata["_product"] = PRODUCT
    metadata["_rules_completeness"] = completeness.value

    return VenueMarket(
        venue=Venue.POLYMARKET,
        venue_market_id=market_id,
        title=str(payload.get("question", payload.get("title", market_id))),
        rules_text=description,
        outcomes=_outcomes(payload),
        close_time=_parse_time(payload.get("endDate") or payload.get("closeTime")),
        # Live payloads carry no resolutionSource field. It stays empty rather than being
        # invented, and an empty settlement source is itself material at approval time.
        settlement_source=str(payload.get("resolutionSource", "") or ""),
        tick_size=tick,
        min_quantity_increment=min_qty,
        fee_config=resolved_fees,
        is_active=is_open,
        # A market whose rules we cannot read in full is not tradable for our purposes,
        # because it cannot be honestly approved as settlement-equivalent to anything.
        is_tradable=(is_open and completeness is RulesCompleteness.LIKELY_COMPLETE),
        # USD, not USDC. No haircut, no depeg, no Polygon.
        settlement_asset=SettlementAsset.USD,
        raw_metadata=metadata,
    )
