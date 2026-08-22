"""Fee models and rounding at boundary values.

Fee rounding is where a "profitable" arbitrage quietly turns into a loss, so these tests
pin the exact arithmetic rather than approximate it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from event_arb.domain.enums import Venue
from event_arb.domain.models import FeeConfig
from event_arb.domain.money import CENT_EXP
from event_arb.venues.kalshi.fees import (
    KalshiOrderFeeAccumulator,
    balance_rounding_fee,
    default_kalshi_fee_config,
    trade_fee,
)
from event_arb.venues.polymarket.fees import (
    polymarket_fee_config,
    polymarket_fee_config_from_market,
    taker_fee,
)

KALSHI = default_kalshi_fee_config()


# --------------------------------------------------------------------------- Kalshi


def test_kalshi_fee_at_maximum_risk_price() -> None:
    """P*(1-P) peaks at P=0.50, giving the documented $1.75 per 100 contracts."""
    assert trade_fee(Decimal("100"), Decimal("0.50"), KALSHI) == Decimal("1.7500")


def test_kalshi_fee_decays_toward_the_extremes() -> None:
    """Documented: $0.63 per 100 contracts at P=0.10 (0.07*100*0.1*0.9 = 0.63)."""
    assert trade_fee(Decimal("100"), Decimal("0.10"), KALSHI) == Decimal("0.6300")


def test_kalshi_fee_rounds_up_to_centicent_never_down() -> None:
    """0.07 * 1 * 0.33 * 0.67 = 0.0154770 -> ceils to 0.0155."""
    assert trade_fee(Decimal("1"), Decimal("0.33"), KALSHI) == Decimal("0.0155")


def test_kalshi_fee_zero_for_zero_quantity() -> None:
    assert trade_fee(Decimal("0"), Decimal("0.5"), KALSHI) == Decimal("0")


def test_kalshi_multiplier_scales_the_fee() -> None:
    doubled = FeeConfig(
        venue=Venue.KALSHI, taker_rate=Decimal("0.07"), multiplier=Decimal("2"), source="test"
    )
    assert trade_fee(Decimal("100"), Decimal("0.50"), doubled) == Decimal("3.5000")


def test_balance_rounding_matches_published_worked_example_row() -> None:
    """Reproduces a real row from Kalshi's fee-rounding worked example.

    Published row (Fill 1): trade fee $0.0085, rounding $0.0065, accumulator $0.0065,
    no rebate, net fee $0.0150, balance change -$0.07. Working backwards, the revenue
    on that fill was -$0.055.
    """
    revenue, fee = Decimal("-0.055"), Decimal("0.0085")
    rounding = balance_rounding_fee(revenue, fee, CENT_EXP)
    assert rounding == Decimal("0.0065")
    assert fee + rounding == Decimal("0.0150")
    assert floor_to_cent(revenue - fee) == Decimal("-0.07")


def floor_to_cent(value: Decimal) -> Decimal:
    from event_arb.domain.money import floor_to

    return floor_to(value, CENT_EXP)


def test_rounding_fee_always_favours_the_venue() -> None:
    """Flooring toward -inf means the rounding fee can never be negative."""
    for revenue in ("-0.055", "-0.1500", "-1.0", "-0.0001"):
        assert balance_rounding_fee(Decimal(revenue), Decimal("0.0085"), CENT_EXP) >= 0


def test_kalshi_rounding_accumulator_rebates_a_whole_cent() -> None:
    """Once accumulated rounding exceeds $0.01, a whole-cent rebate is issued."""
    acc = KalshiOrderFeeAccumulator(config=KALSHI, direct_member=False)
    rebates = [acc.charge_fill(Decimal("0.30"), Decimal("0.50")).rebate for _ in range(4)]
    assert Decimal("0.01") in rebates, "accumulator never rebated across four fills"


def test_accumulator_carries_rounding_across_fills() -> None:
    """A fresh accumulator must not inherit another order's rounding balance."""
    shared = KalshiOrderFeeAccumulator(config=KALSHI)
    first = shared.charge_fill(Decimal("0.30"), Decimal("0.50"))
    fresh = KalshiOrderFeeAccumulator(config=KALSHI)
    assert fresh.charge_fill(Decimal("0.30"), Decimal("0.50")).rounding_fee == first.rounding_fee


def test_kalshi_net_fee_never_negative() -> None:
    """A rebate may cancel a fee; it must never pay us to trade."""
    acc = KalshiOrderFeeAccumulator(config=KALSHI)
    for _ in range(10):
        assert acc.charge_fill(Decimal("0.30"), Decimal("0.50")).net >= Decimal("0")


def test_direct_member_uses_finer_balance_precision() -> None:
    """Direct members floor to $0.0001, so they incur far less rounding fee."""
    direct = KalshiOrderFeeAccumulator(config=KALSHI, direct_member=True)
    non_direct = KalshiOrderFeeAccumulator(config=KALSHI, direct_member=False)
    q, p = Decimal("0.30"), Decimal("0.50")
    assert direct.charge_fill(q, p).rounding_fee < non_direct.charge_fill(q, p).rounding_fee


def test_multi_level_order_accumulates_fees_across_fills() -> None:
    acc = KalshiOrderFeeAccumulator(config=KALSHI)
    total = acc.total_for_fills(
        [(Decimal("50"), Decimal("0.55")), (Decimal("50"), Decimal("0.56"))]
    )
    assert total > Decimal("0")


