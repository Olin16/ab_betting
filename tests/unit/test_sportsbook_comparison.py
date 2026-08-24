"""Sportsbook quote vs exchange contract: payout math and refusals.

Paper only. Every result is a research candidate; nothing here is ever arbitrage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from event_arb.domain.enums import Outcome, RejectionReason, SettlementAsset, Venue
from event_arb.domain.models import BookLevel, FeeConfig, OrderBook, OrderBookSide, VenueMarket
from event_arb.sportsbook.comparison import (
    SportsbookComparison,
    SportsbookComparisonSettings,
    SportsbookRejection,
    compare_quote_to_contract,
)
from event_arb.sportsbook.enums import (
    DataProvider,
    MarketType,
    OddsFormat,
    QuoteStatus,
    SportsbookRiskFlag,
)
from event_arb.sportsbook.models import SportsbookQuote
from event_arb.venues.kalshi.fees import default_kalshi_fee_config

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


def quote(**overrides: object) -> SportsbookQuote:
    base: dict[str, object] = {
        "provider": DataProvider.OPTICODDS,
        "sportsbook": "DraftKings",
        "provider_event_id": "EVT-1",
        "provider_market_id": "moneyline",
        "market_type": MarketType.MONEYLINE,
        "selection": "Los Angeles Lakers",
        "normalized_selection": "los_angeles_lakers",
        "odds": Decimal("150"),
        "odds_format": OddsFormat.AMERICAN,
        "max_stake": Decimal("1000"),
        "received_at": NOW,
    }
    return SportsbookQuote(**{**base, **overrides})  # type: ignore[arg-type]


def levels(*pairs: tuple[str, str]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in pairs)


def book(no_asks: tuple[BookLevel, ...], received_at: datetime = NOW, **kw: object) -> OrderBook:
    return OrderBook(
        venue=Venue.KALSHI,
        market_id="KX-LAL-WIN",
        yes=OrderBookSide(outcome=Outcome.YES, asks=levels(("0.60", "5000"))),
        no=OrderBookSide(outcome=Outcome.NO, asks=no_asks),
        received_at=received_at,
        **kw,  # type: ignore[arg-type]
    )


def market(**kw: object) -> VenueMarket:
    base: dict[str, object] = {
        "venue": Venue.KALSHI,
        "venue_market_id": "KX-LAL-WIN",
        "title": "Will the Lakers win?",
        "rules_text": "Resolves Yes if the Lakers win.",
        "fee_config": default_kalshi_fee_config(),
        "settlement_asset": SettlementAsset.USD,
        "min_quantity_increment": Decimal("1"),
    }
    return VenueMarket(**{**base, **kw})  # type: ignore[arg-type]


PERMISSIVE = SportsbookComparisonSettings(
    min_net_profit_usd=Decimal("0"),
    min_return_pct=Decimal("0"),
    manual_execution_slippage_pct=Decimal("0"),
    exchange_slippage_pct=Decimal("0"),
    operational_cost_usd=Decimal("0"),
    settlement_asset_haircut_pct=Decimal("0"),
)


def as_comparison(result: object) -> SportsbookComparison:
    assert isinstance(result, SportsbookComparison), getattr(result, "detail", result)
    return result


def as_rejection(result: object) -> SportsbookRejection:
    assert isinstance(result, SportsbookRejection), result
    return result


# ---------------------------------------------------------------- payout math


def test_payouts_are_computed_on_both_branches() -> None:
    """Bet 1000 at +150 wins 2500; 2500 NO contracts at $0.30 pay 2500 if it loses."""
    result = as_comparison(
        compare_quote_to_contract(
            quote(), book(levels(("0.30", "5000"))), market(), Outcome.NO, PERMISSIVE, now=NOW
        )
    )
    assert result.decimal_odds == Decimal("2.5")
    assert result.stake == Decimal("1000.0000")
    assert result.exchange_quantity == Decimal("2500")
    assert result.payout_if_selection_wins == Decimal("2500.0000")
    assert result.payout_if_selection_loses == Decimal("2500.0000")
    assert result.guaranteed_payout == Decimal("2500.0000")


def test_net_profit_is_guaranteed_payout_minus_every_disclosed_cost() -> None:
    result = as_comparison(
        compare_quote_to_contract(
            quote(), book(levels(("0.30", "5000"))), market(), Outcome.NO, PERMISSIVE, now=NOW
        )
    )
    reconstructed = (
        result.guaranteed_payout
        - result.sportsbook_outlay
        - result.exchange_outlay
        - result.exchange_fees
        - result.manual_slippage_allowance
        - result.operational_cost_allowance
        - result.settlement_asset_allowance
        - result.tax_allowance
    )
    assert reconstructed == result.net_profit
    assert result.net_profit > 0


def test_uses_payouts_not_implied_probabilities() -> None:
    """A hedge is decided by what you get paid, not by summing two margins."""
    result = as_comparison(
        compare_quote_to_contract(
            quote(), book(levels(("0.30", "5000"))), market(), Outcome.NO, PERMISSIVE, now=NOW
        )
    )
    # Implied probs here sum to 0.4 + 0.30 = 0.70, which alone says nothing about payout.
    assert result.guaranteed_payout == min(
        result.payout_if_selection_wins, result.payout_if_selection_loses
    )


def test_expensive_exchange_side_makes_it_unprofitable() -> None:
    """Break-even is at $0.60 here: 1000 + 2500*P must stay under the 2500 payout."""
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(), book(levels(("0.65", "5000"))), market(), Outcome.NO, PERMISSIVE, now=NOW
        )
    )
    assert rejection.reason is RejectionReason.NEGATIVE_AFTER_FEES


def test_a_five_percent_edge_is_correctly_identified_as_profitable() -> None:
    """+150 is 40% implied; a NO at $0.45 is 55%. Summing to 95% is a real 5% edge."""
    result = as_comparison(
        compare_quote_to_contract(
            quote(), book(levels(("0.45", "5000"))), market(), Outcome.NO, PERMISSIVE, now=NOW
        )
    )
    assert result.net_profit > Decimal("300")


def test_negative_american_odds_are_handled() -> None:
    result = as_comparison(
        compare_quote_to_contract(
            quote(odds=Decimal("-120")),
            book(levels(("0.40", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert result.decimal_odds == Decimal("1.833333")


# -------------------------------------------------------------------- sizing


def test_stake_uses_the_published_limit() -> None:
    result = as_comparison(
        compare_quote_to_contract(
            quote(max_stake=Decimal("250")),
            book(levels(("0.30", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert result.stake == Decimal("250.0000")
    assert result.is_liquidity_verified is True


def test_missing_limit_is_labeled_liquidity_unverified() -> None:
    """An assumed stake illustrates the shape of the trade; it is not executable size."""
    result = as_comparison(
        compare_quote_to_contract(
            quote(max_stake=None),
            book(levels(("0.30", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert result.is_liquidity_verified is False
    assert SportsbookRiskFlag.LIQUIDITY_UNVERIFIED in result.risk_flags


def test_thin_exchange_depth_shrinks_the_bet_to_stay_hedged() -> None:
    """An oversized sportsbook leg is naked exposure on the branch the exchange lost."""
    result = as_comparison(
        compare_quote_to_contract(
            quote(max_stake=Decimal("1000")),
            book(levels(("0.30", "400"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert result.exchange_quantity == Decimal("400")
    assert result.stake < Decimal("1000")
    assert result.payout_if_selection_wins <= result.payout_if_selection_loses


def test_quantity_increment_rounds_down() -> None:
    result = as_comparison(
        compare_quote_to_contract(
            quote(max_stake=Decimal("101")),
            book(levels(("0.30", "5000"))),
            market(min_quantity_increment=Decimal("100")),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert result.exchange_quantity % Decimal("100") == 0


# ---------------------------------------------------------------- refusals


def test_suspended_quote_is_refused() -> None:
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(status=QuoteStatus.SUSPENDED),
            book(levels(("0.30", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert rejection.reason is RejectionReason.MARKET_NOT_TRADABLE


def test_stale_quote_is_refused() -> None:
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(received_at=NOW - timedelta(minutes=10)),
            book(levels(("0.30", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert rejection.reason is RejectionReason.BOOK_STALE


def test_stale_exchange_book_is_refused() -> None:
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(),
            book(levels(("0.30", "5000")), received_at=NOW - timedelta(minutes=10)),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert rejection.reason is RejectionReason.BOOK_STALE


def test_explicitly_stale_book_is_refused() -> None:
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(),
            book(levels(("0.30", "5000")), is_stale=True),
            market(),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert rejection.reason is RejectionReason.BOOK_STALE


def test_untradable_exchange_market_is_refused() -> None:
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(),
            book(levels(("0.30", "5000"))),
            market(is_tradable=False),
            Outcome.NO,
            PERMISSIVE,
            now=NOW,
        )
    )
    assert rejection.reason is RejectionReason.MARKET_NOT_TRADABLE


def test_no_exchange_liquidity_is_refused() -> None:
    rejection = as_rejection(
        compare_quote_to_contract(quote(), book(()), market(), Outcome.NO, PERMISSIVE, now=NOW)
    )
    assert rejection.reason is RejectionReason.BOOK_INCOMPLETE


def test_below_minimum_thresholds_are_refused_with_the_right_reason() -> None:
    strict = SportsbookComparisonSettings(
        min_net_profit_usd=Decimal("100000"),
        min_return_pct=Decimal("0"),
        manual_execution_slippage_pct=Decimal("0"),
        exchange_slippage_pct=Decimal("0"),
        operational_cost_usd=Decimal("0"),
        settlement_asset_haircut_pct=Decimal("0"),
    )
    rejection = as_rejection(
        compare_quote_to_contract(
            quote(), book(levels(("0.30", "5000"))), market(), Outcome.NO, strict, now=NOW
        )
    )
    assert rejection.reason is RejectionReason.BELOW_MIN_PROFIT


# ------------------------------------------------------------------- labeling


def test_result_is_never_labeled_arbitrage() -> None:
    result = as_comparison(
        compare_quote_to_contract(
            quote(), book(levels(("0.30", "5000"))), market(), Outcome.NO, PERMISSIVE, now=NOW
        )
    )
    assert result.label == "research_candidate"
    assert "not risk-free" in str(result.as_dict()["disclaimer"]).lower()


def test_unapproved_mapping_is_flagged() -> None:
    result = as_comparison(
        compare_quote_to_contract(
            quote(),
            book(levels(("0.30", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            mapping_approved=False,
            now=NOW,
        )
    )
    assert SportsbookRiskFlag.UNAPPROVED_MAPPING in result.risk_flags


def test_approved_mapping_still_carries_execution_risk() -> None:
    """Approval settles equivalence. It does not make a manual leg atomic."""
    result = as_comparison(
        compare_quote_to_contract(
            quote(),
            book(levels(("0.30", "5000"))),
            market(),
            Outcome.NO,
            PERMISSIVE,
            mapping_approved=True,
            now=NOW,
        )
    )
    assert SportsbookRiskFlag.UNAPPROVED_MAPPING not in result.risk_flags
    assert SportsbookRiskFlag.MANUAL_EXECUTION_REQUIRED in result.risk_flags
    assert SportsbookRiskFlag.BOOK_MAY_REFUSE_OR_LIMIT in result.risk_flags
    assert result.label == "research_candidate"


def test_non_usd_exchange_settlement_is_charged_a_haircut() -> None:
    charged = SportsbookComparisonSettings(
        min_net_profit_usd=Decimal("0"),
        min_return_pct=Decimal("0"),
        manual_execution_slippage_pct=Decimal("0"),
        exchange_slippage_pct=Decimal("0"),
        operational_cost_usd=Decimal("0"),
        settlement_asset_haircut_pct=Decimal("1"),
    )
    result = as_comparison(
        compare_quote_to_contract(
            quote(),
            book(levels(("0.30", "5000"))),
            market(
                venue=Venue.POLYMARKET,
                settlement_asset=SettlementAsset.USDC,
                fee_config=FeeConfig(venue=Venue.POLYMARKET, taker_rate=Decimal("0")),
            ),
            Outcome.NO,
            charged,
            now=NOW,
        )
    )
    assert result.settlement_asset_allowance > 0


def test_tax_allowance_is_applied_when_configured() -> None:
    taxed = SportsbookComparisonSettings(
        min_net_profit_usd=Decimal("0"),
        min_return_pct=Decimal("0"),
        manual_execution_slippage_pct=Decimal("0"),
        exchange_slippage_pct=Decimal("0"),
        operational_cost_usd=Decimal("0"),
        settlement_asset_haircut_pct=Decimal("0"),
        winnings_tax_pct=Decimal("24"),
    )
    result = as_comparison(
        compare_quote_to_contract(
            quote(), book(levels(("0.30", "5000"))), market(), Outcome.NO, taxed, now=NOW
        )
    )
    assert result.tax_allowance > 0


# ----------------------------------------------------------------- properties


@given(
    american=st.integers(min_value=100, max_value=800).map(Decimal),
    exchange_price=st.decimals(min_value=Decimal("0.05"), max_value=Decimal("0.95"), places=2),
)
@hyp_settings(max_examples=150)
def test_emitted_comparisons_always_reconcile_and_profit(
    american: Decimal, exchange_price: Decimal
) -> None:
    """If a comparison is emitted, its own accounting must hold and profit be positive."""
    result = compare_quote_to_contract(
        quote(odds=american),
        book(levels((str(exchange_price), "100000"))),
        market(),
        Outcome.NO,
        SportsbookComparisonSettings(
            min_net_profit_usd=Decimal("1.00"),
            min_return_pct=Decimal("1.0"),
        ),
        now=NOW,
    )
    if isinstance(result, SportsbookRejection):
        return
    assert result.net_profit > 0
    assert result.guaranteed_payout == min(
        result.payout_if_selection_wins, result.payout_if_selection_loses
    )
    assert result.label == "research_candidate"


@given(american=st.integers(min_value=-800, max_value=-100).map(Decimal))
@hyp_settings(max_examples=100)
def test_negative_odds_never_produce_negative_sizing(american: Decimal) -> None:
    result = compare_quote_to_contract(
        quote(odds=american),
        book(levels(("0.30", "100000"))),
        market(),
        Outcome.NO,
        PERMISSIVE,
        now=NOW,
    )
    if isinstance(result, SportsbookComparison):
        assert result.stake > 0
        assert result.exchange_quantity > 0
