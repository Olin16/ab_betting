"""End-to-end acceptance tests over the fixture venues.

These correspond directly to the project's acceptance criteria:

- synthetic venue feeds produce normalized order books;
- an approved fixture pair produces a depth-aware paper opportunity showing every cost
  component;
- a misleadingly similar fixture is rejected because its settlement rules differ;
- stale order books suppress opportunities;
- live trading remains impossible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from event_arb.config import (
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import (
    MatchStatus,
    OpportunityLabel,
    Outcome,
    PaperTradeStatus,
    RejectionReason,
    RiskFlag,
    Venue,
)
from event_arb.matching.approval import ApprovalError
from event_arb.scanner import Scanner
from tests.conftest import (
    KALSHI_CPI,
    KALSHI_FED,
    KALSHI_NYC,
    POLY_CPI,
    POLY_FED,
    POLY_NYC,
)


@pytest.fixture
def scanner(fixture_dir: object) -> Scanner:
    from pathlib import Path

    assert isinstance(fixture_dir, Path)
    settings = Settings(
        # Legacy Polymarket **Global** corpus. Stated explicitly so this path stays
        # clearly separated from the Polymarket US fixtures and tests.
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            slippage_buffer_pct=Decimal("0.5"),
            operational_cost_usd=Decimal("0.10"),
            max_quantity=Decimal("100"),
            max_book_age_ms=5000,
        ),
        paper=PaperSettings(adverse_fill_pct=Decimal("0")),
    )
    return Scanner(settings)


async def prepared(scanner: Scanner) -> Scanner:
    # Pairs before books: the refresh is scoped to markets referenced by watched pairs.
    await scanner.discover()
    scanner.seed_watched_pairs()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books()
    return scanner


def find_pair(scanner: Scanner, kalshi_id: str, polymarket_id: str) -> str:
    for pair_id, pair in scanner.state.pairs.items():
        if pair.kalshi_market_id == kalshi_id and pair.polymarket_market_id == polymarket_id:
            return pair_id
    raise AssertionError(f"no pair generated for {kalshi_id} <-> {polymarket_id}")


# ------------------------------------------------------- normalized feeds


async def test_fixture_feeds_produce_normalized_two_sided_books(scanner: Scanner) -> None:
    await prepared(scanner)
    kalshi = scanner.store.get(Venue.KALSHI, KALSHI_FED)
    polymarket = scanner.store.get(Venue.POLYMARKET, POLY_FED)
    assert kalshi is not None and polymarket is not None

    # Kalshi publishes bids only; asks must have been synthesized.
    assert kalshi.yes.asks and kalshi.no.asks
    assert kalshi.yes.asks[0].price == Decimal("0.55")
    assert kalshi.yes.asks[0].price + kalshi.no.bids[0].price == Decimal("1")
    assert polymarket.no.asks[0].price == Decimal("0.38")


async def test_discovery_finds_all_fixture_markets(scanner: Scanner) -> None:
    markets = await scanner.discover()
    assert len(markets) == 6
    assert (Venue.KALSHI, KALSHI_FED) in scanner.state.markets
    assert (Venue.POLYMARKET, POLY_FED) in scanner.state.markets


# ------------------------------------------------- approved pair -> opportunity


async def test_approved_pair_produces_a_depth_aware_opportunity(scanner: Scanner) -> None:
    """The headline acceptance criterion, with every cost component present."""
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_FED, POLY_FED)
    scanner.approve(pair_id, approved_by="test@example.com")

    result = scanner.scan()
    confirmed = [o for o in result.confirmed if o.pair_id == pair_id]
    assert len(confirmed) == 1
    opportunity = confirmed[0]

    assert opportunity.label is OpportunityLabel.CONFIRMED
    assert opportunity.executable_quantity == Decimal("100")
    assert opportunity.kalshi_leg.outcome is Outcome.YES
    assert opportunity.polymarket_leg.outcome is Outcome.NO

    # Depth-aware: VWAP comes from real ask levels, and quantity is bounded by the book.
    assert opportunity.kalshi_leg.vwap == Decimal("0.5500")
    assert opportunity.polymarket_leg.vwap == Decimal("0.3800")

    # Every cost component is reported, and they reconcile exactly.
    assert opportunity.gross_spread == Decimal("7.0000")
    assert opportunity.gross_acquisition_cost == Decimal("93.0000")
    assert opportunity.kalshi_fees > Decimal("0")
    assert opportunity.polymarket_fees > Decimal("0")
    assert opportunity.slippage_allowance > Decimal("0")
    assert opportunity.operational_cost_allowance == Decimal("0.1000")
    assert opportunity.usdc_conversion_allowance > Decimal("0")
    assert opportunity.net_profit > Decimal("0")
    assert opportunity.return_on_capital > Decimal("0")
    assert opportunity.detected_at.tzinfo is not None
    assert opportunity.kalshi_leg.book_age_ms >= 0

    # Risk flags are always attached, including the non-atomic warning.
    assert RiskFlag.NON_ATOMIC_EXECUTION in opportunity.risk_flags
    assert RiskFlag.USD_USDC_BASIS in opportunity.risk_flags


async def test_opportunity_becomes_a_paper_trade(scanner: Scanner) -> None:
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_FED, POLY_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    result = scanner.scan()

    trade = await scanner.paper_trade(result.confirmed[0])
    assert trade.status is PaperTradeStatus.BOTH_LEGS_FILLED
    assert trade.hedged_quantity == Decimal("100")
    assert trade.unhedged_quantity == Decimal("0")
    assert trade.realized_pnl_if_hedged > Decimal("0")
    assert scanner.portfolio.daily_report().trades == 1


# ------------------------------------------ misleading pair rejected on rules


async def test_misleadingly_similar_pair_is_flagged_and_cannot_be_approved(
    scanner: Scanner,
) -> None:
    """The NYC pair looks highly profitable but the two contracts settle differently.

    Kalshi: strictly above 90F, preliminary NWS daily report.
    Polymarket: 90F *or higher*, NOAA final corrected monthly report.

    Both the inclusive/exclusive boundary and the data source differ, so the pair must be
    surfaced as material and refused for approval without explicit acknowledgement.
    """
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_NYC, POLY_NYC)
    pair = scanner.state.pairs[pair_id]

    dimensions = {d.dimension for d in pair.rule_differences if d.is_material}
    assert "comparison_operator" in dimensions
    assert "settlement_source" in dimensions

    with pytest.raises(ApprovalError, match="material rule differences"):
        scanner.approve(pair_id, approved_by="test@example.com")


async def test_misleading_pair_is_only_ever_a_research_candidate(scanner: Scanner) -> None:
    """It shows a large apparent spread; it must never be labeled arbitrage."""
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_NYC, POLY_NYC)
    result = scanner.scan()

    matching = [o for o in result.opportunities if o.pair_id == pair_id]
    assert matching, "expected the misleading pair to surface as a research candidate"
    candidate = matching[0]
    assert candidate.net_profit > Decimal("10")  # deliberately attractive-looking
    assert candidate.label is OpportunityLabel.RESEARCH_CANDIDATE
    assert candidate.is_confirmed is False
    assert RiskFlag.RULES_DIFFERENCES_PRESENT in candidate.risk_flags
    assert RiskFlag.UNAPPROVED_PAIR in candidate.risk_flags


async def test_equivalent_pair_with_no_arbitrage_is_rejected_on_economics(
    scanner: Scanner,
) -> None:
    """The CPI pair is genuinely equivalent but offers no edge."""
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_CPI, POLY_CPI)
    scanner.approve(pair_id, approved_by="test@example.com")
    result = scanner.scan()

    assert not [o for o in result.opportunities if o.pair_id == pair_id]
    reasons = {r.reason for r in result.rejections if r.pair_id == pair_id}
    assert RejectionReason.NO_EXECUTABLE_QUANTITY in reasons


# ------------------------------------------------------------ staleness


async def test_stale_books_suppress_opportunities(scanner: Scanner) -> None:
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_FED, POLY_FED)
    scanner.approve(pair_id, approved_by="test@example.com")

    assert scanner.scan().confirmed, "sanity: fresh books should produce an opportunity"

    future = datetime.now(UTC) + timedelta(seconds=30)
    stale_result = scanner.scan(now=future)
    assert stale_result.opportunities == []
    assert any(r.reason is RejectionReason.BOOK_STALE for r in stale_result.rejections)


async def test_explicitly_stale_book_suppresses_opportunities(scanner: Scanner) -> None:
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_FED, POLY_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    scanner.store.mark_stale(Venue.POLYMARKET, POLY_FED)

    result = scanner.scan()
    assert not [o for o in result.confirmed if o.pair_id == pair_id]


# ------------------------------------------------------ rules-change suspension


async def test_rules_change_suspends_an_approved_pair_mid_session(scanner: Scanner) -> None:
    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_FED, POLY_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    assert scanner.scan().confirmed

    original = scanner.state.markets[(Venue.KALSHI, KALSHI_FED)]
    scanner.state.markets[(Venue.KALSHI, KALSHI_FED)] = original.model_copy(
        update={"rules_text": original.rules_text + " The threshold is now 50 basis points."}
    )

    result = scanner.scan()
    assert not [o for o in result.confirmed if o.pair_id == pair_id]
    assert scanner.state.pairs[pair_id].status is MatchStatus.SUSPENDED
    assert scanner.state.pairs[pair_id].is_tradable_pair is False


# ---------------------------------------------------------------- safety


async def test_live_trading_remains_impossible(scanner: Scanner) -> None:
    from event_arb.execution.coordinator import ExecutionCoordinator
    from event_arb.execution.disabled import LiveTradingDisabledError

    await prepared(scanner)
    pair_id = find_pair(scanner, KALSHI_FED, POLY_FED)
    pair = scanner.approve(pair_id, approved_by="test@example.com")
    opportunity = scanner.scan().confirmed[0]

    assert scanner.settings.live_trading_possible is False
    with pytest.raises(LiveTradingDisabledError):
        await ExecutionCoordinator().execute(opportunity, pair)
