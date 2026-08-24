"""Approval readiness: can this pair be approved at all?

Readiness is not similarity. A high score says two markets look alike; readiness says we
hold enough information to claim they *settle* alike.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from event_arb.domain.enums import FeePolicy, MatchStatus, Venue, VenueProduct
from event_arb.domain.models import FeeConfig, MarketPair, RuleDifference, VenueMarket
from event_arb.matching.approval import ApprovalError, approve_pair, record_expert_review
from event_arb.matching.readiness import ReadinessBlocker, assess_readiness

RULES = (
    "This market will resolve to Yes if the stated condition is met, as published by the "
    "official source named below. If the event is postponed the market resolves to No. "
    "Revisions issued after the initial publication do not change settlement."
)
CLOSE = datetime(2026, 9, 17, 18, tzinfo=UTC)


def kalshi(**overrides: object) -> VenueMarket:
    base: dict[str, object] = {
        "venue": Venue.KALSHI,
        "venue_market_id": "K",
        "title": "T",
        "rules_text": RULES,
        "settlement_source": "FOMC statement",
        "close_time": CLOSE,
        "fee_config": FeeConfig(
            venue=Venue.KALSHI,
            taker_rate=Decimal("0.07"),
            fee_policy=FeePolicy.KALSHI_CENTICENT_CEIL,
        ),
    }
    return VenueMarket(**{**base, **overrides})  # type: ignore[arg-type]


def polymarket(**overrides: object) -> VenueMarket:
    base: dict[str, object] = {
        "venue": Venue.POLYMARKET,
        "venue_market_id": "P",
        "title": "T",
        "rules_text": RULES,
        "settlement_source": "FOMC statement",
        "close_time": CLOSE,
        "product": VenueProduct.POLYMARKET_US,
        "fee_config": FeeConfig(
            venue=Venue.POLYMARKET,
            taker_rate=Decimal("0.06"),
            fee_policy=FeePolicy.POLYMARKET_US_BANKERS_CENT,
        ),
    }
    return VenueMarket(**{**base, **overrides})  # type: ignore[arg-type]


def pair(**overrides: object) -> MarketPair:
    base: dict[str, object] = {
        "id": "p1",
        "kalshi_market_id": "K",
        "polymarket_market_id": "P",
        "polymarket_product": VenueProduct.POLYMARKET_US,
    }
    return MarketPair(**{**base, **overrides})  # type: ignore[arg-type]


def blockers(report: object) -> set[ReadinessBlocker]:
    return {code for code, _ in report.blockers}  # type: ignore[attr-defined]


# ------------------------------------------------------------------ the happy path


def test_a_complete_pair_is_ready() -> None:
    report = assess_readiness(pair(), kalshi(), polymarket())
    assert report.is_ready is True
    assert report.blockers == ()


def test_a_ready_pair_can_be_approved() -> None:
    approved = approve_pair(pair(), kalshi(), polymarket(), approved_by="analyst")
    assert approved.is_tradable_pair is True


# ---------------------------------------------------- the empty-source case


def test_two_empty_settlement_sources_are_not_agreement() -> None:
    """The specific hole this assessment closes.

    ``compare_rules`` finds no *difference* when both sources are empty, because empty
    equals empty. But two unknowns are not a verified match, and approving on that basis
    asserts an equivalence nobody established.
    """
    from event_arb.matching.rules_diff import compare_rules

    k = kalshi(settlement_source="")
    p = polymarket(settlement_source="")

    dimensions = {d.dimension for d in compare_rules(k, p).differences}
    assert "settlement_source" not in dimensions, "no difference is reported, as before"

    report = assess_readiness(pair(), k, p)
    assert report.is_ready is False
    assert ReadinessBlocker.KALSHI_SETTLEMENT_SOURCE_UNKNOWN in blockers(report)
    assert ReadinessBlocker.POLYMARKET_SETTLEMENT_SOURCE_UNKNOWN in blockers(report)


def test_empty_sources_block_approval_outright() -> None:
    with pytest.raises(ApprovalError, match="settlement_source_unknown"):
        approve_pair(
            pair(),
            kalshi(settlement_source=""),
            polymarket(settlement_source=""),
            approved_by="analyst",
        )


def test_a_reviewer_may_record_sources_obtained_separately() -> None:
    approved = approve_pair(
        pair(),
        kalshi(settlement_source=""),
        polymarket(settlement_source=""),
        approved_by="analyst",
        kalshi_source_override="Federal Reserve press release, checked manually",
        polymarket_source_override="Federal Reserve press release, checked manually",
    )
    assert approved.is_tradable_pair is True


def test_an_override_for_one_leg_only_is_not_enough() -> None:
    """Per leg, because one known source and one unknown is still an unknown."""
    with pytest.raises(ApprovalError, match="polymarket_settlement_source_unknown"):
        approve_pair(
            pair(),
            kalshi(settlement_source=""),
            polymarket(settlement_source=""),
            approved_by="analyst",
            kalshi_source_override="checked manually",
        )


# --------------------------------------------------------------- other blockers


def test_insufficient_rules_block_approval() -> None:
    report = assess_readiness(pair(), kalshi(rules_text="Resolves Yes."), polymarket())
    assert ReadinessBlocker.KALSHI_RULES_INSUFFICIENT in blockers(report)


def test_missing_close_time_blocks_approval() -> None:
    report = assess_readiness(pair(), kalshi(close_time=None), polymarket())
    assert ReadinessBlocker.KALSHI_CLOSE_TIME_MISSING in blockers(report)
    assert "kalshi close time" in report.missing_information


def test_missing_product_identity_blocks_approval() -> None:
    report = assess_readiness(pair(), kalshi(), polymarket(product=VenueProduct.NONE))
    assert ReadinessBlocker.PRODUCT_IDENTITY_MISSING in blockers(report)


def test_mismatched_product_identity_blocks_approval() -> None:
    report = assess_readiness(
        pair(polymarket_product=VenueProduct.POLYMARKET_GLOBAL), kalshi(), polymarket()
    )
    assert ReadinessBlocker.PRODUCT_IDENTITY_MISMATCHED in blockers(report)


def test_unresolved_outcome_identity_blocks_approval() -> None:
    report = assess_readiness(pair(), kalshi(), polymarket(outcomes=("Yes",)))
    assert ReadinessBlocker.OUTCOME_IDENTITY_UNRESOLVED in blockers(report)


def test_required_settlement_deadline_blocks_when_unknown() -> None:
    report = assess_readiness(pair(), kalshi(), polymarket(), settlement_deadline_required=True)
    assert ReadinessBlocker.SETTLEMENT_DEADLINE_UNKNOWN in blockers(report)


def test_known_settlement_deadlines_satisfy_the_requirement() -> None:
    deadline = datetime(2026, 10, 1, tzinfo=UTC)
    report = assess_readiness(
        pair(),
        kalshi(settlement_deadline=deadline),
        polymarket(settlement_deadline=deadline),
        settlement_deadline_required=True,
    )
    assert report.is_ready is True


@pytest.mark.parametrize(
    "dimension",
    [
        "threshold",
        "comparison_operator",
        "settlement_source",
        "revision_policy",
        "postponement",
        "invalid_outcome",
    ],
)
def test_contradictory_dimensions_block_approval(dimension: str) -> None:
    """These can settle the legs to opposite outcomes, so equivalence is simply false."""
    difference = RuleDifference(
        dimension=dimension,
        kalshi_value="a",
        polymarket_value="b",
        is_material=True,
        explanation="test",
    )
    report = assess_readiness(pair(), kalshi(), polymarket(), differences=(difference,))
    assert ReadinessBlocker.CONTRADICTORY_DIFFERENCE in blockers(report)
    assert dimension in report.contradictory_dimensions


def test_a_non_contradictory_material_difference_does_not_block() -> None:
    """Not every material difference makes the legs settle oppositely."""
    difference = RuleDifference(
        dimension="close_time",
        kalshi_value="a",
        polymarket_value="b",
        is_material=True,
        explanation="test",
    )
    report = assess_readiness(pair(), kalshi(), polymarket(), differences=(difference,))
    assert report.is_ready is True


# ------------------------------------------------------------- expert review


def test_expert_review_never_produces_a_tradable_pair() -> None:
    """An exception must not be able to masquerade as settlement equivalence."""
    reviewed = record_expert_review(
        pair(),
        kalshi(),
        polymarket(),
        reviewed_by="expert@example.com",
        justification=(
            "Documented exception: the two rulebooks differ on wording only, confirmed "
            "with both venues in writing on 2026-08-23."
        ),
    )
    assert reviewed.status is MatchStatus.EXPERT_REVIEW
    assert reviewed.manually_approved is False
    assert reviewed.is_tradable_pair is False
    assert reviewed.label.value == "research_candidate"


def test_expert_review_requires_a_substantive_justification() -> None:
    with pytest.raises(ApprovalError, match="substantive written justification"):
        record_expert_review(
            pair(), kalshi(), polymarket(), reviewed_by="e@example.com", justification="ok"
        )


def test_expert_review_requires_an_identified_reviewer() -> None:
    with pytest.raises(ApprovalError, match="identified reviewer"):
        record_expert_review(
            pair(), kalshi(), polymarket(), reviewed_by="  ", justification="x" * 60
        )


def test_readiness_report_serializes_for_the_dashboard() -> None:
    report = assess_readiness(pair(), kalshi(settlement_source=""), polymarket())
    payload = report.as_dict()
    assert payload["ready_for_approval"] is False
    assert isinstance(payload["blockers"], list)
    assert payload["missing_information"] == ["kalshi settlement source"]
