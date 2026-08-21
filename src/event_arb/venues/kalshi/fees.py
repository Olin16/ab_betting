"""Kalshi fee model, including balance rounding.

Source: <https://docs.kalshi.com/getting_started/fee_rounding> and the Kalshi fee
schedule, read 2026-08-21.

The headline formula is::

    trade_fee = ceil(M * rate * C * P * (1 - P))

but "ceil" is doing more work than it appears. Per the fee-rounding page the venue:

1. rounds the trade fee **up to the nearest $0.0001** (a centicent);
2. computes ``balance_change = revenue - trade_fee`` (revenue is negative for a buy);
3. **floors** that toward negative infinity to the member's balance precision
   ($0.0001 for direct members, $0.01 otherwise);
4. charges the difference as a ``rounding_fee``;
5. accumulates rounding fees **per order across all fills** and rebates a whole cent once
   the accumulator exceeds $0.01.

Modelling only step 1 would understate cost on small or fractional fills, which is
precisely the size range where a thin arbitrage edge lives. The accumulator is stateful,
so a fee calculation is scoped to one order via :class:`KalshiOrderFeeAccumulator`.

The published worked example confirms the column relationship
``net fee = trade fee + rounding - rebate`` and that the accumulator carries rounding
across fills; :func:`balance_rounding_fee` is tested directly against one of those rows.
The fee *rate* used in the documentation's examples could not be reconstructed from the
published numbers, so the rate and multiplier here come from configuration only.

The rate and multiplier are **not** hard-coded: they arrive per market in a
:class:`~event_arb.domain.models.FeeConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from event_arb.domain.models import FeeConfig
from event_arb.domain.money import CENT_EXP, CENTICENT_EXP, ZERO, ceil_to, floor_to

__all__ = [
    "DEFAULT_KALSHI_TAKER_RATE",
    "FillFee",
    "KalshiOrderFeeAccumulator",
    "balance_rounding_fee",
    "default_kalshi_fee_config",
    "trade_fee",
]

#: Kalshi's published general coefficient. Per-series multipliers may differ; always
#: prefer a FeeConfig sourced from the live fee schedule.
DEFAULT_KALSHI_TAKER_RATE = Decimal("0.07")

REBATE_THRESHOLD = Decimal("0.01")


def default_kalshi_fee_config(source: str = "fee-schedule-2026-07 (assumed)") -> FeeConfig:
    from event_arb.domain.enums import Venue

    return FeeConfig(
        venue=Venue.KALSHI,
        taker_rate=DEFAULT_KALSHI_TAKER_RATE,
        maker_rate=DEFAULT_KALSHI_TAKER_RATE,
        multiplier=Decimal("1"),
        source=source,
    )


def trade_fee(quantity: Decimal, price: Decimal, config: FeeConfig, maker: bool = False) -> Decimal:
    """``ceil(M * rate * C * P * (1 - P))`` rounded up to the nearest centicent."""
    if quantity <= ZERO:
        return ZERO
    rate = config.maker_rate if maker else config.taker_rate
    raw = config.multiplier * rate * quantity * price * (Decimal("1") - price)
    return ceil_to(raw, CENTICENT_EXP)


def balance_rounding_fee(revenue: Decimal, fee: Decimal, balance_exp: Decimal) -> Decimal:
    """The rounding fee produced by flooring a balance change to the account grid.

    ``revenue`` is negative for a purchase. The venue computes
    ``balance_change = revenue - fee``, floors it toward negative infinity to the
    member's balance precision, and charges the difference::

        rounding_fee = balance_change - floor(balance_change)

    Because flooring is toward negative infinity, the result is always >= 0: the
    rounding always favours the venue.
    """
    balance_change = revenue - fee
    return balance_change - floor_to(balance_change, balance_exp)


@dataclass(frozen=True)
class FillFee:
    """Fee breakdown for one fill."""

    trade_fee: Decimal
    rounding_fee: Decimal
    rebate: Decimal

    @property
    def net(self) -> Decimal:
        """Never negative: a rebate can cancel the rounding fee but not pay us."""
        return max(ZERO, self.trade_fee + self.rounding_fee - self.rebate)


@dataclass
class KalshiOrderFeeAccumulator:
    """Per-order fee state. One instance per simulated order, not per market."""

    config: FeeConfig
    direct_member: bool = False
    _rounding_accumulator: Decimal = field(default=ZERO, init=False)

    @property
    def balance_exp(self) -> Decimal:
        return CENTICENT_EXP if self.direct_member else CENT_EXP

    def charge_fill(self, quantity: Decimal, price: Decimal, maker: bool = False) -> FillFee:
        """Compute the fee for one fill of ``quantity`` at ``price`` (a buy).

        ``revenue`` is negative for a purchase: we pay ``quantity * price``.
        """
        fee = trade_fee(quantity, price, self.config, maker=maker)
        revenue = -(quantity * price)
        rounding_fee = balance_rounding_fee(revenue, fee, self.balance_exp)

        self._rounding_accumulator += rounding_fee
        rebate = ZERO
        if self._rounding_accumulator > REBATE_THRESHOLD:
            rebate = REBATE_THRESHOLD
            self._rounding_accumulator -= REBATE_THRESHOLD

        return FillFee(trade_fee=fee, rounding_fee=rounding_fee, rebate=rebate)

    def total_for_fills(self, fills: list[tuple[Decimal, Decimal]], maker: bool = False) -> Decimal:
        """Total net fee across ``(quantity, price)`` fills of a single order."""
        return sum((self.charge_fill(q, p, maker=maker).net for q, p in fills), ZERO)
