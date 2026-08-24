"""Fee routing by explicit policy.

The sharpest risk is a silent one: Global's five-decimal ceiling and US banker's rounding
both return plausible numbers, so a mis-routed fee looks correct. These tests fail if the
two implementations are swapped.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from event_arb.domain.enums import FeePolicy, Venue
from event_arb.domain.models import FeeConfig
from event_arb.fees import UnknownFeePolicyError, taker_fee_for
from event_arb.venues.polymarket.fees import taker_fee as global_taker_fee
from event_arb.venues.polymarket_us.fees import polymarket_us_fee_config
from event_arb.venues.polymarket_us.fees import taker_fee as us_taker_fee


def config(policy: FeePolicy, rate: str = "0.06") -> FeeConfig:
    return FeeConfig(venue=Venue.POLYMARKET, taker_rate=Decimal(rate), fee_policy=policy)


def test_us_and_global_rounding_genuinely_differ() -> None:
    """If this ever stops holding, the swap tests below prove nothing."""
    cfg = config(FeePolicy.POLYMARKET_US_BANKERS_CENT)
    quantity, price = Decimal("3"), Decimal("0.50")
    assert us_taker_fee(quantity, price, cfg) != global_taker_fee(quantity, price, cfg)


def test_us_policy_routes_to_bankers_rounding() -> None:
    """0.06*3*0.5*0.5 = 0.045 -> 0.04 (half to even). A ceiling would give 0.05."""
    fee = taker_fee_for(Decimal("3"), Decimal("0.50"), config(FeePolicy.POLYMARKET_US_BANKERS_CENT))
    assert fee == Decimal("0.04")


def test_global_policy_routes_to_five_decimal_ceiling() -> None:
    """Same inputs, different policy: ceil to 5 dp gives 0.045 exactly."""
    fee = taker_fee_for(Decimal("3"), Decimal("0.50"), config(FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL))
    assert fee == Decimal("0.04500")


def test_swapping_the_policies_changes_the_answer() -> None:
    """The regression this whole mechanism exists to prevent."""
    quantity, price = Decimal("3"), Decimal("0.50")
    us = taker_fee_for(quantity, price, config(FeePolicy.POLYMARKET_US_BANKERS_CENT))
    glob = taker_fee_for(quantity, price, config(FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL))
    assert us != glob


def test_kalshi_policy_routes_to_centicent_ceiling() -> None:
    cfg = FeeConfig(
        venue=Venue.KALSHI,
        taker_rate=Decimal("0.07"),
        fee_policy=FeePolicy.KALSHI_CENTICENT_CEIL,
    )
    assert taker_fee_for(Decimal("100"), Decimal("0.50"), cfg) == Decimal("1.75")


def test_unknown_policy_fails_closed() -> None:
    """A market whose fee model we cannot identify must not be priced at all."""
    with pytest.raises(UnknownFeePolicyError, match="set an explicit FeePolicy"):
        taker_fee_for(Decimal("100"), Decimal("0.50"), config(FeePolicy.UNKNOWN))


def test_unknown_policy_error_names_the_source_for_debugging() -> None:
    cfg = FeeConfig(venue=Venue.POLYMARKET, taker_rate=Decimal("0.05"), source="some-config")
    with pytest.raises(UnknownFeePolicyError, match="some-config"):
        taker_fee_for(Decimal("1"), Decimal("0.5"), cfg)


def test_zero_quantity_is_free_under_every_policy() -> None:
    for policy in (
        FeePolicy.POLYMARKET_US_BANKERS_CENT,
        FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL,
        FeePolicy.KALSHI_CENTICENT_CEIL,
    ):
        assert taker_fee_for(Decimal("0"), Decimal("0.5"), config(policy)) == Decimal("0")


def test_us_fee_config_declares_its_policy() -> None:
    assert polymarket_us_fee_config().fee_policy is FeePolicy.POLYMARKET_US_BANKERS_CENT


def test_policy_is_not_inferred_from_venue() -> None:
    """Both Polymarket products share Venue.POLYMARKET, so venue cannot decide."""
    us = config(FeePolicy.POLYMARKET_US_BANKERS_CENT)
    glob = config(FeePolicy.POLYMARKET_GLOBAL_5DP_CEIL)
    assert us.venue is glob.venue
    assert us.fee_policy is not glob.fee_policy
