"""Per-account balances, capital enforcement, and the ledger they are derived from.

These tests exist because the previous suite passed while the simulator debited the wrong
currency and spent money no account held. Every test here names the specific wrong
behaviour it prevents, so a future change that reintroduces one fails with an explanation
rather than an assertion count.
"""

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
from event_arb.domain.models import LegPlan, Opportunity
from event_arb.paper.accounts import AccountId, account_for
from event_arb.paper.executor import PaperTrade
from event_arb.paper.fills import SimulatedOrder
from event_arb.paper.ledger import EntryKind, LedgerEntry, PaperLedger
from event_arb.paper.portfolio import PaperPortfolio, expected_accounts

KALSHI_USD = AccountId(Venue.KALSHI, VenueProduct.NONE, SettlementAsset.USD)
POLY_US_USD = AccountId(Venue.POLYMARKET, VenueProduct.POLYMARKET_US, SettlementAsset.USD)
POLY_GLOBAL_USDC = AccountId(Venue.POLYMARKET, VenueProduct.POLYMARKET_GLOBAL, SettlementAsset.USDC)


def leg(
    venue: Venue,
    product: VenueProduct,
    asset: SettlementAsset,
    outcome: Outcome,
    price: str,
    quantity: str = "100",
) -> LegPlan:
    q = Decimal(quantity)
    return LegPlan(
        venue=venue,
        product=product,
        market_id="M1" if venue is Venue.KALSHI else "M2",
        outcome=outcome,
        quantity=q,
        vwap=Decimal(price),
        worst_price=Decimal(price),
        levels_consumed=1,
        gross_cost=q * Decimal(price),
        fees=Decimal("0"),
        settlement_asset=asset,
        book_age_ms=10,
    )


def opportunity(
    *,
    product: VenueProduct = VenueProduct.POLYMARKET_US,
    polymarket_asset: SettlementAsset = SettlementAsset.USD,
    quantity: str = "100",
    kalshi_fees: str = "1.00",
    polymarket_fees: str = "0.50",
    detected_at: datetime | None = None,
) -> Opportunity:
    q = Decimal(quantity)
    kalshi_leg = leg(
        Venue.KALSHI, VenueProduct.NONE, SettlementAsset.USD, Outcome.YES, "0.55", quantity
    )
    polymarket_leg = leg(Venue.POLYMARKET, product, polymarket_asset, Outcome.NO, "0.38", quantity)
    return Opportunity(
        pair_id="pair1",
        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
        label=OpportunityLabel.CONFIRMED,
        requested_quantity=q,
        executable_quantity=q,
        kalshi_leg=kalshi_leg,
        polymarket_leg=polymarket_leg,
        gross_acquisition_cost=kalshi_leg.gross_cost + polymarket_leg.gross_cost,
        kalshi_fees=Decimal(kalshi_fees),
        polymarket_fees=Decimal(polymarket_fees),
        slippage_allowance=Decimal("0"),
        operational_cost_allowance=Decimal("0"),
        usdc_conversion_allowance=Decimal("0"),
        expected_payout=q,
        net_profit=Decimal("5"),
        return_on_capital=Decimal("5"),
        gross_spread=Decimal("0.07"),
        detected_at=detected_at or datetime.now(UTC),
    )


