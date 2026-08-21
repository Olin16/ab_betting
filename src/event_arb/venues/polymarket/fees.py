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

from decimal import Decimal

from event_arb.domain.enums import Venue
from event_arb.domain.models import FeeConfig
from event_arb.domain.money import ZERO, ceil_to

__all__ = ["CATEGORY_TAKER_RATES", "FEE_EXP", "MIN_FEE", "polymarket_fee_config", "taker_fee"]

FEE_EXP = Decimal("0.00001")
MIN_FEE = Decimal("0.00001")

#: Published 2026-08-21. ASSUMPTION: current and complete. Verify before live use.
CATEGORY_TAKER_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "geopolitical": ZERO,
    "world": ZERO,
}


def polymarket_fee_config(category: str | None = None) -> FeeConfig:
    """Build a fee config for a market category.

    An unknown category falls back to the **highest** published rate, not the lowest:
    overstating fees suppresses a marginal opportunity, whereas understating them invents
    one.
    """
    key = (category or "").strip().lower()
    if key in CATEGORY_TAKER_RATES:
        rate = CATEGORY_TAKER_RATES[key]
        source = f"docs.polymarket.com/trading/fees category={key}"
    else:
        rate = max(CATEGORY_TAKER_RATES.values())
        source = f"conservative default (unknown category={key or 'none'!r})"
    return FeeConfig(venue=Venue.POLYMARKET, taker_rate=rate, maker_rate=ZERO, source=source)


def taker_fee(quantity: Decimal, price: Decimal, config: FeeConfig) -> Decimal:
    """``C * feeRate * p * (1 - p)``, rounded up to 5 dp with a minimum charge."""
    if quantity <= ZERO or config.taker_rate <= ZERO:
        return ZERO
    raw = quantity * config.taker_rate * price * (Decimal("1") - price)
    if raw <= ZERO:
        return ZERO
    return max(MIN_FEE, ceil_to(raw, FEE_EXP))
