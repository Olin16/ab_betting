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
    top_slugs_for_recheck,
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
KALSHI_FEES = FeeConfig(
    venue=Venue.KALSHI, taker_rate=Decimal("0.07"), fee_policy=FeePolicy.KALSHI_CENTICENT_CEIL
)
US_FEES = FeeConfig(
    venue=Venue.POLYMARKET,
    taker_rate=Decimal("0.06"),
    fee_policy=FeePolicy.POLYMARKET_US_BANKERS_CENT,
)


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
    recheck_top=25,
)


# ------------------------------------------------------------------ request plan


def test_plan_declares_both_bounded_pages_and_the_worst_case() -> None:
    built = make_plan()
    assert built.minimum_requests == 2
    assert built.worst_case_requests == 2 + LIMITS.recheck_top


def test_plan_states_no_credentials_are_sent() -> None:
    rendered = make_plan().render()
    assert "credentials         : none" in rendered
    for entry in make_plan().entries:
        assert entry.sends_credentials is False


def test_plan_states_no_order_books_and_no_websockets() -> None:
    rendered = make_plan().render()
    assert "order books fetched : 0" in rendered
    assert "websockets opened   : 0" in rendered


def test_smoke_plan_adds_two_bounded_book_rows() -> None:
    built = build_request_plan(
        LIMITS,
        _StubAdapter("https://external-api.kalshi.com/trade-api/v2"),
        _StubAdapter("https://gateway.polymarket.us"),
        VenueProduct.POLYMARKET_US,
        orderbook_pairs=2,
    )
    assert built.worst_case_requests == 31
    assert len(built.entries) == 5
    assert built.entries[-2].count == "0..2"
    assert built.entries[-1].count == "0..2"
    assert "order books fetched : 0..4" in built.render()


@pytest.mark.parametrize("pairs", [-1, 3])
def test_plan_refuses_more_than_two_book_pairs(pairs: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 2"):
        build_request_plan(
            LIMITS,
            _StubAdapter("https://kalshi.test"),
            _StubAdapter("https://polymarket.test"),
            VenueProduct.POLYMARKET_US,
            orderbook_pairs=pairs,
        )


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
    slugs = top_slugs_for_recheck(report, DiscoveryLimits(recheck_top=3))
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
    assert payload["approvals_created"] == 0
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


def test_live_shaped_titles_retrieve_at_the_production_threshold() -> None:
    """The bound this suite previously asserted as a number, exercised as behaviour.

    ``test_scope_matches_the_documented_initial_test_bounds`` pins ``min_score`` at 0.55,
    but every other test here passes ``Decimal("0")``, and the fixture end-to-end path runs
    at 0.4 — so nothing checked that 0.55 *retrieves anything* from markets shaped like the
    ones the venues actually return. The 2026-08-26 smoke test found that out live: two
    250-market catalogues, 62,500 comparisons, zero candidates, and it stopped needing two.

    These titles are deliberately venue-native — no explicit year, different phrasing per
    venue — which is what made prose date extraction come up empty on the real run.
    """
    close = datetime(2026, 9, 17, 18, tzinfo=UTC)
    kalshi = [
        VenueMarket(
            venue=Venue.KALSHI,
            venue_market_id="KXFEDDECISION-26SEP",
            title="Fed decision in September?",
            rules_text=RULES,
            settlement_source="FOMC statement",
            close_time=close,
            fee_config=KALSHI_FEES,
        ),
        VenueMarket(
            venue=Venue.KALSHI,
            venue_market_id="KXHIGHNY-26AUG22",
            title="NYC high temp on Aug 22?",
            rules_text=RULES,
            settlement_source="NWS observation",
            close_time=datetime(2026, 8, 23, 4, tzinfo=UTC),
            fee_config=KALSHI_FEES,
        ),
    ]
    polymarket = [
        VenueMarket(
            venue=Venue.POLYMARKET,
            venue_market_id="fed-cuts-september",
            title="Fed cuts rates in September?",
            rules_text=RULES,
            settlement_source="FOMC statement",
            close_time=close,
            product=VenueProduct.POLYMARKET_US,
            fee_config=US_FEES,
        ),
        VenueMarket(
            venue=Venue.POLYMARKET,
            venue_market_id="nyc-high-temp-august-22",
            title="Highest temperature in NYC on August 22?",
            rules_text=RULES,
            settlement_source="NWS observation",
            close_time=datetime(2026, 8, 23, 4, tzinfo=UTC),
            product=VenueProduct.POLYMARKET_US,
            fee_config=US_FEES,
        ),
    ]

    report = research_candidates(kalshi, polymarket, LIMITS)

    assert report.candidates_retained >= 2, (
        "the bounded smoke test needs two distinct plausible pairs; retrieval at "
        f"{LIMITS.min_score} produced {report.candidates_retained}"
    )
    matched = {(r.pair.kalshi_market_id, r.pair.polymarket_market_id) for r in report.records}
    assert ("KXFEDDECISION-26SEP", "fed-cuts-september") in matched
    assert ("KXHIGHNY-26AUG22", "nyc-high-temp-august-22") in matched


def test_scope_matches_the_documented_initial_test_bounds() -> None:
    """250 per venue, 62,500 comparisons, 0.55, 3 per market, 100 kept, top 25 verified."""
    assert LIMITS.kalshi_limit == 250
    assert LIMITS.polymarket_limit == 250
    assert LIMITS.max_local_comparisons == 62_500
    assert LIMITS.min_score == Decimal("0.55")
    assert LIMITS.max_per_market == 3
    assert LIMITS.max_results == 100
    assert LIMITS.recheck_top == 25


# ------------------------------------------------- verification and cursors


def test_existence_recheck_defaults_to_false_until_re_fetched() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    assert report.records[0].existence_rechecked is False
    assert report.records[0].existence_rechecked_at is None
    assert report.existence_rechecked == 0


def test_a_rechecked_record_states_exactly_what_was_checked() -> None:
    """It used to be called "verified", which claimed far more than it did."""
    from dataclasses import replace
    from datetime import UTC, datetime

    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    when = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    marked = replace(report.records[0], existence_rechecked=True, existence_rechecked_at=when)
    payload = marked.as_dict()

    assert payload["existence_rechecked"] is True
    assert payload["existence_rechecked_at"] == when.isoformat()
    assert payload["existence_recheck_scope"] == (
        "the venue still returns this market id; no metadata was re-read"
    )
    assert "verified" not in payload


def test_no_output_field_claims_verification() -> None:
    """A reviewer must not be able to read "verified" anywhere in this report."""
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    assert not any("verif" in key for key in report.as_dict())
    assert not any("verif" in key for key in report.records[0].as_dict())


def test_cursor_count_is_reported_in_the_summary() -> None:
    report = research_candidates(
        [kalshi_market(0)], [us_market(0)], DiscoveryLimits(min_score=Decimal("0"))
    )
    report.pagination_cursors_ignored = 2
    assert report.as_dict()["pagination_cursors_ignored"] == 2
