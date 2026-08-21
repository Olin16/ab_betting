"""Arbitrage calculation: both directions, every cost component, boundary rounding."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from event_arb.config import OpportunitySettings
from event_arb.domain.enums import (
    ArbDirection,
    MatchStatus,
    OpportunityLabel,
    Outcome,
    RejectionReason,
    RiskFlag,
    SettlementAsset,
    Venue,
)
from event_arb.domain.models import (
    BookLevel,
    FeeConfig,
    MarketPair,
    Opportunity,
    OrderBook,
    OrderBookSide,
    RejectedOpportunity,
    VenueMarket,
)
from event_arb.opportunities.calculator import CalculationInputs, calculate_direction
from event_arb.venues.kalshi.fees import default_kalshi_fee_config
from event_arb.venues.polymarket.fees import polymarket_fee_config

CLOSE = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)


def levels(*pairs: tuple[str, str]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in pairs)


def book(
    venue: Venue,
    market_id: str,
    yes_asks: tuple[BookLevel, ...],
    no_asks: tuple[BookLevel, ...],
    yes_bids: tuple[BookLevel, ...] = (),
    no_bids: tuple[BookLevel, ...] = (),
    sequence: int | None = 1,
) -> OrderBook:
    return OrderBook(
        venue=venue,
        market_id=market_id,
        yes=OrderBookSide(outcome=Outcome.YES, bids=yes_bids, asks=yes_asks),
        no=OrderBookSide(outcome=Outcome.NO, bids=no_bids, asks=no_asks),
        sequence=sequence,
    )


def kalshi_market(fee: FeeConfig | None = None, tradable: bool = True) -> VenueMarket:
    return VenueMarket(
        venue=Venue.KALSHI,
        venue_market_id="K1",
        title="Kalshi market",
        rules_text="rules",
        close_time=CLOSE,
        fee_config=fee or default_kalshi_fee_config(),
        settlement_asset=SettlementAsset.USD,
        is_tradable=tradable,
        is_active=tradable,
    )


def poly_market(category: str = "finance", tradable: bool = True) -> VenueMarket:
    return VenueMarket(
        venue=Venue.POLYMARKET,
        venue_market_id="P1",
        title="Polymarket market",
        rules_text="rules",
        close_time=CLOSE,
        fee_config=polymarket_fee_config(category),
        settlement_asset=SettlementAsset.USDC,
        is_tradable=tradable,
        is_active=tradable,
    )


def approved_pair(**overrides: object) -> MarketPair:
    base: dict[str, object] = {
        "id": "pair1",
        "kalshi_market_id": "K1",
        "polymarket_market_id": "P1",
        "status": MatchStatus.APPROVED,
        "manually_approved": True,
    }
    return MarketPair(**{**base, **overrides})  # type: ignore[arg-type]


def make_inputs(
    kalshi_yes_asks: tuple[BookLevel, ...] = levels(("0.55", "300")),
    poly_no_asks: tuple[BookLevel, ...] = levels(("0.38", "300")),
    kalshi_no_asks: tuple[BookLevel, ...] = levels(("0.47", "300")),
    poly_yes_asks: tuple[BookLevel, ...] = levels(("0.64", "300")),
    pair: MarketPair | None = None,
    settings: OpportunitySettings | None = None,
    kalshi_age: int = 100,
    poly_age: int = 100,
    poly_category: str = "geopolitical",
    usdc_haircut: Decimal = Decimal("0"),
    kalshi_tradable: bool = True,
    poly_tradable: bool = True,
) -> CalculationInputs:
    """Build a calculation context. Defaults use a fee-free Polymarket category and no
    USDC haircut so individual cost components can be isolated in tests."""
    return CalculationInputs(
        pair=pair or approved_pair(),
        kalshi_market=kalshi_market(tradable=kalshi_tradable),
        polymarket_market=poly_market(poly_category, tradable=poly_tradable),
        kalshi_book=book(Venue.KALSHI, "K1", kalshi_yes_asks, kalshi_no_asks),
        polymarket_book=book(Venue.POLYMARKET, "P1", poly_yes_asks, poly_no_asks, sequence=None),
        settings=settings
        or OpportunitySettings(
            min_net_profit_usd=Decimal("0"),
            min_return_pct=Decimal("0"),
            slippage_buffer_pct=Decimal("0"),
            operational_cost_usd=Decimal("0"),
            max_quantity=Decimal("100"),
        ),
        kalshi_book_age_ms=kalshi_age,
        polymarket_book_age_ms=poly_age,
        usdc_haircut_pct=usdc_haircut,
    )


def as_opportunity(result: Opportunity | RejectedOpportunity) -> Opportunity:
    assert isinstance(result, Opportunity), getattr(result, "detail", result)
    return result


def as_rejection(result: Opportunity | RejectedOpportunity) -> RejectedOpportunity:
    assert isinstance(result, RejectedOpportunity), result
    return result


# --------------------------------------------------------------------- direction A


def test_direction_a_buys_kalshi_yes_and_polymarket_no() -> None:
    opp = as_opportunity(calculate_direction(make_inputs(), ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.kalshi_leg.outcome is Outcome.YES
    assert opp.polymarket_leg.outcome is Outcome.NO


def test_direction_a_profit_arithmetic_is_exact() -> None:
    """100 @ $0.55 + 100 @ $0.38 = $93.00 for a guaranteed $100.00 payout."""
    opp = as_opportunity(calculate_direction(make_inputs(), ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.executable_quantity == Decimal("100")
    assert opp.gross_acquisition_cost == Decimal("93.0000")
    assert opp.expected_payout == Decimal("100.0000")
    assert opp.gross_spread == Decimal("7.0000")
    # Kalshi fee: ceil(0.07 * 100 * 0.55 * 0.45) = 1.7325 -> $1.74 after cent rounding.
    assert opp.kalshi_fees == Decimal("1.74")
    assert opp.polymarket_fees == Decimal("0")  # fee-free category
    assert opp.net_profit == Decimal("5.2600")


def test_all_cost_components_are_reported_separately() -> None:
    """The dashboard has to show where every cent went."""
    opp = as_opportunity(calculate_direction(make_inputs(), ArbDirection.A_KALSHI_YES_POLY_NO))
    reconstructed = (
        opp.expected_payout
        - opp.gross_acquisition_cost
        - opp.kalshi_fees
        - opp.polymarket_fees
        - opp.slippage_allowance
        - opp.operational_cost_allowance
        - opp.usdc_conversion_allowance
    )
    assert reconstructed == opp.net_profit


# --------------------------------------------------------------------- direction B


def test_direction_b_buys_kalshi_no_and_polymarket_yes() -> None:
    inputs = make_inputs(
        kalshi_no_asks=levels(("0.40", "300")), poly_yes_asks=levels(("0.52", "300"))
    )
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.B_KALSHI_NO_POLY_YES))
    assert opp.kalshi_leg.outcome is Outcome.NO
    assert opp.polymarket_leg.outcome is Outcome.YES
    assert opp.gross_acquisition_cost == Decimal("92.0000")


def test_unprofitable_direction_is_rejected_while_the_other_succeeds() -> None:
    """The default fixture has an edge one way only; both must be evaluated."""
    inputs = make_inputs()
    profitable = calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)
    unprofitable = calculate_direction(inputs, ArbDirection.B_KALSHI_NO_POLY_YES)
    assert isinstance(profitable, Opportunity)
    assert as_rejection(unprofitable).reason is RejectionReason.NO_EXECUTABLE_QUANTITY


def test_inverted_outcome_mapping_flips_the_polymarket_leg() -> None:
    """When Polymarket YES == Kalshi NO, buying both YES legs would double the bet."""
    inputs = make_inputs(
        pair=approved_pair(outcomes_inverted=True),
        poly_yes_asks=levels(("0.38", "300")),
        poly_no_asks=levels(("0.64", "300")),
    )
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.kalshi_leg.outcome is Outcome.YES
    # Inverted: Polymarket YES is the hedge for Kalshi YES, not Polymarket NO.
    assert opp.polymarket_leg.outcome is Outcome.YES
    assert opp.gross_acquisition_cost == Decimal("93.0000")


# ------------------------------------------------------------------------- depth


def test_executable_quantity_limited_by_the_thinner_leg() -> None:
    inputs = make_inputs(
        kalshi_yes_asks=levels(("0.55", "300")), poly_no_asks=levels(("0.38", "40"))
    )
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.executable_quantity == Decimal("40")
    assert opp.kalshi_leg.quantity == opp.polymarket_leg.quantity


def test_vwap_used_not_top_of_book() -> None:
    """Second level must raise the effective price of the whole order."""
    inputs = make_inputs(
        kalshi_yes_asks=levels(("0.55", "50"), ("0.58", "100")),
        poly_no_asks=levels(("0.38", "300")),
    )
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.kalshi_leg.levels_consumed == 2
    assert opp.kalshi_leg.vwap == Decimal("0.5650")
    assert opp.kalshi_leg.worst_price == Decimal("0.58")


def test_deep_levels_beyond_the_edge_are_not_counted() -> None:
    """A level at $0.70 cannot pair with a $0.38 leg; the walk must stop."""
    inputs = make_inputs(
        kalshi_yes_asks=levels(("0.55", "10"), ("0.70", "1000")),
        poly_no_asks=levels(("0.38", "300")),
    )
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.executable_quantity == Decimal("10")


# ------------------------------------------------------------------- rejections


def test_no_arbitrage_when_combined_ask_reaches_a_dollar() -> None:
    inputs = make_inputs(
        kalshi_yes_asks=levels(("0.62", "300")), poly_no_asks=levels(("0.38", "300"))
    )
    rejection = as_rejection(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert rejection.reason is RejectionReason.NO_EXECUTABLE_QUANTITY


def test_boundary_exactly_one_dollar_is_rejected() -> None:
    """At exactly $1.00 there is no edge, only fees. This boundary must not pass."""
    inputs = make_inputs(
        kalshi_yes_asks=levels(("0.60", "300")), poly_no_asks=levels(("0.40", "300"))
    )
    assert (
        as_rejection(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)).reason
        is RejectionReason.NO_EXECUTABLE_QUANTITY
    )


def test_thin_edge_erased_by_fees_is_rejected() -> None:
    """A 1-cent gross spread does not survive Kalshi's fee at mid-range prices."""
    inputs = make_inputs(
        kalshi_yes_asks=levels(("0.55", "300")), poly_no_asks=levels(("0.44", "300"))
    )
    assert (
        as_rejection(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)).reason
        is RejectionReason.NEGATIVE_AFTER_FEES
    )


