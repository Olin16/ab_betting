"""Polymarket US fee model.

Source: <https://docs.polymarket.us/fees>, read 2026-08-23.

    Fee = theta * C * p * (1 - p)

with ``C`` contracts and ``p`` the trade price in dollars ($0.01-$0.99).

Three things make this materially different from both other venues, and each one is a
place a copied implementation would be silently wrong:

**Theta differs by side.** Taker ``0.06``; maker ``-0.0125`` — negative, because makers are
*paid* a rebate rather than charged. It is represented here as a positive
``maker_rebate_rate`` so that "rate" keeps meaning "amount charged" everywhere, and a
rebate cannot be mistaken for a fee by a caller that forgets the sign.

**Rounding is banker's rounding to the nearest cent.** Kalshi ceils to a centicent and then
floors the balance change; Polymarket Global ceils to five decimals. Round-half-to-even is
neither, and at the half-cent boundary it rounds *down* half the time — so a model that
assumed a ceiling would overstate fees, and one that assumed truncation would understate
them.

**Taker rebates are retrospective and volume-tiered** (10% / 25% / 50% above $250k / $1M /
$10M of prior-month taker volume). They are not applied at trade time and are deliberately
**not** netted into the fee here: assuming a rebate you have not yet earned understates
cost, which is the direction of error that invents profit.
"""

from __future__ import annotations

from decimal import Decimal

from event_arb.domain.enums import FeePolicy, Venue
from event_arb.domain.models import FeeConfig
from event_arb.domain.money import CENT_EXP, ZERO, round_half_even_to

__all__ = [
    "MAKER_REBATE_THETA",
    "TAKER_REBATE_TIERS",
    "TAKER_THETA",
    "maker_rebate",
    "polymarket_us_fee_config",
    "taker_fee",
]

#: Published 2026-08-23. Configuration, not a constant in the calculation path.
TAKER_THETA = Decimal("0.06")

#: Published as ``-0.0125``. Held positive here; see the module docstring.
MAKER_REBATE_THETA = Decimal("0.0125")

#: Prior-month taker volume -> rebate fraction. Retrospective; never netted into a quote.
TAKER_REBATE_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10000000"), Decimal("0.50")),
    (Decimal("1000000"), Decimal("0.25")),
    (Decimal("250000"), Decimal("0.10")),
)


def polymarket_us_fee_config(
    taker_theta: Decimal | None = None,
    maker_rebate_theta: Decimal | None = None,
    source: str = "docs.polymarket.us/fees (read 2026-08-23)",
) -> FeeConfig:
    """Build a fee config for a Polymarket US market.

    Fee parameters are configuration and must be refreshed from the live schedule; the
    published values are defaults, not guarantees.
    """
    return FeeConfig(
        venue=Venue.POLYMARKET,
        fee_policy=FeePolicy.POLYMARKET_US_BANKERS_CENT,
        taker_rate=TAKER_THETA if taker_theta is None else taker_theta,
        # Makers are not *charged*; they are paid. The charge is zero.
        maker_rate=ZERO,
        maker_rebate_rate=(
            MAKER_REBATE_THETA if maker_rebate_theta is None else maker_rebate_theta
        ),
        source=source,
    )


def _fee_base(quantity: Decimal, price: Decimal, theta: Decimal) -> Decimal:
    if quantity <= ZERO or theta <= ZERO:
        return ZERO
    return theta * quantity * price * (Decimal("1") - price)


def taker_fee(quantity: Decimal, price: Decimal, config: FeeConfig) -> Decimal:
    """``theta * C * p * (1 - p)``, banker's-rounded to the nearest cent."""
    raw = _fee_base(quantity, price, config.taker_rate)
    if raw <= ZERO:
        return ZERO
    return round_half_even_to(raw, CENT_EXP)


def maker_rebate(quantity: Decimal, price: Decimal, config: FeeConfig) -> Decimal:
    """Rebate **paid to** a resting maker, as a positive amount.

    Informational for this project: an arbitrage scanner crosses the spread and is a taker
    on both legs, so it never earns this.
    """
    raw = _fee_base(quantity, price, config.maker_rebate_rate)
    if raw <= ZERO:
        return ZERO
    return round_half_even_to(raw, CENT_EXP)


def taker_rebate_fraction(prior_month_taker_volume: Decimal) -> Decimal:
    """Retrospective rebate fraction for a prior-month volume. Never applied to a quote."""
    for threshold, fraction in TAKER_REBATE_TIERS:
        if prior_month_taker_volume >= threshold:
            return fraction
    return ZERO