def filled_trade(
    opp: Opportunity,
    *,
    leg1_quantity: str = "100",
    leg2_quantity: str | None = "100",
    trade_id: str = "trade-1",
) -> PaperTrade:
    """A trade with the fills spelled out, so accounting can be asserted exactly."""

    def order(plan: LegPlan, quantity: str) -> SimulatedOrder:
        q = Decimal(quantity)
        return SimulatedOrder(
            venue=plan.venue,
            market_id=plan.market_id,
            outcome=plan.outcome,
            requested_quantity=plan.quantity,
            filled_quantity=q,
            fills=(),
            gross_cost=q * plan.vwap,
            limit_price=plan.worst_price,
            client_order_id=f"{trade_id}-{plan.venue.value}",
        )

    trade = PaperTrade(
        id=trade_id,
        pair_id=opp.pair_id,
        direction=opp.direction,
        opportunity=opp,
        leg1=order(opp.kalshi_leg, leg1_quantity),
        leg2=order(opp.polymarket_leg, leg2_quantity) if leg2_quantity is not None else None,
        kalshi_fees=opp.kalshi_fees,
        polymarket_fees=opp.polymarket_fees,
    )
    if leg2_quantity is None:
        trade.set_execution_status(PaperTradeStatus.LEG2_FAILED)
    elif Decimal(leg2_quantity) < Decimal(leg1_quantity):
        trade.set_execution_status(PaperTradeStatus.PARTIALLY_HEDGED)
    else:
        trade.set_execution_status(PaperTradeStatus.BOTH_LEGS_FILLED)
    return trade


# ------------------------------------------------------------------ account identity


def test_kalshi_usd_and_polymarket_us_usd_are_different_accounts() -> None:
    """The defect this whole module exists for: both legs settle in USD, at two venues.

    A single USD balance let a Polymarket US leg spend Kalshi's cash. They are the same
    currency and not the same money — moving between them is a withdrawal and a deposit.
    """
    kalshi = account_for(Venue.KALSHI, VenueProduct.NONE, SettlementAsset.USD)
    polymarket = account_for(Venue.POLYMARKET, VenueProduct.POLYMARKET_US, SettlementAsset.USD)
    assert kalshi != polymarket
    assert kalshi.key == "kalshi:USD"
    assert polymarket.key == "polymarket_us:USD"


def test_polymarket_products_never_share_an_account() -> None:
    assert account_for(
        Venue.POLYMARKET, VenueProduct.POLYMARKET_US, SettlementAsset.USD
    ) != account_for(Venue.POLYMARKET, VenueProduct.POLYMARKET_GLOBAL, SettlementAsset.USD)


@pytest.mark.parametrize("key", ["kalshi:USD", "polymarket_us:USD", "polymarket_global:USDC"])
def test_account_keys_round_trip(key: str) -> None:
    assert AccountId.parse(key).key == key


@pytest.mark.parametrize("key", ["", "kalshi", "kalshi:EUR", "coinbase:USD", "none:USD"])
def test_unparseable_account_keys_are_refused(key: str) -> None:
    """A typo must not silently mint an empty account that then funds nothing."""
    with pytest.raises(ValueError):
        AccountId.parse(key)


def test_expected_accounts_follow_the_configured_product() -> None:
    us = expected_accounts(VenueProduct.POLYMARKET_US)
    glob = expected_accounts(VenueProduct.POLYMARKET_GLOBAL)
    assert [a.key for a in us] == ["kalshi:USD", "polymarket_us:USD"]
    assert [a.key for a in glob] == ["kalshi:USD", "polymarket_global:USDC"]


# ------------------------------------------------------------------------- the ledger


def test_balances_are_derived_and_replay_is_idempotent() -> None:
    ledger = PaperLedger({KALSHI_USD: Decimal("100")})
    entry = LedgerEntry(
        entry_id="t1:leg1:cost",
        account=KALSHI_USD,
        kind=EntryKind.TRADE_DEBIT,
        amount=Decimal("-55"),
        trade_id="t1",
    )
    assert ledger.append(entry) is True
    assert ledger.append(entry) is False, "the same entry id must never apply twice"
    assert ledger.balance(KALSHI_USD) == Decimal("45.0000")


def test_a_reservation_reduces_available_but_not_balance() -> None:
    ledger = PaperLedger({KALSHI_USD: Decimal("100")})
    ledger.append(
        LedgerEntry(
            entry_id="t1:reserve:kalshi:USD",
            account=KALSHI_USD,
            kind=EntryKind.RESERVATION,
            amount=Decimal("60"),
            trade_id="t1",
        )
    )
    assert ledger.balance(KALSHI_USD) == Decimal("100.0000")
    assert ledger.available(KALSHI_USD) == Decimal("40.0000")


