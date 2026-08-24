"""End-to-end Kalshi <-> Polymarket **US** on fixtures.

The headline demo previously ran on the Global corpus, so it proved the USD/USDC path and
said nothing about US behaviour. This proves the US path specifically.

**The pair here is deliberately synthetic and is not asserted as a real live mapping.**
It exists to exercise US settlement, fees, ticks and approval, not to claim two real
contracts are equivalent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import (
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import (
    ArbDirection,
    FeePolicy,
    MatchStatus,
    OpportunityLabel,
    PaperTradeStatus,
    RiskFlag,
    SettlementAsset,
    Venue,
    VenueProduct,
)
from event_arb.domain.keys import kalshi_key, polymarket_key
from event_arb.matching.approval import ApprovalError
from event_arb.paper.executor import settle
from event_arb.scanner import Scanner

KALSHI_FED = "KXFEDDECISION-26SEP-CUT25"
US_FED = "fed-cuts-25bps-september-2026"
US_NYC = "nyc-high-temp-aug-22-2026"


@pytest.fixture
def us_scanner(fixture_dir: Path) -> Scanner:
    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )
    return Scanner(settings)


async def prepared(scanner: Scanner) -> Scanner:
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books()
    return scanner


def find_pair(scanner: Scanner, polymarket_id: str) -> str:
    for pair_id, pair in scanner.state.pairs.items():
        if pair.polymarket_market_id == polymarket_id:
            return pair_id
    raise AssertionError(f"no pair generated for {polymarket_id}")


# ------------------------------------------------------- product selection


async def test_us_product_selects_the_us_fixture_corpus(us_scanner: Scanner) -> None:
    """Global market ids must not appear when the US product is configured."""
    await us_scanner.discover()
    polymarket_ids = {
        mid for (venue, _p, mid) in us_scanner.state.markets if venue is Venue.POLYMARKET
    }
    assert US_FED in polymarket_ids
    assert not any(mid.startswith("0x") for mid in polymarket_ids)


async def test_discovered_us_markets_carry_the_us_product(us_scanner: Scanner) -> None:
    await us_scanner.discover()
    market = us_scanner.state.markets[polymarket_key(VenueProduct.POLYMARKET_US, US_FED)]
    assert market.product is VenueProduct.POLYMARKET_US
    assert market.fee_config.fee_policy is FeePolicy.POLYMARKET_US_BANKERS_CENT


async def test_both_legs_settle_in_usd(us_scanner: Scanner) -> None:
    await us_scanner.discover()
    kalshi = us_scanner.state.markets[kalshi_key(KALSHI_FED)]
    polymarket = us_scanner.state.markets[polymarket_key(VenueProduct.POLYMARKET_US, US_FED)]
    assert kalshi.settlement_asset is SettlementAsset.USD
    assert polymarket.settlement_asset is SettlementAsset.USD


async def test_us_market_uses_its_own_fee_coefficient(us_scanner: Scanner) -> None:
    await us_scanner.discover()
    market = us_scanner.state.markets[polymarket_key(VenueProduct.POLYMARKET_US, US_FED)]
    assert market.fee_config.taker_rate == Decimal("0.06")
    assert market.fee_config.source == "market.feeCoefficient"


async def test_sub_penny_ticks_are_accepted_by_the_depth_walker(
    us_scanner: Scanner,
) -> None:
    """0.001 prices must fill; a penny tick would reject them as off-grid."""
    scanner = await prepared(us_scanner)
    book = scanner.store.get(polymarket_key(VenueProduct.POLYMARKET_US, US_FED))
    assert book is not None
    assert book.yes.asks[0].price == Decimal("0.380")
    market = scanner.state.markets[polymarket_key(VenueProduct.POLYMARKET_US, US_FED)]
    assert market.tick_size == Decimal("0.001")


# --------------------------------------------------- opportunity and paper


async def test_approved_us_pair_produces_a_confirmed_opportunity(
    us_scanner: Scanner,
) -> None:
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")

    result = scanner.scan()
    confirmed = [o for o in result.confirmed if o.pair_id == pair_id]
    assert len(confirmed) == 1
    opportunity = confirmed[0]

    assert opportunity.label is OpportunityLabel.CONFIRMED
    assert opportunity.executable_quantity > 0
    assert opportunity.net_profit > 0
    assert opportunity.kalshi_leg.settlement_asset is SettlementAsset.USD
    assert opportunity.polymarket_leg.settlement_asset is SettlementAsset.USD


async def test_no_usdc_haircut_or_basis_flag_on_a_us_pair(us_scanner: Scanner) -> None:
    """The whole economic point of the US product."""
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    opportunity = scanner.scan().confirmed[0]

    assert opportunity.usdc_conversion_allowance == Decimal("0.0000")
    assert RiskFlag.USD_USDC_BASIS not in opportunity.risk_flags


async def test_reported_polymarket_fee_uses_us_bankers_rounding(
    us_scanner: Scanner,
) -> None:
    """A cent-rounded fee; Global's five-decimal ceiling would not land here."""
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    opportunity = scanner.scan().confirmed[0]

    fee = opportunity.polymarket_fees
    assert fee > 0
    # Banker's rounding to the cent leaves at most two significant decimal places.
    assert fee == fee.quantize(Decimal("0.0001"))
    assert (fee * 100) % 1 == 0, f"US fee {fee} is not cent-rounded"


