"""Polymarket fee model.

Source (verified 2026-08-21): <https://docs.polymarket.com/trading/fees>

    fee = C * feeRate * p * (1 - p)

Takers only — makers are never charged. ``feeRate`` is **category dependent**, so it is
carried per market in a :class:`~event_arb.domain.models.FeeConfig` rather than being a
module constant. The published rates at the time of writing are recorded in
:data:`CATEGORY_TAKER_RATES` purely as a starting point for configuration; they must be
re-verified against the live docs before being relied upon.

Fees round to five decimal places with a 0.00001 USDC minimum charge on a non-zero fee.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from event_arb.domain.enums import FeePolicy, Venue
from event_arb.domain.models import FeeConfig
from event_arb.domain.money import ZERO, ceil_to

__all__ = [
    "CATEGORY_TAKER_RATES",
    "FEE_EXP",
    "MIN_FEE",
    "polymarket_fee_config",
    "polymarket_fee_config_from_market",
    "taker_fee",
]

FEE_EXP = Decimal("0.00001")
MIN_FEE = Decimal("0.00001")

#: Published 2026-08-22. This table is a documented fallback only: live discovery should
#: prefer a venue-provided per-market rate whenever the API exposes one.
CATEGORY_TAKER_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "general": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "geopolitics": ZERO,
    "world": ZERO,
}

_CATEGORY_ALIASES: dict[str, str] = {
    "financial": "finance",
    "economic": "economics",
    "sport": "sports",
    "technology": "tech",
    "geopolitical": "geopolitics",
    "world events": "world",
}


def polymarket_fee_config(category: str | None = None) -> FeeConfig:
    """Build a fee config for a market category.

    An unknown category falls back to the **highest** published rate, not the lowest:
    overstating fees suppresses a marginal opportunity, whereas understating them invents
    one.
    """
    supplied_key = (category or "").strip().lower()
    key = _CATEGORY_ALIASES.get(supplied_key, supplied_key)
    if key in CATEGORY_TAKER_RATES:
        rate = CATEGORY_TAKER_RATES[key]
        source = f"docs.polymarket.com/trading/fees category={key}"
    else:
        rate = max(CATEGORY_TAKER_RATES.values())
        source = f"conservative default (unknown category={supplied_key or 'none'!r})"
    return FeeConfig(
        venue=Venue.POLYMARKET,
        taker_rate=rate,
        maker_rate=ZERO,
        fee_policy=FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL,
        source=source,
    )


def polymarket_fee_config_from_market(payload: dict[str, Any]) -> FeeConfig:
    """Build a fee config from a market payload, preferring venue-provided rates.

    The market object exposes ``feesEnabled`` and ``feeSchedule.rate``
    (docs.polymarket.com/market-data/market-details). Those are authoritative for that
    market; the category table is only a fallback for when they are absent, because a
    hard-coded rate table goes stale silently and the wrong direction of staleness
    invents profit.

    Precedence:

    1. ``feesEnabled: false`` -> zero rate, whatever else the payload says;
    2. a parseable ``feeSchedule.rate`` -> that exact rate;
    3. the documented category table;
    4. the conservative maximum published rate.
    """
    fees_enabled = payload.get("feesEnabled")
    if fees_enabled is False:
        return FeeConfig(
            venue=Venue.POLYMARKET,
            taker_rate=ZERO,
            maker_rate=ZERO,
            fee_policy=FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL,
            source="market.feesEnabled=false",
        )

    schedule = payload.get("feeSchedule")
    if isinstance(schedule, dict) and schedule.get("rate") is not None:
        rate: Decimal | None
        try:
            rate = Decimal(str(schedule["rate"]))
        except (InvalidOperation, ValueError):
            # A malformed rate must never read as cheap.
            rate = None
        if rate is not None and rate >= ZERO:
            return FeeConfig(
                venue=Venue.POLYMARKET,
                taker_rate=rate,
                maker_rate=ZERO,
                fee_policy=FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL,
                source="market.feeSchedule.rate",
            )

    category = payload.get("category")
    return polymarket_fee_config(category if isinstance(category, str) else None)


def taker_fee(quantity: Decimal, price: Decimal, config: FeeConfig) -> Decimal:
    """``C * feeRate * p * (1 - p)``, rounded up to 5 dp with a minimum charge."""
    if quantity <= ZERO or config.taker_rate <= ZERO:
        return ZERO
    raw = quantity * config.taker_rate * price * (Decimal("1") - price)
    if raw <= ZERO:
        return ZERO
    return max(MIN_FEE, ceil_to(raw, FEE_EXP))