def test_compaction_bounds_the_ledger_without_moving_a_balance() -> None:
    ledger = PaperLedger({KALSHI_USD: Decimal("100")})
    old = datetime.now(UTC) - timedelta(days=400)
    for i in range(5):
        ledger.append(
            LedgerEntry(
                entry_id=f"t{i}:leg1:cost",
                account=KALSHI_USD,
                kind=EntryKind.TRADE_DEBIT,
                amount=Decimal("-10"),
                trade_id=f"t{i}",
                created_at=old,
            )
        )
    before = ledger.balance(KALSHI_USD)

    removed = ledger.compact_before(datetime.now(UTC) - timedelta(days=30), frozenset())

    assert removed > 0
    assert ledger.balance(KALSHI_USD) == before, "compaction must be exactly cash-neutral"


def test_compaction_leaves_an_open_trades_entries_alone() -> None:
    ledger = PaperLedger({KALSHI_USD: Decimal("100")})
    old = datetime.now(UTC) - timedelta(days=400)
    ledger.append(
        LedgerEntry(
            entry_id="open:leg1:cost",
            account=KALSHI_USD,
            kind=EntryKind.TRADE_DEBIT,
            amount=Decimal("-10"),
            trade_id="still-open",
            created_at=old,
        )
    )
    ledger.compact_before(datetime.now(UTC) - timedelta(days=30), frozenset({"still-open"}))
    assert ledger.has("open:leg1:cost")


# ------------------------------------------------------- debiting the correct account


def test_a_us_trade_debits_two_usd_accounts_and_leaves_usdc_untouched() -> None:
    """The reproduced defect: a US trade drew down USDC that no US account holds.

    Both legs of a Polymarket US opportunity settle in USD. Leg 2 was debited from USDC
    unconditionally, so the USD balance was understated and a USDC balance moved for a
    trade that never touched USDC.
    """
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    trade = filled_trade(opportunity())

    portfolio.record(trade)

    assert portfolio.balance(KALSHI_USD) == Decimal("944.0000")  # 1000 - 55 - 1.00
    assert portfolio.balance(POLY_US_USD) == Decimal("961.5000")  # 1000 - 38 - 0.50
    assert portfolio.balance(POLY_GLOBAL_USDC) == Decimal("1000.0000")


def test_a_global_trade_still_debits_usdc_for_its_polymarket_leg() -> None:
    """The legacy corpus must keep working: Global genuinely does settle in USDC."""
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    opp = opportunity(product=VenueProduct.POLYMARKET_GLOBAL, polymarket_asset=SettlementAsset.USDC)

    portfolio.record(filled_trade(opp))

    assert portfolio.balance(KALSHI_USD) == Decimal("944.0000")
    assert portfolio.balance(POLY_GLOBAL_USDC) == Decimal("961.5000")
    assert portfolio.balance(POLY_US_USD) == Decimal("1000.0000")


def test_recording_the_same_trade_twice_does_not_double_debit() -> None:
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    trade = filled_trade(opportunity())
    portfolio.record(trade)
    after_first = portfolio.balance(KALSHI_USD)

    portfolio.record(trade)

    assert portfolio.balance(KALSHI_USD) == after_first
    assert len(portfolio.trades) == 1


# ------------------------------------------------------------- capital enforcement


def test_zero_funds_prevents_a_fill() -> None:
    portfolio = PaperPortfolio(Decimal("0"), Decimal("0"))
    check = portfolio.check_capital(opportunity())
    assert not check.allowed
    assert set(check.shortfalls) == {"kalshi:USD", "polymarket_us:USD"}