# ----------------------------------------------------------------------- Polymarket


def test_polymarket_taker_fee_formula() -> None:
    """C * rate * p * (1-p): 100 * 0.04 * 0.38 * 0.62 = 0.9424."""
    config = polymarket_fee_config("finance")
    assert taker_fee(Decimal("100"), Decimal("0.38"), config) == Decimal("0.94240")


def test_polymarket_fee_free_categories_charge_nothing() -> None:
    config = polymarket_fee_config("geopolitical")
    assert taker_fee(Decimal("1000"), Decimal("0.5"), config) == Decimal("0")


def test_polymarket_minimum_charge_applies_to_tiny_fees() -> None:
    """Documented minimum is 0.00001 USDC on any non-zero fee."""
    config = polymarket_fee_config("finance")
    fee = taker_fee(Decimal("0.0001"), Decimal("0.5"), config)
    assert fee == Decimal("0.00001")


def test_polymarket_maker_rate_is_zero() -> None:
    """Documented: makers are never charged."""
    assert polymarket_fee_config("crypto").maker_rate == Decimal("0")


def test_polymarket_unknown_category_is_conservative() -> None:
    assert polymarket_fee_config(None).taker_rate == Decimal("0.07")


@pytest.mark.parametrize(
    ("category", "rate"),
    [
        ("crypto", "0.07"),
        ("sports", "0.05"),
        ("economics", "0.05"),
        ("culture", "0.05"),
        ("weather", "0.05"),
        ("general", "0.05"),
        ("finance", "0.04"),
        ("politics", "0.04"),
        ("mentions", "0.04"),
        ("tech", "0.04"),
        ("geopolitics", "0"),
        ("world", "0"),
    ],
)
def test_polymarket_documented_category_rates(category: str, rate: str) -> None:
    """Known categories must not fall through to the conservative 0.07 default."""
    assert polymarket_fee_config(category).taker_rate == Decimal(rate)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("geopolitical", "geopolitics"),
        ("technology", "tech"),
        ("economic", "economics"),
        ("world events", "world"),
    ],
)
def test_polymarket_category_aliases(alias: str, canonical: str) -> None:
    assert polymarket_fee_config(alias).taker_rate == polymarket_fee_config(canonical).taker_rate


# ------------------------------------------------------------------------ properties


@given(
    quantity=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000"), places=2),
    price=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("0.99"), places=2),
)
def test_fees_are_never_negative_and_never_exceed_notional(
    quantity: Decimal, price: Decimal
) -> None:
    """A fee larger than the trade's notional would signal a modelling error."""
    kalshi = trade_fee(quantity, price, KALSHI)
    poly = taker_fee(quantity, price, polymarket_fee_config("crypto"))
    notional = quantity * price
    assert Decimal("0") <= kalshi
    assert Decimal("0") <= poly
    assert kalshi <= notional + Decimal("0.0001")
    assert poly <= notional + Decimal("0.00001")


@pytest.mark.parametrize("price", ["0.01", "0.50", "0.99"])
def test_fee_symmetric_around_one_half(price: str) -> None:
    """P*(1-P) is symmetric, so fees at p and 1-p must match."""
    p = Decimal(price)
    assert trade_fee(Decimal("100"), p, KALSHI) == trade_fee(
        Decimal("100"), Decimal("1") - p, KALSHI
    )


# ------------------------------------- per-market Polymarket fee configuration


def test_per_market_fee_schedule_overrides_the_category_table() -> None:
    """Regression for CODEX_REVIEW P2.

    The market object carries feeSchedule.rate. A category lookup is a fallback for
    when that is absent, not the primary source: hard-coded category rates go stale
    silently, and the spec requires querying per-market fee settings.
    """
    config = polymarket_fee_config_from_market(
        {"category": "crypto", "feesEnabled": True, "feeSchedule": {"rate": "0.02"}}
    )
    assert config.taker_rate == Decimal("0.02")
    assert "feeSchedule" in config.source


def test_fees_disabled_on_a_market_means_zero() -> None:
    config = polymarket_fee_config_from_market(
        {"category": "crypto", "feesEnabled": False, "feeSchedule": {"rate": "0.07"}}
    )
    assert config.taker_rate == Decimal("0")
    assert taker_fee(Decimal("100"), Decimal("0.5"), config) == Decimal("0")


def test_missing_fee_schedule_falls_back_to_the_category_rate() -> None:
    config = polymarket_fee_config_from_market({"category": "sports"})
    assert config.taker_rate == Decimal("0.05")


def test_unparseable_fee_schedule_falls_back_conservatively() -> None:
    """A malformed rate must not be read as cheap; overstating fees is the safe error."""
    config = polymarket_fee_config_from_market(
        {"category": "not-a-real-category", "feeSchedule": {"rate": "banana"}}
    )
    assert config.taker_rate == Decimal("0.07")


def test_makers_are_never_charged_regardless_of_source() -> None:
    config = polymarket_fee_config_from_market(
        {"category": "sports", "feesEnabled": True, "feeSchedule": {"rate": "0.05"}}
    )
    assert config.maker_rate == Decimal("0")
