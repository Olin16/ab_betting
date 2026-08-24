"""Fee dispatch by explicit policy.

Three venues round incompatibly, and two of them share a :class:`~event_arb.domain.enums.Venue`
value:

============================== ==========================================================
Policy                         Rounding
============================== ==========================================================
``KALSHI_CENTICENT_CEIL``      ceil to $0.0001, then a per-order balance-floor accumulator
``POLYMARKET_GLOBAL_5DP_CEIL`` ceil to 5 dp, minimum charge 0.00001 USDC
``POLYMARKET_US_BANKERS_CENT`` round-half-to-even to $0.01
============================== ==========================================================

Dispatch is on :attr:`~event_arb.domain.models.FeeConfig.fee_policy`, never on ``venue``.
Inferring from the venue would route Polymarket US markets through Global's five-decimal
ceiling, which overstates fees at the half-cent boundary — and, more importantly, would be
a silent error: both produce a plausible-looking number.

``UNKNOWN`` refuses rather than guessing. A conservative default was considered and
rejected: "charge the highest known policy" hides a configuration mistake behind a number
that looks deliberate, whereas a refusal surfaces it at the moment the market is priced.
"""

from __future__ import annotations

from decimal import Decimal

from event_arb.domain.enums import FeePolicy
from event_arb.domain.models import FeeConfig
from event_arb.domain.money import CENT_EXP, ZERO, ceil_to

__all__ = ["UnknownFeePolicyError", "taker_fee_for"]


class UnknownFeePolicyError(ValueError):
    """Raised when a market carries no usable fee policy.

    Fails closed: a market whose fee model we cannot identify must not be priced.
    """

    def __init__(self, config: FeeConfig) -> None:
        super().__init__(
            f"cannot price {config.venue.value} market with fee_policy="
            f"{config.fee_policy.value!r} (source={config.source!r}); "
            "set an explicit FeePolicy rather than relying on the venue"
        )


def taker_fee_for(quantity: Decimal, price: Decimal, config: FeeConfig) -> Decimal:
    """Taker fee for one fill, routed by the market's declared policy.

    Kalshi's per-order rounding accumulator is *not* modelled here: it is stateful across
    fills of one order, so callers that walk multiple levels must use
    :class:`~event_arb.venues.kalshi.fees.KalshiOrderFeeAccumulator` directly. This
    function covers the single-fill case and the two stateless Polymarket policies.
    """
    if quantity <= ZERO:
        return ZERO

    policy = config.fee_policy
    if policy is FeePolicy.POLYMARKET_US_BANKERS_CENT:
        from event_arb.venues.polymarket_us.fees import taker_fee as us_taker_fee

        return us_taker_fee(quantity, price, config)

    if policy is FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL:
        from event_arb.venues.polymarket.fees import taker_fee as global_taker_fee

        return global_taker_fee(quantity, price, config)

    if policy is FeePolicy.KALSHI_CENTICENT_CEIL:
        from event_arb.venues.kalshi.fees import trade_fee

        return ceil_to(trade_fee(quantity, price, config), CENT_EXP)

    raise UnknownFeePolicyError(config)