def test_affordability_includes_fees() -> None:
    """Cash enough for the contracts but not the fee is not enough cash."""
    opp = opportunity(kalshi_fees="5.00", polymarket_fees="0")
    exact = PaperPortfolio(
        opening_balances={KALSHI_USD: Decimal("55.00"), POLY_US_USD: Decimal("1000")}
    )
    assert not exact.check_capital(opp).allowed

    funded = PaperPortfolio(
        opening_balances={KALSHI_USD: Decimal("60.00"), POLY_US_USD: Decimal("1000")}
    )
    assert funded.check_capital(opp).allowed


def test_funds_at_one_venue_cannot_fund_the_other() -> None:
    """A pooled USD balance made this pass. It must not."""
    portfolio = PaperPortfolio(
        opening_balances={KALSHI_USD: Decimal("10000"), POLY_US_USD: Decimal("0")}
    )
    check = portfolio.check_capital(opportunity())
    assert not check.allowed
    assert set(check.shortfalls) == {"polymarket_us:USD"}


def test_a_reservation_stops_a_second_trade_spending_the_same_cash() -> None:
    """Two opportunities racing through one account must not both pass the check."""
    portfolio = PaperPortfolio(
        opening_balances={KALSHI_USD: Decimal("60"), POLY_US_USD: Decimal("60")}
    )
    first = opportunity()
    assert portfolio.check_capital(first).allowed
    portfolio.reserve("hold-1", portfolio.funding_for(first))

    second = opportunity(detected_at=datetime.now(UTC) + timedelta(seconds=1))
    assert not portfolio.check_capital(second).allowed


def test_min_account_balance_is_respected() -> None:
    portfolio = PaperPortfolio(
        opening_balances={KALSHI_USD: Decimal("60"), POLY_US_USD: Decimal("60")},
        min_account_balance=Decimal("50"),
    )
    check = portfolio.check_capital(opportunity())
    assert not check.allowed
    assert "minimum" in check.reason


def test_notional_and_open_notional_limits_refuse_with_a_stated_reason() -> None:
    portfolio = PaperPortfolio(Decimal("10000"), Decimal("10000"), max_trade_notional=Decimal("10"))
    check = portfolio.check_capital(opportunity())
    assert not check.allowed
    assert "max_trade_notional" in check.reason
    assert not check.shortfalls, "a limit refusal is not a funding shortfall"


def test_open_unhedged_exposure_blocks_further_trades() -> None:
    """The answer to a failed second leg: stop, rather than accumulate naked exposure."""
    portfolio = PaperPortfolio(
        Decimal("10000"), Decimal("10000"), max_open_unhedged_quantity=Decimal("10")
    )
    portfolio.record(filled_trade(opportunity(), leg2_quantity=None))
    assert portfolio.total_unhedged_quantity == Decimal("100.0000")

    check = portfolio.check_capital(opportunity())

    assert not check.allowed
    assert "unhedged" in check.reason


def test_a_partial_fill_releases_the_cash_it_did_not_spend() -> None:
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    opp = opportunity()
    funding = portfolio.funding_for(opp)
    portfolio.reserve("trade-1", funding)
    assert portfolio.available(POLY_US_USD) < portfolio.balance(POLY_US_USD)

    portfolio.record(filled_trade(opp, leg2_quantity="40"))

    # Only 40 contracts of leg 2 were bought, and nothing stays held afterwards.
    assert portfolio.balance(POLY_US_USD) == Decimal("984.3000")  # 1000 - 15.20 - 0.50
    assert portfolio.available(POLY_US_USD) == portfolio.balance(POLY_US_USD)


# ------------------------------------------------------- releases undo holds, only holds
#
# A release used to be recomputed from the funding requirement and appended
# unconditionally, so it did not need a hold to exist in order to hand back cash. These
# tests exist because that made the capital gate inert on the one path that runs
# unattended, and the whole suite still passed.


def test_releasing_a_hold_that_was_never_taken_frees_nothing() -> None:
    """A release with no hold behind it used to credit availability out of nothing."""
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))

    assert portfolio.release("a-trade-that-never-held-anything") == []

    assert portfolio.ledger.reserved(KALSHI_USD) == Decimal("0.0000")
    assert portfolio.available(KALSHI_USD) == portfolio.balance(KALSHI_USD)


