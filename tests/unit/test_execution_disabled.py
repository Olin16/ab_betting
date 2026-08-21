"""Live trading must be impossible in this build.

These tests are the enforcement mechanism for the project's central constraint. If any of
them fail, the build can place a real order and must not be run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from event_arb.config import Settings
from event_arb.domain.enums import (
    ArbDirection,
    MatchStatus,
    OpportunityLabel,
    Outcome,
    SettlementAsset,
    Venue,
)
from event_arb.domain.models import LegPlan, MarketPair, Opportunity
from event_arb.execution.base import OrderRequest
from event_arb.execution.controls import TradingControls, TradingLimits
from event_arb.execution.coordinator import ExecutionCoordinator
from event_arb.execution.disabled import DisabledExecutionAdapter, LiveTradingDisabledError


def make_opportunity(confirmed: bool = True) -> Opportunity:
    leg = LegPlan(
        venue=Venue.KALSHI,
        market_id="K1",
        outcome=Outcome.YES,
        quantity=Decimal("10"),
        vwap=Decimal("0.55"),
        worst_price=Decimal("0.55"),
        levels_consumed=1,
        gross_cost=Decimal("5.50"),
        fees=Decimal("0.10"),
        settlement_asset=SettlementAsset.USD,
        book_age_ms=10,
    )
    poly_leg = leg.model_copy(
        update={
            "venue": Venue.POLYMARKET,
            "market_id": "P1",
            "outcome": Outcome.NO,
            "vwap": Decimal("0.38"),
            "worst_price": Decimal("0.38"),
            "gross_cost": Decimal("3.80"),
            "settlement_asset": SettlementAsset.USDC,
        }
    )
    return Opportunity(
        pair_id="pair1",
        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
        label=OpportunityLabel.CONFIRMED if confirmed else OpportunityLabel.RESEARCH_CANDIDATE,
        requested_quantity=Decimal("10"),
        executable_quantity=Decimal("10"),
        kalshi_leg=leg,
        polymarket_leg=poly_leg,
        gross_acquisition_cost=Decimal("9.30"),
        kalshi_fees=Decimal("0.10"),
        polymarket_fees=Decimal("0"),
        slippage_allowance=Decimal("0"),
        operational_cost_allowance=Decimal("0"),
        usdc_conversion_allowance=Decimal("0"),
        expected_payout=Decimal("10.00"),
        net_profit=Decimal("0.60"),
        return_on_capital=Decimal("6"),
        gross_spread=Decimal("0.70"),
    )


def approved_pair() -> MarketPair:
    return MarketPair(
        id="pair1",
        kalshi_market_id="K1",
        polymarket_market_id="P1",
        status=MatchStatus.APPROVED,
        manually_approved=True,
    )


def order_request() -> OrderRequest:
    return OrderRequest(
        venue=Venue.KALSHI,
        market_id="K1",
        outcome=Outcome.YES,
        quantity=Decimal("10"),
        limit_price=Decimal("0.55"),
        client_order_id="cid-1",
    )


# ---------------------------------------------------------------- hard disablement


def test_trading_enabled_defaults_to_false() -> None:
    assert Settings().trading_enabled is False


def test_live_trading_is_impossible_even_when_the_flag_is_set() -> None:
    """Setting the flag must not create a live path; there is no implementation to enable."""
    settings = Settings(trading_enabled=True)
    assert settings.live_trading_possible is False


async def test_submit_always_raises() -> None:
    with pytest.raises(LiveTradingDisabledError):
        await DisabledExecutionAdapter().submit(order_request())


async def test_submit_raises_even_when_constructed_with_trading_enabled() -> None:
    with pytest.raises(LiveTradingDisabledError):
        await DisabledExecutionAdapter(trading_enabled=True).submit(order_request())


async def test_cancel_and_get_order_also_raise() -> None:
    adapter = DisabledExecutionAdapter()
    with pytest.raises(LiveTradingDisabledError):
        await adapter.cancel(Venue.KALSHI, "order-1")
    with pytest.raises(LiveTradingDisabledError):
        await adapter.get_order(Venue.KALSHI, "cid-1")


async def test_coordinator_refuses_even_with_every_control_satisfied() -> None:
    """The last line of defence: controls passing is still not enough to trade."""
    controls = TradingControls(
        trading_enabled=True,
        startup_confirmed=True,
        geoblock_allowed=True,
        manual_approval_required=False,
        balances={SettlementAsset.USD: Decimal("10000"), SettlementAsset.USDC: Decimal("10000")},
    )
    coordinator = ExecutionCoordinator(controls=controls)
    assert coordinator.would_allow(make_opportunity(), approved_pair()).allowed is True

    with pytest.raises(LiveTradingDisabledError):
        await coordinator.execute(make_opportunity(), approved_pair())


def test_coordinator_defaults_to_the_disabled_adapter() -> None:
    assert isinstance(ExecutionCoordinator().adapter, DisabledExecutionAdapter)


def test_no_market_data_adapter_exposes_an_order_method() -> None:
    """Read-only adapters must have no submission surface at all."""
    from event_arb.venues.base import VenueAdapter

    forbidden = {"submit", "submit_order", "place_order", "create_order", "buy", "sell"}
    assert not (forbidden & set(dir(VenueAdapter)))


# --------------------------------------------------------------- control coverage


def _permissive_controls(**overrides: object) -> TradingControls:
    base: dict[str, object] = {
        "trading_enabled": True,
        "startup_confirmed": True,
        "geoblock_allowed": True,
        "manual_approval_required": False,
        "balances": {
            SettlementAsset.USD: Decimal("10000"),
            SettlementAsset.USDC: Decimal("10000"),
        },
    }
    return TradingControls(**{**base, **overrides})  # type: ignore[arg-type]


def test_geoblock_failure_denies_the_trade() -> None:
    decision = _permissive_controls(geoblock_allowed=False).evaluate(
        make_opportunity(), approved_pair()
    )
    assert decision.allowed is False
    assert "geographic" in decision.denied_reason


def test_trading_disabled_denies_the_trade() -> None:
    decision = _permissive_controls(trading_enabled=False).evaluate(
        make_opportunity(), approved_pair()
    )
    assert "TradingEnabled is false" in decision.denied_reason


def test_unapproved_pair_denies_the_trade() -> None:
    unapproved = MarketPair(id="p", kalshi_market_id="K1", polymarket_market_id="P1")
    decision = _permissive_controls().evaluate(make_opportunity(confirmed=False), unapproved)
    assert decision.allowed is False
    assert "not manually approved" in decision.denied_reason


def test_kill_switch_denies_everything() -> None:
    controls = _permissive_controls()
    controls.engage_kill_switch("operator intervention")
    assert controls.evaluate(make_opportunity(), approved_pair()).allowed is False


def test_circuit_breaker_trips_after_consecutive_failures() -> None:
    controls = _permissive_controls()
    for _ in range(3):
        controls.record_failure()
    assert controls.circuit_breaker_tripped is True
    assert controls.evaluate(make_opportunity(), approved_pair()).allowed is False


def test_circuit_breaker_resets_on_success() -> None:
    controls = _permissive_controls()
    controls.record_failure()
    controls.record_success()
    assert controls.consecutive_failures == 0
    assert controls.circuit_breaker_tripped is False


def test_order_size_limit_is_enforced() -> None:
    controls = _permissive_controls(limits=TradingLimits(max_order_quantity=Decimal("5")))
    assert controls.evaluate(make_opportunity(), approved_pair()).allowed is False


def test_daily_loss_limit_is_enforced() -> None:
    controls = _permissive_controls(limits=TradingLimits(daily_loss_limit=Decimal("10")))
    controls.record_loss(Decimal("10"))
    decision = controls.evaluate(make_opportunity(), approved_pair())
    assert "daily loss" in decision.denied_reason


def test_daily_loss_resets_on_a_new_day() -> None:
    controls = _permissive_controls(limits=TradingLimits(daily_loss_limit=Decimal("10")))
    controls.record_loss(Decimal("10"), when=datetime(2026, 8, 20, 12, tzinfo=UTC))
    controls.record_loss(Decimal("1"), when=datetime(2026, 8, 21, 12, tzinfo=UTC))
    assert controls.realized_loss_today == Decimal("1")


def test_minimum_balance_is_enforced() -> None:
    controls = _permissive_controls(
        balances={SettlementAsset.USD: Decimal("5"), SettlementAsset.USDC: Decimal("10000")},
        limits=TradingLimits(min_balance=Decimal("100")),
    )
    assert "USD balance" in controls.evaluate(make_opportunity(), approved_pair()).denied_reason


def test_existing_unhedged_exposure_blocks_new_trades() -> None:
    controls = _permissive_controls(open_unhedged_quantity=Decimal("25"))
    decision = controls.evaluate(make_opportunity(), approved_pair())
    assert "unhedged exposure" in decision.denied_reason


def test_per_trade_manual_approval_is_required_by_default() -> None:
    controls = _permissive_controls(manual_approval_required=True)
    assert controls.evaluate(make_opportunity(), approved_pair()).allowed is False
    assert (
        controls.evaluate(make_opportunity(), approved_pair(), manually_approved_trade=True).allowed
        is True
    )


def test_total_exposure_limit_is_enforced() -> None:
    controls = _permissive_controls(
        open_exposure=Decimal("1995"), limits=TradingLimits(max_total_exposure=Decimal("2000"))
    )
    assert "total exposure" in controls.evaluate(make_opportunity(), approved_pair()).denied_reason