def test_below_minimum_profit_is_rejected_with_that_reason() -> None:
    inputs = make_inputs(
        settings=OpportunitySettings(
            min_net_profit_usd=Decimal("1000"),
            min_return_pct=Decimal("0"),
            slippage_buffer_pct=Decimal("0"),
            operational_cost_usd=Decimal("0"),
            max_quantity=Decimal("100"),
        )
    )
    assert (
        as_rejection(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)).reason
        is RejectionReason.BELOW_MIN_PROFIT
    )


def test_below_minimum_return_is_rejected_with_that_reason() -> None:
    inputs = make_inputs(
        settings=OpportunitySettings(
            min_net_profit_usd=Decimal("0"),
            min_return_pct=Decimal("99"),
            slippage_buffer_pct=Decimal("0"),
            operational_cost_usd=Decimal("0"),
            max_quantity=Decimal("100"),
        )
    )
    assert (
        as_rejection(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)).reason
        is RejectionReason.BELOW_MIN_RETURN
    )


def test_untradable_market_is_rejected() -> None:
    assert (
        as_rejection(
            calculate_direction(
                make_inputs(kalshi_tradable=False), ArbDirection.A_KALSHI_YES_POLY_NO
            )
        ).reason
        is RejectionReason.MARKET_NOT_TRADABLE
    )


