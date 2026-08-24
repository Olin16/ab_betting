"""Bounded candidate research.

A broad pull is the largest volume of venue traffic this project produces, so the bounds
are the feature. **No test here makes a live request.**
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from event_arb.candidates import (
    DiscoveryLimits,
    RequestPlan,
    build_request_plan,
    research_candidates,
    top_slugs_for_verification,
)
from event_arb.domain.enums import FeePolicy, MatchStatus, Venue, VenueProduct
from event_arb.domain.models import FeeConfig, MarketPair, VenueMarket
from event_arb.matching.candidates import pair_id_for

RULES = (
    "This market will resolve to Yes if the stated condition is met, as published by the "
    "official source named below. If the event is postponed the market resolves to No. "
    "Revisions issued after the initial publication do not change settlement."
)
CLOSE = datetime(2026, 9, 17, 18, tzinfo=UTC)


def kalshi_market(index: int) -> VenueMarket:
    return VenueMarket(
        venue=Venue.KALSHI,
        venue_market_id=f"KX-{index}",
        title=f"Will US CPI year-over-year for month {index} be above 3.0%?",
        rules_text=RULES,
        settlement_source="BLS CPI release",
        close_time=CLOSE,
        fee_config=FeeConfig(
            venue=Venue.KALSHI,
            taker_rate=Decimal("0.07"),
            fee_policy=FeePolicy.KALSHI_CENTICENT_CEIL,
        ),
    )


def us_market(index: int) -> VenueMarket:
    return VenueMarket(
        venue=Venue.POLYMARKET,
        venue_market_id=f"cpi-month-{index}",
        title=f"US CPI YoY above 3.0% for month {index}?",
        rules_text=RULES,
        settlement_source="BLS CPI release",
        close_time=CLOSE,
        product=VenueProduct.POLYMARKET_US,
        fee_config=FeeConfig(
            venue=Venue.POLYMARKET,
            taker_rate=Decimal("0.06"),
            fee_policy=FeePolicy.POLYMARKET_US_BANKERS_CENT,
        ),
    )


class _StubAdapter:
    """Minimal stand-in exposing what the planner reads off a real adapter."""

    def __init__(self, base_url: str, sends_credentials: bool = False) -> None:
        self.discovery_base_url = base_url
        self.sends_credentials = sends_credentials


def make_plan(**overrides: object) -> RequestPlan:
    kalshi = _StubAdapter(
        str(overrides.get("kalshi_url", "https://kalshi.test/v2")),
        bool(overrides.get("kalshi_auth", False)),
    )
    polymarket = _StubAdapter(
        str(overrides.get("polymarket_url", "https://pmus.test")),
        bool(overrides.get("polymarket_auth", False)),
    )
    product = overrides.get("product", VenueProduct.POLYMARKET_US)
    return build_request_plan(LIMITS, kalshi, polymarket, product)  # type: ignore[arg-type]


LIMITS = DiscoveryLimits(
    kalshi_limit=250,
    polymarket_limit=250,
    min_score=Decimal("0.55"),
    max_per_market=3,
    max_results=100,
    verify_top=25,
)


# ------------------------------------------------------------------ request plan


def test_plan_declares_both_bounded_pages_and_the_worst_case() -> None:
    built = make_plan()
    assert built.minimum_requests == 2
    assert built.worst_case_requests == 2 + LIMITS.verify_top


def test_plan_states_no_credentials_are_sent() -> None:
    rendered = make_plan().render()
    assert "credentials         : none" in rendered
    for entry in make_plan().entries:
        assert entry.sends_credentials is False


def test_plan_states_no_order_books_and_no_websockets() -> None:
    rendered = make_plan().render()
    assert "order books fetched : 0" in rendered
    assert "websockets opened   : 0" in rendered


def test_plan_states_pagination_is_not_followed() -> None:
    built = make_plan()
    assert "NOT followed" in built.pagination


def test_plan_bounds_local_comparisons_at_the_stated_scope() -> None:
    """250 x 250 = 62,500 local comparisons, costing no requests."""
    assert LIMITS.max_local_comparisons == 62_500
    assert "62,500" in make_plan().render()


# ---------------------------------------------------------------------- bounds


def test_results_are_capped() -> None:
    kalshi = [kalshi_market(i) for i in range(20)]
    polymarket = [us_market(i) for i in range(20)]
    limits = DiscoveryLimits(min_score=Decimal("0"), max_results=5, max_per_market=3)
    report = research_candidates(kalshi, polymarket, limits)
    assert report.candidates_retained == 5
    assert len(report.records) == 5


def test_candidates_per_market_are_capped() -> None:
    kalshi = [kalshi_market(0)]
    polymarket = [us_market(i) for i in range(10)]
    limits = DiscoveryLimits(min_score=Decimal("0"), max_per_market=2, max_results=100)
    report = research_candidates(kalshi, polymarket, limits)
    assert report.candidates_generated <= 2


def test_minimum_score_is_honoured() -> None:
    kalshi = [kalshi_market(0)]
    polymarket = [us_market(0)]
    strict = research_candidates(kalshi, polymarket, DiscoveryLimits(min_score=Decimal("0.99")))
    assert strict.candidates_generated == 0


def test_comparison_count_is_reported() -> None:
    report = research_candidates(
        [kalshi_market(i) for i in range(4)],
        [us_market(i) for i in range(5)],
        DiscoveryLimits(min_score=Decimal("0")),
    )
    assert report.comparisons == 20


def test_verification_shortlist_is_capped_and_ordered() -> None:
    report = research_candidates(
        [kalshi_market(i) for i in range(10)],
        [us_market(i) for i in range(10)],
        DiscoveryLimits(min_score=Decimal("0"), max_results=100),
    )
    slugs = top_slugs_for_verification(report, DiscoveryLimits(verify_top=3))
    assert len(slugs) <= 3
    assert len(set(slugs)) == len(slugs)


# ------------------------------------------------------- research never decides


def test_no_candidate_is_ever_approved() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    for record in report.records:
        assert record.pair.status is MatchStatus.SUGGESTED
        assert record.pair.manually_approved is False
        assert record.as_dict()["label"] == "research_candidate"


def test_report_states_it_took_no_trading_actions() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    payload = report.as_dict()
    assert payload["order_books_fetched"] == 0
    assert payload["auto_approved"] == 0
    assert payload["paper_trades"] == 0


@pytest.mark.parametrize(
    "status",
    [
        MatchStatus.APPROVED,
        MatchStatus.REJECTED,
        MatchStatus.SUSPENDED,
        MatchStatus.EXPERT_REVIEW,
        MatchStatus.UNDER_REVIEW,
    ],
)
def test_existing_human_dispositions_are_preserved(status: MatchStatus) -> None:
    """A research run must never overwrite a decision a person already made."""
    kalshi, polymarket = kalshi_market(0), us_market(0)
    pair_id = pair_id_for(kalshi.venue_market_id, polymarket.venue_market_id, polymarket.product)
    existing = {
        pair_id: MarketPair(
            id=pair_id,
            kalshi_market_id=kalshi.venue_market_id,
            polymarket_market_id=polymarket.venue_market_id,
            status=status,
            manually_approved=status is MatchStatus.APPROVED,
        )
    }
    report = research_candidates(
        [kalshi], [polymarket], DiscoveryLimits(min_score=Decimal("0")), existing_pairs=existing
    )
    assert report.preserved_dispositions == 1
    assert report.records == []


def test_a_previously_suggested_pair_is_refreshed() -> None:
    """Only human dispositions are protected; a stale suggestion may be regenerated."""
    kalshi, polymarket = kalshi_market(0), us_market(0)
    pair_id = pair_id_for(kalshi.venue_market_id, polymarket.venue_market_id, polymarket.product)
    existing = {
        pair_id: MarketPair(
            id=pair_id,
            kalshi_market_id=kalshi.venue_market_id,
            polymarket_market_id=polymarket.venue_market_id,
            status=MatchStatus.SUGGESTED,
        )
    }
    report = research_candidates(
        [kalshi], [polymarket], DiscoveryLimits(min_score=Decimal("0")), existing_pairs=existing
    )
    assert report.preserved_dispositions == 0
    assert report.candidates_retained == 1


# ------------------------------------------------------------------- readiness


def test_records_carry_readiness_so_reviewers_see_blockers() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    record = report.records[0]
    assert record.readiness.is_ready is True
    assert record.as_dict()["ready_for_approval"] is True


def test_unready_candidates_are_retained_but_flagged() -> None:
    """A blocked candidate is still worth showing a reviewer, clearly labelled."""
    kalshi = kalshi_market(0).model_copy(update={"settlement_source": ""})
    report = research_candidates([kalshi], [us_market(0)], DiscoveryLimits(min_score=Decimal("0")))
    record = report.records[0]
    assert record.readiness.is_ready is False
    blockers = record.as_dict()["blockers"]
    assert isinstance(blockers, list | tuple)
    assert "kalshi_settlement_source_unknown" in blockers
    assert report.ready_for_approval == 0


def test_retained_record_excludes_raw_response_bodies() -> None:
    """Only what a reviewer needs is persisted; whole catalogues stay in memory."""
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    payload = report.records[0].as_dict()
    assert "raw_metadata" not in payload
    assert "payload" not in payload


def test_scope_matches_the_documented_initial_test_bounds() -> None:
    """250 per venue, 62,500 comparisons, 0.55, 3 per market, 100 kept, top 25 verified."""
    assert LIMITS.kalshi_limit == 250
    assert LIMITS.polymarket_limit == 250
    assert LIMITS.max_local_comparisons == 62_500
    assert LIMITS.min_score == Decimal("0.55")
    assert LIMITS.max_per_market == 3
    assert LIMITS.max_results == 100
    assert LIMITS.verify_top == 25


# ------------------------------------------------- verification and cursors


def test_verified_defaults_to_false_until_re_fetched() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    assert report.records[0].verified is False
    assert report.verified == 0


def test_a_record_can_be_marked_verified() -> None:
    """The CLI sets this after re-fetching; the field is not decorative."""
    from dataclasses import replace

    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    marked = replace(report.records[0], verified=True)
    assert marked.verified is True
    assert marked.as_dict()["targeted_reverify"] is True


def test_cursor_count_is_reported_in_the_summary() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    report.pagination_cursors_ignored = 2
    assert report.as_dict()["pagination_cursors_ignored"] == 2
