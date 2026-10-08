"""Settlement: per-venue outcomes, per-account payouts, and crediting exactly once.

Settlement arithmetic existed and was never connected to cash, so a computed $100 payout
changed no balance. It also compared both legs against one shared winning outcome, which
is wrong for an inverted pair and cannot represent two venues resolving differently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypedDict

from event_arb.domain.enums import Outcome, PaperTradeStatus, SettlementAsset, VenueProduct
from event_arb.paper.executor import PaperTrade, SettlementOutcomes, settle
from event_arb.paper.portfolio import PaperPortfolio
from event_arb.paper.settlement import apply_settlement, reconcile
from tests.unit.test_paper_accounting import (
    KALSHI_USD,
    POLY_US_USD,
    filled_trade,
    opportunity,
)


class RecordedEvidence(TypedDict):
    """The operator-supplied half of a settlement, spelled out so `**` stays checkable."""

    evidence: str
    recorded_by: str


RECORDED: RecordedEvidence = {
    "evidence": "CME FedWatch, official statement",
    "recorded_by": "reviewer",
}


def payouts(trade: PaperTrade) -> dict[str, str]:
    """The per-account payouts a settled trade recorded."""
    by_account = (trade.settlement_result or {})["payouts_by_account"]
    assert isinstance(by_account, dict)
    return by_account


def funded() -> PaperPortfolio:
    return PaperPortfolio(Decimal("1000"), Decimal("1000"))


# ------------------------------------------------------------------ crediting cash


def test_settlement_credits_the_accounts_that_were_paid() -> None:
    """The reproduced defect: a payout of 100.0000 moved no balance at all."""
    portfolio = funded()
    trade = filled_trade(opportunity())
    portfolio.record(trade)
    before_kalshi = portfolio.balance(KALSHI_USD)
    before_poly = portfolio.balance(POLY_US_USD)

    record = apply_settlement(
        portfolio, trade, SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
    )

    # Kalshi held YES and YES won, so Kalshi is paid $1 a contract; Polymarket held NO.
    assert portfolio.balance(KALSHI_USD) == before_kalshi + Decimal("100")
    assert portfolio.balance(POLY_US_USD) == before_poly
    assert record.credited == Decimal("100.0000")
    assert record.hedge_held


def test_the_payout_lands_on_the_venue_that_won_not_a_pooled_currency() -> None:
    portfolio = funded()
    trade = filled_trade(opportunity())
    portfolio.record(trade)

    apply_settlement(portfolio, trade, SettlementOutcomes.for_event(Outcome.NO, **RECORDED))

    result = trade.settlement_result or {}
    assert result["payouts_by_account"] == {
        "kalshi:USD": "0",
        "polymarket_us:USD": "100.0000",
    }


def test_settling_twice_cannot_double_credit() -> None:
    portfolio = funded()
    trade = filled_trade(opportunity())
    portfolio.record(trade)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)

    apply_settlement(portfolio, trade, outcomes)
    after_first = portfolio.balance(KALSHI_USD)
    second = apply_settlement(portfolio, trade, outcomes)

    assert second.already_settled
    assert portfolio.balance(KALSHI_USD) == after_first


# ------------------------------------------------------------- outcomes per venue


def test_an_inverted_pair_resolves_each_venue_in_its_own_outcome_space() -> None:
    """Polymarket YES corresponds to Kalshi NO, so one event is two labels.

    The previous code compared both legs against a single shared winner, so an inverted
    pair scored one leg against the wrong side of its own book.
    """
    outcomes = SettlementOutcomes.for_event(Outcome.YES, outcomes_inverted=True)
    assert outcomes.kalshi is Outcome.YES
    assert outcomes.polymarket is Outcome.NO


def test_a_hedged_inverted_pair_still_pays_a_dollar_a_contract() -> None:
    portfolio = funded()
    # Inverted: the calculator already flipped the Polymarket leg, so both legs hold YES.
    opp = opportunity()
    trade = filled_trade(opp)
    portfolio.record(trade)

    apply_settlement(
        portfolio,
        trade,
        SettlementOutcomes.for_event(Outcome.YES, outcomes_inverted=True, **RECORDED),
    )

    result = trade.settlement_result or {}
    # Kalshi YES wins; Polymarket resolves NO and its leg holds NO, so it wins too —
    # which is exactly why the hedged portion is checked against the money, not labels.
    assert result["kalshi_winning_outcome"] == "YES"
    assert result["polymarket_winning_outcome"] == "NO"


def test_a_cross_venue_discrepancy_is_representable_and_flagged() -> None:
    """Two venues resolving inconsistently is a real risk, not an impossible state.

    A single shared winner could not express it. Here both legs lose, so the "hedge"
    paid nothing and the trade says so instead of quietly reporting a clean settlement.
    """
    portfolio = funded()
    trade = filled_trade(opportunity())
    portfolio.record(trade)

    # Kalshi holds YES and resolves NO; Polymarket holds NO and resolves YES.
    apply_settlement(
        portfolio,
        trade,
        SettlementOutcomes(kalshi=Outcome.NO, polymarket=Outcome.YES, **RECORDED),
    )

    result = trade.settlement_result or {}
    assert result["hedge_held"] is False
    assert result["total_payout"] == "0.0000"
    assert any("settlement discrepancy" in note for note in trade.notes)


def test_an_unhedged_winner_is_paid_and_an_unhedged_loser_is_not() -> None:
    portfolio = funded()
    winner = filled_trade(opportunity(), leg2_quantity=None, trade_id="win")
    loser = filled_trade(opportunity(), leg2_quantity=None, trade_id="lose")
    portfolio.record(winner)
    portfolio.record(loser)

    apply_settlement(portfolio, winner, SettlementOutcomes.for_event(Outcome.YES, **RECORDED))
    apply_settlement(portfolio, loser, SettlementOutcomes.for_event(Outcome.NO, **RECORDED))

    assert (winner.settlement_result or {})["total_payout"] == "100.0000"
    assert (loser.settlement_result or {})["total_payout"] == "0.0000"


def test_global_and_us_settle_into_their_own_assets() -> None:
    portfolio = funded()
    us = filled_trade(opportunity(), trade_id="us")
    glob = filled_trade(
        opportunity(product=VenueProduct.POLYMARKET_GLOBAL, polymarket_asset=SettlementAsset.USDC),
        trade_id="global",
    )
    portfolio.record(us)
    portfolio.record(glob)

    apply_settlement(portfolio, us, SettlementOutcomes.for_event(Outcome.NO, **RECORDED))
    apply_settlement(portfolio, glob, SettlementOutcomes.for_event(Outcome.NO, **RECORDED))

    assert payouts(us)["polymarket_us:USD"] == "100.0000"
    assert payouts(glob)["polymarket_global:USDC"] == "100.0000"
    assert (glob.settlement_result or {})["settlement_assets_differ"] is True
    assert (us.settlement_result or {})["settlement_assets_differ"] is False


# ------------------------------------------------------- status and reporting split


def test_settling_does_not_erase_the_hedge_classification() -> None:
    """Settling a partially-hedged trade used to remove it from the partial count.

    ``status`` moved to ``settled`` and was the only record of how the trade filled, so
    the hedge-success rate improved purely because time had passed.
    """
    portfolio = funded()
    trade = filled_trade(opportunity(), leg2_quantity="40")
    portfolio.record(trade)
    assert portfolio.daily_report().partially_hedged == 1

    apply_settlement(portfolio, trade, SettlementOutcomes.for_event(Outcome.YES, **RECORDED))

    assert trade.status is PaperTradeStatus.SETTLED
    assert trade.execution_status is PaperTradeStatus.PARTIALLY_HEDGED
    assert portfolio.daily_report().partially_hedged == 1


def test_settled_pnl_is_attributed_to_the_day_it_settled() -> None:
    """A trade opened Monday and settled Friday is Monday's execution and Friday's cash."""
    portfolio = funded()
    opened = datetime.now(UTC) - timedelta(days=4)
    trade = filled_trade(opportunity())
    trade.created_at = opened
    portfolio.record(trade)
    apply_settlement(portfolio, trade, SettlementOutcomes.for_event(Outcome.YES, **RECORDED))

    open_day = portfolio.daily_report(opened.date())
    settle_day = portfolio.daily_report(datetime.now(UTC).date())

    assert open_day.trades == 1 and open_day.settled_trades == 0
    assert settle_day.trades == 0 and settle_day.settled_trades == 1
    assert settle_day.settled_pnl != Decimal("0")
    assert settle_day.settled_pnl_by_account["kalshi:USD"] == "100.0000"


def test_an_unsettled_trade_still_counts_as_open_exposure() -> None:
    portfolio = funded()
    trade = filled_trade(opportunity(), leg2_quantity=None)
    portfolio.record(trade)
    assert portfolio.total_unhedged_quantity == Decimal("100.0000")

    apply_settlement(portfolio, trade, SettlementOutcomes.for_event(Outcome.YES, **RECORDED))

    assert portfolio.total_unhedged_quantity == Decimal("0.0000")
    assert portfolio.open_exposure() == []


# ----------------------------------------------------------------- batch reconcile


def test_reconcile_can_credit_or_merely_compute() -> None:
    computing = funded()
    trade = filled_trade(opportunity(), trade_id="compute")
    computing.record(trade)
    before = computing.balance(KALSHI_USD)

    result = reconcile([trade], {"pair1": Outcome.YES})

    assert result.settled == 1
    assert computing.balance(KALSHI_USD) == before, "no portfolio passed: compute only"

    crediting = funded()
    other = filled_trade(opportunity(), trade_id="credit")
    crediting.record(other)
    reconcile([other], {"pair1": Outcome.YES}, portfolio=crediting)
    assert crediting.balance(KALSHI_USD) > before


def test_reconcile_skips_a_pair_with_no_recorded_outcome() -> None:
    portfolio = funded()
    trade = filled_trade(opportunity())
    portfolio.record(trade)

    result = reconcile([trade], {}, portfolio=portfolio)

    assert result.settled == 0
    assert not trade.is_settled


def test_the_legacy_single_outcome_signature_still_works() -> None:
    """Existing callers pass one Outcome; they must keep meaning what they meant."""
    trade = filled_trade(opportunity())
    settle(trade, Outcome.YES)
    result = trade.settlement_result or {}
    assert result["kalshi_winning_outcome"] == "YES"
    assert result["polymarket_winning_outcome"] == "YES"
    assert trade.is_settled


def test_unhedged_leg_from_the_global_corpus_is_measured_against_its_own_book() -> None:
    """Regression for the previous logic reading the wrong leg on a USDC pair."""
    portfolio = funded()
    opp = opportunity(product=VenueProduct.POLYMARKET_GLOBAL, polymarket_asset=SettlementAsset.USDC)
    # Leg 2 filled more than leg 1, so the unhedged remainder sits on Polymarket.
    trade = filled_trade(opp, leg1_quantity="40", leg2_quantity="100")
    portfolio.record(trade)

    apply_settlement(portfolio, trade, SettlementOutcomes.for_event(Outcome.NO, **RECORDED))

    # Polymarket held NO and NO won: all 100 of its contracts pay, in USDC.
    assert payouts(trade)["polymarket_global:USDC"] == "100.0000"
    assert payouts(trade)["kalshi:USD"] == "0"