def test_reserved_cash_never_goes_negative_however_often_release_is_called() -> None:
    """`reserved` below zero is not a harmless sign error: it invents spendable cash."""
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    portfolio.reserve("hold-1", portfolio.funding_for(opportunity()))

    assert portfolio.release("hold-1") != []
    for _ in range(3):
        assert portfolio.release("hold-1") == [], "a hold can only be given back once"

    assert portfolio.ledger.reserved(KALSHI_USD) == Decimal("0.0000")


def test_a_release_hands_back_exactly_what_the_hold_took() -> None:
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    held = portfolio.reserve("hold-1", portfolio.funding_for(opportunity()))

    released = portfolio.release("hold-1")

    assert {e.account for e in released} == {e.account for e in held}
    by_account = {e.account: e.amount for e in held}
    assert all(e.amount == -by_account[e.account] for e in released)


def test_a_trade_recorded_under_a_different_id_than_its_hold_still_spends_cash() -> None:
    """The exact shape of the automated path, which no test covered.

    Automated trading holds under the *opportunity's* id and records under the *trade's*,
    so the two ids never match. While `record` released unconditionally, every trade
    therefore appended a phantom release that cancelled its own debit to the cent —
    `reserved` went negative, `available` stayed pinned at the opening balance, and the
    capital gate could not refuse anything no matter how much had already been spent.
    """
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    opp = opportunity()
    portfolio.reserve("hold:pair1:some-instant", portfolio.funding_for(opp))

    portfolio.record(filled_trade(opp, trade_id="a-different-id"))
    portfolio.release("hold:pair1:some-instant")

    spent = Decimal("1000") - portfolio.balance(KALSHI_USD)
    assert spent > Decimal("0"), "the trade must actually cost something"
    assert portfolio.ledger.reserved(KALSHI_USD) == Decimal("0.0000")
    assert portfolio.available(KALSHI_USD) == portfolio.balance(KALSHI_USD)


def test_a_hold_left_by_a_dead_process_is_released_when_the_portfolio_is_restored() -> None:
    """Reservations are exempt from retention, so an unreaped hold reserves cash forever."""
    portfolio = PaperPortfolio(Decimal("1000"), Decimal("1000"))
    stranded = portfolio.reserve(
        "hold:from-a-process-that-died", portfolio.funding_for(opportunity())
    )
    assert portfolio.available(KALSHI_USD) < portfolio.balance(KALSHI_USD)

    released = portfolio.release_stale_holds()

    assert {e.entry_id for e in released} == {
        e.entry_id.replace(":reserve:", ":release:") for e in stranded
    }
    assert portfolio.available(KALSHI_USD) == portfolio.balance(KALSHI_USD)
    assert portfolio.release_stale_holds() == [], "reaping twice must change nothing"


# --------------------------------------------------------- compatibility of settings


def test_per_asset_settings_open_each_account_at_that_figure() -> None:
    """The stated policy: not a pool to divide, so no split is invented."""
    settings = PaperSettings(starting_usd_balance=Decimal("7500"))
    assert settings.starting_balance_for("kalshi:USD", SettlementAsset.USD) == Decimal("7500")
    assert settings.starting_balance_for("polymarket_us:USD", SettlementAsset.USD) == Decimal(
        "7500"
    )


def test_explicit_per_account_balances_win() -> None:
    settings = PaperSettings(
        starting_usd_balance=Decimal("7500"),
        starting_balances={"kalshi:USD": Decimal("100")},
    )
    assert settings.starting_balance_for("kalshi:USD", SettlementAsset.USD) == Decimal("100")
    assert settings.starting_balance_for("polymarket_us:USD", SettlementAsset.USD) == Decimal(
        "7500"
    )


def test_a_misspelled_account_key_is_rejected_at_configuration_time() -> None:
    with pytest.raises(ValueError):
        PaperSettings(starting_balances={"polymarket:usd": Decimal("100")})
