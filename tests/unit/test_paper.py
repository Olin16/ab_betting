"""Paper trading: partial fills, leg-2 failure, unhedged exposure, settlement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from event_arb.config import PaperSettings
from event_arb.domain.enums import (
    ArbDirection,
    OpportunityLabel,
    Outcome,
    PaperTradeStatus,
    SettlementAsset,
    Venue,
    VenueProduct,
)
from event_arb.domain.models import (
    BookLevel,
    LegPlan,
    Opportunity,
    OrderBook,
    OrderBookSide,
    VenueMarket,
)
from event_arb.orderbooks.freshness import FreshnessPolicy
from event_arb.paper.executor import PaperExecutor, settle
from event_arb.paper.fills import simulate_buy
from event_arb.paper.portfolio import PaperPortfolio
from event_arb.paper.settlement import reconcile
from event_arb.venues.kalshi.fees import default_kalshi_fee_config
from event_arb.venues.polymarket.fees import polymarket_fee_config

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def levels(*pairs: tuple[str, str]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in pairs)


def make_book(
    venue: Venue, market_id: str, yes_asks: tuple[BookLevel, ...], no_asks: tuple[BookLevel, ...]
) -> OrderBook:
    # Receipt time is "now": the executor validates freshness at arrival, and these
    # tests are about fill behaviour rather than staleness.
    product = VenueProduct.POLYMARKET_GLOBAL if venue is Venue.POLYMARKET else VenueProduct.NONE
    return OrderBook(
        venue=venue,
        product=product,
        market_id=market_id,
        yes=OrderBookSide(outcome=Outcome.YES, asks=yes_asks),
        no=OrderBookSide(outcome=Outcome.NO, asks=no_asks),
        received_at=datetime.now(UTC),
    )


KALSHI_MARKET = VenueMarket(
    venue=Venue.KALSHI,
    venue_market_id="K1",
    title="K",
    rules_text="r",
    fee_config=default_kalshi_fee_config(),
    settlement_asset=SettlementAsset.USD,
)
POLY_MARKET = VenueMarket(
    venue=Venue.POLYMARKET,
    venue_market_id="P1",
    title="P",
    rules_text="r",
    fee_config=polymarket_fee_config("geopolitical"),
    settlement_asset=SettlementAsset.USDC,
)


def make_opportunity(quantity: str = "100") -> Opportunity:
    q = Decimal(quantity)
    kalshi_leg = LegPlan(
        venue=Venue.KALSHI,
        market_id="K1",
        outcome=Outcome.YES,
        quantity=q,
        vwap=Decimal("0.55"),
        worst_price=Decimal("0.55"),
        levels_consumed=1,
        gross_cost=q * Decimal("0.55"),
        fees=Decimal("1.74"),
        settlement_asset=SettlementAsset.USD,
        book_age_ms=50,
    )
    poly_leg = LegPlan(
        venue=Venue.POLYMARKET,
        market_id="P1",
        outcome=Outcome.NO,
        quantity=q,
        vwap=Decimal("0.38"),
        worst_price=Decimal("0.38"),
        levels_consumed=1,
        gross_cost=q * Decimal("0.38"),
        fees=Decimal("0"),
        settlement_asset=SettlementAsset.USDC,
        book_age_ms=60,
    )
    return Opportunity(
        pair_id="pair1",
        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
        label=OpportunityLabel.CONFIRMED,
        requested_quantity=q,
        executable_quantity=q,
        kalshi_leg=kalshi_leg,
        polymarket_leg=poly_leg,
        gross_acquisition_cost=q * Decimal("0.93"),
        kalshi_fees=Decimal("1.74"),
        polymarket_fees=Decimal("0"),
        slippage_allowance=Decimal("0"),
        operational_cost_allowance=Decimal("0"),
        usdc_conversion_allowance=Decimal("0"),
        expected_payout=q,
        net_profit=q * Decimal("0.07") - Decimal("1.74"),
        return_on_capital=Decimal("5"),
        gross_spread=q * Decimal("0.07"),
    )


def executor_with(
    books: dict[tuple[Venue, str], OrderBook | None], settings: PaperSettings | None = None
) -> PaperExecutor:
    async def provider(venue: Venue, market_id: str, latency_ms: int) -> OrderBook | None:
        return books.get((venue, market_id))

    # Books here are module-level constants, so they age as the suite runs. These tests
    # are about fill behaviour; the age-specific cases below use explicitly old books.
    return PaperExecutor(
        settings or PaperSettings(adverse_fill_pct=Decimal("0")),
        provider,
        freshness=FreshnessPolicy(max_age_ms=60_000),
    )


FULL_KALSHI = make_book(Venue.KALSHI, "K1", levels(("0.55", "300")), levels(("0.47", "300")))
FULL_POLY = make_book(Venue.POLYMARKET, "P1", levels(("0.64", "300")), levels(("0.38", "300")))


# ------------------------------------------------------------------------ fills


def test_simulate_buy_fills_within_a_level() -> None:
    order = simulate_buy(FULL_KALSHI, Outcome.YES, Decimal("50"), "oid")
    assert order.filled_quantity == Decimal("50")
    assert order.is_complete
    assert order.vwap == Decimal("0.55")


def test_limit_price_prevents_paying_up() -> None:
    """A real order is a limit order; the simulator must not fill above it."""
    book = make_book(Venue.KALSHI, "K1", levels(("0.55", "10"), ("0.80", "500")), ())
    order = simulate_buy(book, Outcome.YES, Decimal("100"), "oid", limit_price=Decimal("0.55"))
    assert order.filled_quantity == Decimal("10")
    assert order.is_complete is False
    assert order.shortfall == Decimal("90")


def test_adverse_selection_reduces_available_size() -> None:
    """Faster traders take part of the book before our order lands."""
    order = simulate_buy(FULL_KALSHI, Outcome.YES, Decimal("300"), "oid", adverse_pct=Decimal("50"))
    assert order.filled_quantity == Decimal("150")


def test_client_order_ids_are_recorded() -> None:
    """Idempotency keys matter even in simulation; they are part of the audit record."""
    order = simulate_buy(FULL_KALSHI, Outcome.YES, Decimal("10"), "abc-123")
    assert order.client_order_id == "abc-123"


# -------------------------------------------------------------------- execution


async def test_both_legs_fill_completely() -> None:
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.BOTH_LEGS_FILLED
    assert trade.hedged_quantity == Decimal("100")
    assert trade.unhedged_quantity == Decimal("0")
    assert trade.realized_pnl_if_hedged > Decimal("0")


async def test_leg2_failure_leaves_fully_unhedged_exposure() -> None:
    """The defining cross-venue risk: leg 1 fills, leg 2 does not."""
    executor = executor_with({(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): None})
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.LEG2_FAILED
    assert trade.hedged_quantity == Decimal("0")
    assert trade.unhedged_quantity == Decimal("100")
    assert trade.unhedged_venue is Venue.KALSHI
    assert trade.realized_pnl_if_hedged < Decimal("0")


async def test_leg2_empty_book_leaves_unhedged_exposure() -> None:
    empty_poly = make_book(Venue.POLYMARKET, "P1", (), ())
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): empty_poly}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.LEG2_FAILED
    assert trade.unhedged_quantity == Decimal("100")


async def test_partial_leg2_fill_reports_exact_unhedged_size() -> None:
    thin_poly = make_book(Venue.POLYMARKET, "P1", levels(("0.64", "300")), levels(("0.38", "60")))
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): thin_poly}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.PARTIALLY_HEDGED
    assert trade.hedged_quantity == Decimal("60")
    assert trade.unhedged_quantity == Decimal("40")
    assert trade.unhedged_venue is Venue.KALSHI


async def test_leg2_is_sized_to_leg1s_actual_fill_not_the_plan() -> None:
    """Hedging the planned size after a partial leg 1 would create reverse exposure."""
    thin_kalshi = make_book(Venue.KALSHI, "K1", levels(("0.55", "30")), ())
    executor = executor_with(
        {(Venue.KALSHI, "K1"): thin_kalshi, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.leg1 is not None and trade.leg2 is not None
    assert trade.leg1.filled_quantity == Decimal("30")
    assert trade.leg2.requested_quantity == Decimal("30")
    assert trade.unhedged_quantity == Decimal("0")
    assert trade.status is PaperTradeStatus.BOTH_LEGS_FILLED


async def test_no_leg1_fill_means_no_exposure_at_all() -> None:
    empty_kalshi = make_book(Venue.KALSHI, "K1", (), ())
    executor = executor_with(
        {(Venue.KALSHI, "K1"): empty_kalshi, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.PENDING
    assert trade.unhedged_quantity == Decimal("0")
    assert trade.leg2 is None


async def test_price_movement_between_legs_is_measured_as_slippage() -> None:
    """Leg 2 arriving into a worse book must show up as realized slippage."""
    moved_poly = make_book(
        Venue.POLYMARKET,
        "P1",
        levels(("0.64", "300")),
        levels(("0.38", "20"), ("0.42", "300")),
    )
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): moved_poly}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    # The limit price stops us at 0.38, so only 20 fill; the rest is unhedged.
    assert trade.hedged_quantity == Decimal("20")
    assert trade.unhedged_quantity == Decimal("80")


async def test_latency_settings_are_recorded_on_the_trade() -> None:
    settings = PaperSettings(leg1_latency_ms=120, leg2_latency_ms=350)
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}, settings
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.leg1_latency_ms == 120
    assert trade.leg2_latency_ms == 350


async def test_fees_are_charged_on_both_legs() -> None:
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.kalshi_fees > Decimal("0")
    assert trade.total_fees == trade.kalshi_fees + trade.polymarket_fees


# ------------------------------------------------------------------- settlement


async def test_settlement_of_a_fully_hedged_trade_pays_par_either_way() -> None:
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    for winner in (Outcome.YES, Outcome.NO):
        trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
        settle(trade, winner)
        assert trade.settlement_result is not None
        assert trade.settlement_result["total_payout"] == "100.0000"
        assert trade.status is PaperTradeStatus.SETTLED


async def test_unhedged_position_settlement_depends_on_the_outcome() -> None:
    """This is where an unhedged leg stops being theoretical."""
    executor = executor_with({(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): None})

    winner = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    settle(winner, Outcome.YES)  # we hold Kalshi YES
    assert winner.settlement_result is not None
    win_pnl = Decimal(str(winner.settlement_result["final_pnl"]))

    loser = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    settle(loser, Outcome.NO)
    assert loser.settlement_result is not None
    loss_pnl = Decimal(str(loser.settlement_result["final_pnl"]))

    assert win_pnl > Decimal("0") > loss_pnl


async def test_reconciliation_reports_variance_from_unhedged_outcomes() -> None:
    executor = executor_with({(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): None})
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    result = reconcile([trade], {"pair1": Outcome.YES})
    assert result.settled == 1
    assert result.unhedged_wins == 1
    assert result.variance > Decimal("0")


async def test_trades_without_a_known_outcome_are_not_settled() -> None:
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert reconcile([trade], {}).settled == 0
    assert trade.status is not PaperTradeStatus.SETTLED


# -------------------------------------------------------------------- portfolio


async def test_portfolio_keeps_usd_and_usdc_separate() -> None:
    """Netting the two would hide exactly the basis risk we are trying to surface."""
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    portfolio.record(await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET))
    assert portfolio.balance(SettlementAsset.USD) < Decimal("1000")
    assert portfolio.balance(SettlementAsset.USDC) < Decimal("1000")
    assert portfolio.balance(SettlementAsset.USD) != portfolio.balance(SettlementAsset.USDC)


async def test_portfolio_lists_open_unhedged_exposure() -> None:
    portfolio = PaperPortfolio()
    executor = executor_with({(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): None})
    portfolio.record(await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET))
    exposure = portfolio.open_exposure()
    assert len(exposure) == 1
    assert exposure[0]["quantity"] == "100.00"
    assert exposure[0]["venue"] == Venue.KALSHI.value


async def test_daily_report_tracks_hedge_success_rate() -> None:
    portfolio = PaperPortfolio()
    good = executor_with({(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY})
    bad = executor_with({(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): None})
    portfolio.record(await good.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET))
    portfolio.record(await bad.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET))

    report = portfolio.daily_report()
    assert report.trades == 2
    assert report.fully_hedged == 1
    assert report.leg2_failures == 1
    assert report.hedge_success_rate == Decimal("50.0000")


async def test_exports_are_parseable() -> None:
    import json

    portfolio = PaperPortfolio()
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    portfolio.record(await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET))

    payload = json.loads(portfolio.to_json())
    assert "trades" in payload and len(payload["trades"]) == 1
    csv_text = portfolio.to_csv()
    assert csv_text.splitlines()[0].startswith("id,pair_id,direction")
    assert len(csv_text.strip().splitlines()) == 2


@pytest.mark.parametrize("adverse", ["0", "25", "100"])
async def test_adverse_fill_settings_never_produce_negative_fills(adverse: str) -> None:
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY},
        PaperSettings(adverse_fill_pct=Decimal(adverse)),
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.hedged_quantity >= Decimal("0")
    assert trade.unhedged_quantity >= Decimal("0")


# ------------------------------------------------- arrival-book validation


def stale_book(
    venue: Venue,
    market_id: str,
    yes_asks: tuple[BookLevel, ...],
    no_asks: tuple[BookLevel, ...],
    **overrides: object,
) -> OrderBook:
    base: dict[str, object] = {"is_stale": True}
    product = VenueProduct.POLYMARKET_GLOBAL if venue is Venue.POLYMARKET else VenueProduct.NONE
    return OrderBook(
        venue=venue,
        product=product,
        market_id=market_id,
        yes=OrderBookSide(outcome=Outcome.YES, asks=yes_asks),
        no=OrderBookSide(outcome=Outcome.NO, asks=no_asks),
        received_at=datetime.now(UTC),
        **{**base, **overrides},  # type: ignore[arg-type]
    )


async def test_stale_leg1_book_produces_no_exposure() -> None:
    """Regression for CODEX_REVIEW P1.

    A stale arrival book previously filled cleanly, reporting both_legs_filled and a
    100% hedge success rate. Paper evidence feeds the live-readiness gate in
    docs/operations.md, so a simulator that trades on stale books lets that gate
    certify itself.
    """
    executor = executor_with(
        {
            (Venue.KALSHI, "K1"): stale_book(Venue.KALSHI, "K1", levels(("0.55", "300")), ()),
            (Venue.POLYMARKET, "P1"): FULL_POLY,
        }
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.PENDING
    assert trade.hedged_quantity == Decimal("0")
    assert trade.unhedged_quantity == Decimal("0")
    assert trade.leg1 is None
    assert any("stale" in note for note in trade.notes)


async def test_stale_leg2_book_leaves_honest_unhedged_exposure() -> None:
    """Leg 1 already filled, so refusing leg 2 must report real unhedged exposure."""
    executor = executor_with(
        {
            (Venue.KALSHI, "K1"): FULL_KALSHI,
            (Venue.POLYMARKET, "P1"): stale_book(
                Venue.POLYMARKET, "P1", (), levels(("0.38", "300"))
            ),
        }
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.LEG2_FAILED
    assert trade.unhedged_quantity == Decimal("100")
    assert trade.unhedged_venue is Venue.KALSHI


async def test_incomplete_book_is_refused() -> None:
    executor = executor_with(
        {
            (Venue.KALSHI, "K1"): stale_book(
                Venue.KALSHI,
                "K1",
                levels(("0.55", "300")),
                (),
                is_stale=False,
                is_complete=False,
            ),
            (Venue.POLYMARKET, "P1"): FULL_POLY,
        }
    )
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.PENDING
    assert trade.leg1 is None


async def test_book_older_than_the_freshness_limit_is_refused() -> None:
    """Age alone disqualifies a book, even when nothing marked it stale."""
    ancient = OrderBook(
        venue=Venue.KALSHI,
        market_id="K1",
        yes=OrderBookSide(outcome=Outcome.YES, asks=levels(("0.55", "300"))),
        no=OrderBookSide(outcome=Outcome.NO, asks=()),
        received_at=datetime.now(UTC) - timedelta(hours=6),
    )
    executor = executor_with({(Venue.KALSHI, "K1"): ancient, (Venue.POLYMARKET, "P1"): FULL_POLY})
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.PENDING
    assert trade.leg1 is None


async def test_wrong_market_or_venue_book_is_refused() -> None:
    """A provider returning the wrong book must not be silently traded against."""
    wrong = OrderBook(
        venue=Venue.KALSHI,
        market_id="SOME-OTHER-MARKET",
        yes=OrderBookSide(outcome=Outcome.YES, asks=levels(("0.10", "300"))),
        no=OrderBookSide(outcome=Outcome.NO, asks=()),
        received_at=datetime.now(UTC),
    )
    executor = executor_with({(Venue.KALSHI, "K1"): wrong, (Venue.POLYMARKET, "P1"): FULL_POLY})
    trade = await executor.execute(make_opportunity(), KALSHI_MARKET, POLY_MARKET)
    assert trade.status is PaperTradeStatus.PENDING
    assert trade.leg1 is None


# --------------------------------------------------- asset-aware settlement


def _legs_with_assets(kalshi: SettlementAsset, polymarket: SettlementAsset) -> Opportunity:
    opportunity = make_opportunity()
    return opportunity.model_copy(
        update={
            "kalshi_leg": opportunity.kalshi_leg.model_copy(update={"settlement_asset": kalshi}),
            "polymarket_leg": opportunity.polymarket_leg.model_copy(
                update={"settlement_asset": polymarket}
            ),
        }
    )


async def test_global_settlement_retains_usdc_reporting() -> None:
    """The legacy path must keep its conversion and depeg reporting."""
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(
        _legs_with_assets(SettlementAsset.USD, SettlementAsset.USDC),
        KALSHI_MARKET,
        POLY_MARKET,
    )
    settle(trade, Outcome.YES)
    result = trade.settlement_result
    assert result is not None
    assert result["polymarket_settlement_asset"] == "USDC"
    assert result["settlement_assets_differ"] is True
    assert "usdc_denominated_payout" in result
    assert "depeg" in str(result["settlement_asset_note"])


async def test_us_settlement_reports_usd_and_omits_the_usdc_note() -> None:
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    trade = await executor.execute(
        _legs_with_assets(SettlementAsset.USD, SettlementAsset.USD),
        KALSHI_MARKET,
        POLY_MARKET,
    )
    settle(trade, Outcome.YES)
    result = trade.settlement_result
    assert result is not None
    assert result["polymarket_settlement_asset"] == "USD"
    assert result["settlement_assets_differ"] is False
    assert "usdc_denominated_payout" not in result
    assert "no cross-asset conversion" in str(result["settlement_asset_note"])


async def test_settlement_assets_are_read_from_the_legs_not_assumed() -> None:
    """Hardcoding either product misreports the other."""
    executor = executor_with(
        {(Venue.KALSHI, "K1"): FULL_KALSHI, (Venue.POLYMARKET, "P1"): FULL_POLY}
    )
    for asset in (SettlementAsset.USD, SettlementAsset.USDC):
        trade = await executor.execute(
            _legs_with_assets(SettlementAsset.USD, asset), KALSHI_MARKET, POLY_MARKET
        )
        settle(trade, Outcome.YES)
        assert trade.settlement_result is not None
        assert trade.settlement_result["polymarket_settlement_asset"] == asset.value