def test_empty_ask_side_is_rejected() -> None:
    assert (
        as_rejection(
            calculate_direction(make_inputs(poly_no_asks=()), ArbDirection.A_KALSHI_YES_POLY_NO)
        ).reason
        is RejectionReason.BOOK_INCOMPLETE
    )


# ------------------------------------------------------------------------ buffers


def test_slippage_buffer_scales_with_gross_cost() -> None:
    inputs = make_inputs(
        settings=OpportunitySettings(
            min_net_profit_usd=Decimal("0"),
            min_return_pct=Decimal("0"),
            slippage_buffer_pct=Decimal("1"),
            operational_cost_usd=Decimal("0"),
            max_quantity=Decimal("100"),
        )
    )
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.slippage_allowance == Decimal("0.9300")


def test_usdc_haircut_is_charged_against_the_payout() -> None:
    """USDC is not USD. A 1% haircut on a $100 payout is $1.00 of real cost."""
    opp = as_opportunity(
        calculate_direction(
            make_inputs(usdc_haircut=Decimal("1")), ArbDirection.A_KALSHI_YES_POLY_NO
        )
    )
    assert opp.usdc_conversion_allowance == Decimal("1.0000")


def test_polymarket_category_fee_is_charged() -> None:
    """Finance category is 0.04: 100 * 0.04 * 0.38 * 0.62 = $0.9424."""
    opp = as_opportunity(
        calculate_direction(make_inputs(poly_category="finance"), ArbDirection.A_KALSHI_YES_POLY_NO)
    )
    assert opp.polymarket_fees == Decimal("0.9424")