async def test_both_directions_are_evaluated(us_scanner: Scanner) -> None:
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    result = scanner.scan()

    seen = {o.direction for o in result.opportunities if o.pair_id == pair_id}
    seen |= {r.direction for r in result.rejections if r.pair_id == pair_id}
    assert seen == set(ArbDirection)


async def test_opportunity_becomes_a_paper_trade(us_scanner: Scanner) -> None:
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    opportunity = scanner.scan().confirmed[0]

    trade = await scanner.paper_trade(opportunity)
    assert trade.status is PaperTradeStatus.BOTH_LEGS_FILLED
    assert trade.hedged_quantity > 0
    assert trade.unhedged_quantity == Decimal("0")


# ------------------------------------------------------- settlement and export


async def test_settlement_reports_usd_on_both_legs_with_no_usdc_note(
    us_scanner: Scanner,
) -> None:
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    trade = await scanner.paper_trade(scanner.scan().confirmed[0])

    settle(trade, trade.opportunity.kalshi_leg.outcome)
    result = trade.settlement_result
    assert result is not None
    assert result["kalshi_settlement_asset"] == "USD"
    assert result["polymarket_settlement_asset"] == "USD"
    assert result["settlement_assets_differ"] is False
    assert "usdc_denominated_payout" not in result
    assert "no cross-asset conversion" in str(result["settlement_asset_note"])


async def test_export_remains_asset_correct(us_scanner: Scanner) -> None:
    import json

    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    trade = await scanner.paper_trade(scanner.scan().confirmed[0])
    settle(trade, trade.opportunity.kalshi_leg.outcome)

    payload = json.loads(scanner.portfolio.to_json())
    exported = payload["trades"][0]["settlement_result"]
    assert exported["polymarket_settlement_asset"] == "USD"
    assert "USDC" not in json.dumps(exported)


# --------------------------------------------------- the misleading US pair


async def test_materially_different_us_pair_stays_a_research_candidate(
    us_scanner: Scanner,
) -> None:
    """Automation must never promote a contradictory pair.

    The NYC fixture pairs an exclusive Kalshi threshold against an inclusive US one, with
    different grading sources. It looks attractive and must stay a research candidate.
    """
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_NYC)
    pair = scanner.state.pairs[pair_id]

    dimensions = {d.dimension for d in pair.rule_differences if d.is_material}
    assert "comparison_operator" in dimensions

    with pytest.raises(ApprovalError, match="not ready for approval"):
        scanner.approve(pair_id, approved_by="test@example.com")

    result = scanner.scan()
    for opportunity in result.opportunities:
        if opportunity.pair_id == pair_id:
            assert opportunity.label is OpportunityLabel.RESEARCH_CANDIDATE
            assert RiskFlag.UNAPPROVED_PAIR in opportunity.risk_flags


async def test_stale_books_suppress_us_opportunities(us_scanner: Scanner) -> None:
    scanner = await prepared(us_scanner)
    pair_id = find_pair(scanner, US_FED)
    scanner.approve(pair_id, approved_by="test@example.com")
    assert scanner.scan().confirmed

    future = datetime.now(UTC) + timedelta(minutes=10)
    assert scanner.scan(now=future).opportunities == []


async def test_untradable_us_markets_are_excluded(us_scanner: Scanner) -> None:
    """Markets whose rules text is insufficient cannot reach an opportunity."""
    await us_scanner.discover()
    thin = us_scanner.state.markets[
        polymarket_key(VenueProduct.POLYMARKET_US, "market-with-thin-rules")
    ]
    assert thin.is_tradable is False


async def test_seeded_and_generated_pairs_carry_the_us_product(
    us_scanner: Scanner,
) -> None:
    scanner = await prepared(us_scanner)
    for pair in scanner.state.pairs.values():
        assert pair.polymarket_product is VenueProduct.POLYMARKET_US
        assert pair.status is MatchStatus.SUGGESTED