# -------------------------------------------------------------------------- label


def test_unapproved_pair_is_labeled_research_candidate_never_arbitrage() -> None:
    """The central safety rule: only a human-approved pair is 'confirmed'."""
    inputs = make_inputs(pair=MarketPair(id="p", kalshi_market_id="K1", polymarket_market_id="P1"))
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.label is OpportunityLabel.RESEARCH_CANDIDATE
    assert opp.is_confirmed is False


def test_approved_pair_is_labeled_confirmed() -> None:
    opp = as_opportunity(calculate_direction(make_inputs(), ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.label is OpportunityLabel.CONFIRMED


def test_suspended_pair_is_never_confirmed() -> None:
    inputs = make_inputs(pair=approved_pair(status=MatchStatus.SUSPENDED, manually_approved=False))
    opp = as_opportunity(calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert opp.label is OpportunityLabel.RESEARCH_CANDIDATE


def test_risk_flags_are_carried_onto_the_opportunity() -> None:
    inputs = make_inputs()
    flagged = CalculationInputs(
        **{**inputs.__dict__, "risk_flags": (RiskFlag.NON_ATOMIC_EXECUTION,)}
    )
    opp = as_opportunity(calculate_direction(flagged, ArbDirection.A_KALSHI_YES_POLY_NO))
    assert RiskFlag.NON_ATOMIC_EXECUTION in opp.risk_flags


# ---------------------------------------------------------------------- properties


@given(
    kalshi_price=st.decimals(min_value=Decimal("0.05"), max_value=Decimal("0.94"), places=2),
    poly_price=st.decimals(min_value=Decimal("0.05"), max_value=Decimal("0.94"), places=2),
    quantity=st.decimals(min_value=Decimal("1"), max_value=Decimal("500"), places=0),
)
@hyp_settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_reported_profit_is_always_positive_and_fully_accounted(
    kalshi_price: Decimal, poly_price: Decimal, quantity: Decimal
) -> None:
    """Whatever the book, an emitted opportunity must satisfy its own accounting.

    This is the core invariant: if the engine reports an opportunity, net profit is
    strictly positive and equals payout minus every disclosed cost component.
    """
    inputs = make_inputs(
        kalshi_yes_asks=levels((str(kalshi_price), str(quantity))),
        poly_no_asks=levels((str(poly_price), str(quantity))),
        usdc_haircut=Decimal("0.10"),
        settings=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            slippage_buffer_pct=Decimal("0.5"),
            operational_cost_usd=Decimal("0.10"),
            max_quantity=Decimal("500"),
        ),
    )
    result = calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)
    if isinstance(result, RejectedOpportunity):
        return

    assert result.net_profit > Decimal("0")
    assert result.net_profit >= Decimal("0.50")
    assert result.return_on_capital >= Decimal("0.5")
    assert result.gross_acquisition_cost < result.expected_payout
    assert (
        result.expected_payout
        - result.gross_acquisition_cost
        - result.kalshi_fees
        - result.polymarket_fees
        - result.slippage_allowance
        - result.operational_cost_allowance
        - result.usdc_conversion_allowance
    ) == result.net_profit


@given(
    kalshi_price=st.decimals(min_value=Decimal("0.05"), max_value=Decimal("0.94"), places=2),
    poly_price=st.decimals(min_value=Decimal("0.05"), max_value=Decimal("0.94"), places=2),
)
@hyp_settings(max_examples=150)
def test_legs_are_always_hedged_at_equal_quantity(
    kalshi_price: Decimal, poly_price: Decimal
) -> None:
    """Unequal leg sizes are an unhedged bet, never an arbitrage."""
    inputs = make_inputs(
        kalshi_yes_asks=levels((str(kalshi_price), "137")),
        poly_no_asks=levels((str(poly_price), "89")),
    )
    result = calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)
    if isinstance(result, Opportunity):
        assert result.kalshi_leg.quantity == result.polymarket_leg.quantity
        assert result.kalshi_leg.quantity == result.executable_quantity


@pytest.mark.parametrize("price", ["0.01", "0.99"])
def test_extreme_prices_do_not_break_the_calculation(price: str) -> None:
    inputs = make_inputs(kalshi_yes_asks=levels((price, "100")))
    result = calculate_direction(inputs, ArbDirection.A_KALSHI_YES_POLY_NO)
    assert isinstance(result, (Opportunity, RejectedOpportunity))
