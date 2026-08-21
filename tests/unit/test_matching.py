"""Market matching: normalization, candidate scoring, rules diff, approval lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from event_arb.domain.enums import (
    ComparisonOperator,
    MatchStatus,
    OpportunityLabel,
    SettlementAsset,
    Venue,
)
from event_arb.domain.models import FeeConfig, MarketPair, VenueMarket
from event_arb.matching.approval import (
    ApprovalError,
    approve_pair,
    check_for_rules_change,
    enforce_rules_integrity,
    reject_pair,
    suspend_pair,
)
from event_arb.matching.candidates import generate_candidates, pair_id_for, score_pair
from event_arb.matching.normalizer import (
    NormalizedMarket,
    extract_dates,
    extract_threshold,
    normalize_text,
)
from event_arb.matching.rules_diff import compare_rules, detect_revision_policy

FEE = FeeConfig(venue=Venue.KALSHI, taker_rate=Decimal("0.07"), source="test")


def market(
    venue: Venue,
    market_id: str,
    title: str,
    rules: str,
    source: str = "",
    close: datetime | None = None,
) -> VenueMarket:
    return VenueMarket(
        venue=venue,
        venue_market_id=market_id,
        title=title,
        rules_text=rules,
        settlement_source=source,
        close_time=close or datetime(2026, 8, 23, 4, 0, tzinfo=UTC),
        fee_config=FEE,
    )


# ------------------------------------------------------------------------ normalizer


def test_normalize_text_strips_punctuation_and_case() -> None:
    assert normalize_text("Will the Fed CUT rates?!") == "will the fed cut rates"


@pytest.mark.parametrize(
    ("text", "expected_op", "expected_value"),
    [
        ("above 90F", ComparisonOperator.GT, Decimal("90")),
        ("90F or higher", ComparisonOperator.GTE, Decimal("90")),
        ("at least 3.0%", ComparisonOperator.GTE, Decimal("3.0")),
        ("strictly greater than 90 degrees", ComparisonOperator.GT, Decimal("90")),
        ("below 2.5%", ComparisonOperator.LT, Decimal("2.5")),
        ("no more than 4", ComparisonOperator.LTE, Decimal("4")),
        ("exactly 25 bps", ComparisonOperator.EQ, Decimal("25")),
    ],
)
def test_extract_threshold(
    text: str, expected_op: ComparisonOperator, expected_value: Decimal
) -> None:
    value, operator, _unit = extract_threshold(text)
    assert operator is expected_op
    assert value == expected_value


def test_inclusive_and_exclusive_operators_are_distinguished() -> None:
    """>= 90 and > 90 disagree at exactly 90. Conflating them is a settlement error."""
    _, gte, _ = extract_threshold("90 or higher")
    _, gt, _ = extract_threshold("above 90")
    assert gte is not gt
    assert gte is not None and gt is not None
    assert gte.is_inclusive and not gt.is_inclusive


def test_extract_threshold_returns_none_when_uncertain() -> None:
    assert extract_threshold("Who will win the election?") == (None, None, None)


def test_extract_dates_iso_and_text() -> None:
    assert date_of(extract_dates("resolves on 2026-09-17")) == (2026, 9, 17)
    assert date_of(extract_dates("on August 22, 2026 the high")) == (2026, 8, 22)


def date_of(dates: frozenset[object]) -> tuple[int, int, int]:
    only = next(iter(dates))
    assert hasattr(only, "year")
    return (only.year, only.month, only.day)  # type: ignore[attr-defined]


def test_dates_without_a_year_are_ignored_not_guessed() -> None:
    """Assuming the current year could pair a 2026 contract with a 2027 one."""
    assert extract_dates("on August 22 the high temperature") == frozenset()


# ------------------------------------------------------------------------- candidates


def test_identical_questions_score_highly() -> None:
    k = NormalizedMarket.from_market("K", "US CPI year-over-year above 3.0% for August 2026")
    p = NormalizedMarket.from_market("P", "US CPI YoY above 3.0% for August 2026")
    assert score_pair(k, p).score > Decimal("0.7")


def test_different_thresholds_are_heavily_penalized() -> None:
    """Near-identical wording with a different number must not look like a match."""
    k = NormalizedMarket.from_market("K", "Will NYC high be above 90F on 2026-08-22?")
    p = NormalizedMarket.from_market("P", "Will NYC high be above 95F on 2026-08-22?")
    score = score_pair(k, p)
    assert score.thresholds_agree is False
    assert score.score < Decimal("0.4")


def test_different_dates_are_heavily_penalized() -> None:
    k = NormalizedMarket.from_market("K", "Will NYC high be above 90F on 2026-08-22?")
    p = NormalizedMarket.from_market("P", "Will NYC high be above 90F on 2026-08-29?")
    score = score_pair(k, p)
    assert score.dates_agree is False
    assert score.score < Decimal("0.4")


def test_pair_id_is_deterministic_and_order_sensitive() -> None:
    assert pair_id_for("K", "P") == pair_id_for("K", "P")
    assert pair_id_for("K", "P") != pair_id_for("P", "K")


def test_generated_candidates_are_never_pre_approved() -> None:
    """Machine matching proposes; only a human disposes."""
    k = [market(Venue.KALSHI, "K1", "CPI above 3.0% for August 2026", "rules")]
    p = [market(Venue.POLYMARKET, "P1", "CPI above 3.0% for August 2026", "rules")]
    candidates = generate_candidates(k, p, min_score=Decimal("0"))
    assert candidates
    for pair, _score, _diff in candidates:
        assert pair.status is MatchStatus.SUGGESTED
        assert pair.manually_approved is False
        assert pair.label is OpportunityLabel.RESEARCH_CANDIDATE


# ------------------------------------------------------------------------ rules diff


def test_inclusive_vs_exclusive_threshold_is_material() -> None:
    """The NYC fixture case: '> 90' on Kalshi vs '>= 90' on Polymarket."""
    k = market(
        Venue.KALSHI,
        "K",
        "Will the high in NYC be above 90F on 2026-08-22?",
        "If the maximum temperature is strictly greater than 90 degrees Fahrenheit then Yes.",
        "NWS Central Park",
    )
    p = market(
        Venue.POLYMARKET,
        "P",
        "Will NYC hit 90F or higher on August 22, 2026?",
        "Resolves Yes if the maximum temperature is greater than or equal to 90 degrees.",
        "NOAA final monthly climate report",
    )
    comparison = compare_rules(k, p)
    dimensions = {d.dimension for d in comparison.material}
    assert "comparison_operator" in dimensions
    assert comparison.has_material


def test_different_settlement_source_is_material() -> None:
    k = market(Venue.KALSHI, "K", "Same title", "rules", "NWS preliminary daily report")
    p = market(Venue.POLYMARKET, "P", "Same title", "rules", "NOAA final monthly report")
    dimensions = {d.dimension for d in compare_rules(k, p).material}
    assert "settlement_source" in dimensions


def test_revision_policy_difference_is_detected() -> None:
    k_rules = (
        "Settlement uses the preliminary daily report. Later corrections do not change settlement."
    )
    p_rules = "Resolution uses the final corrected monthly climate report published by NOAA."
    assert detect_revision_policy(k_rules) != detect_revision_policy(p_rules)


def test_mismatched_close_times_are_material() -> None:
    k = market(Venue.KALSHI, "K", "T", "r", "src", datetime(2026, 8, 23, 4, 0, tzinfo=UTC))
    p = market(
        Venue.POLYMARKET,
        "P",
        "T",
        "r",
        "src",
        datetime(2026, 8, 23, 4, 0, tzinfo=UTC) + timedelta(hours=6),
    )
    dimensions = {d.dimension for d in compare_rules(k, p).material}
    assert "close_time" in dimensions


def test_truly_equivalent_markets_have_no_material_differences() -> None:
    rules = (
        "Resolves Yes if the year-over-year change in CPI for All Urban Consumers for "
        "August 2026, as first published by the Bureau of Labor Statistics, is strictly "
        "greater than 3.0 percent. Subsequent revisions are ignored."
    )
    close = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    k = market(Venue.KALSHI, "K", "US CPI YoY above 3.0% for August 2026", rules, "BLS CPI", close)
    p = market(
        Venue.POLYMARKET, "P", "US CPI YoY above 3.0% for August 2026", rules, "BLS CPI", close
    )
    comparison = compare_rules(k, p)
    assert not comparison.has_material, [d.dimension for d in comparison.material]


def test_usd_vs_usdc_is_flagged_but_not_material() -> None:
    """Different settlement assets are expected here; they are priced, not disqualifying."""
    k = market(Venue.KALSHI, "K", "T", "r", "src")
    p = market(Venue.POLYMARKET, "P", "T", "r", "src").model_copy(
        update={"settlement_asset": SettlementAsset.USDC}
    )
    asset_diffs = [d for d in compare_rules(k, p).differences if d.dimension == "settlement_asset"]
    assert asset_diffs and asset_diffs[0].is_material is False


# --------------------------------------------------------------------------- approval


def _pair(**overrides: object) -> MarketPair:
    base = {"id": "pair1", "kalshi_market_id": "K", "polymarket_market_id": "P"}
    return MarketPair(**{**base, **overrides})  # type: ignore[arg-type]


def test_approval_requires_a_named_reviewer() -> None:
    k, p = market(Venue.KALSHI, "K", "T", "r"), market(Venue.POLYMARKET, "P", "T", "r")
    with pytest.raises(ApprovalError):
        approve_pair(_pair(), k, p, approved_by="  ")


def test_material_differences_block_unacknowledged_approval() -> None:
    """A reviewer cannot approve past a settlement-changing difference by reflex."""
    k, p = market(Venue.KALSHI, "K", "T", "r"), market(Venue.POLYMARKET, "P", "T", "r")
    comparison = compare_rules(
        market(Venue.KALSHI, "K", "above 90F on 2026-08-22", "r", "NWS"),
        market(Venue.POLYMARKET, "P", "90F or higher on 2026-08-22", "r", "NOAA"),
    )
    pair = _pair(rule_differences=comparison.differences)
    with pytest.raises(ApprovalError, match="material rule differences"):
        approve_pair(pair, k, p, approved_by="analyst@example.com")

    approved = approve_pair(
        pair,
        k,
        p,
        approved_by="analyst@example.com",
        acknowledged_material_differences=True,
    )
    assert approved.is_tradable_pair is True


def test_approval_records_rules_hashes_and_label() -> None:
    k, p = market(Venue.KALSHI, "K", "T", "rules A"), market(Venue.POLYMARKET, "P", "T", "rules B")
    approved = approve_pair(_pair(), k, p, approved_by="analyst")
    assert approved.kalshi_rules_hash_at_approval == k.rules_hash
    assert approved.polymarket_rules_hash_at_approval == p.rules_hash
    assert approved.label is OpportunityLabel.CONFIRMED
    assert approved.approved_at is not None


def test_rules_change_after_approval_auto_suspends_the_pair() -> None:
    """The core safety behaviour: a venue rewriting its rules invalidates the approval."""
    k = market(Venue.KALSHI, "K", "T", "Resolves Yes above 90F.")
    p = market(Venue.POLYMARKET, "P", "T", "Resolves Yes above 90F.")
    approved = approve_pair(_pair(), k, p, approved_by="analyst")
    assert approved.is_tradable_pair is True

    edited = k.model_copy(update={"rules_text": "Resolves Yes at 90F or higher."})
    check = check_for_rules_change(approved, edited, p)
    assert check.changed and check.kalshi_changed and not check.polymarket_changed

    enforced = enforce_rules_integrity(approved, edited, p)
    assert enforced.status is MatchStatus.SUSPENDED
    assert enforced.manually_approved is False
    assert enforced.is_tradable_pair is False
    assert enforced.label is OpportunityLabel.RESEARCH_CANDIDATE


def test_cosmetic_whitespace_change_does_not_suspend() -> None:
    """Reformatting is not a rules change; churn here would train reviewers to ignore it."""
    k = market(Venue.KALSHI, "K", "T", "Resolves Yes above 90F.")
    p = market(Venue.POLYMARKET, "P", "T", "rules")
    approved = approve_pair(_pair(), k, p, approved_by="analyst")
    reformatted = k.model_copy(update={"rules_text": "Resolves   Yes\n above 90F."})
    assert check_for_rules_change(approved, reformatted, p).changed is False


def test_unapproved_pair_is_not_checked_for_rules_change() -> None:
    k, p = market(Venue.KALSHI, "K", "T", "r"), market(Venue.POLYMARKET, "P", "T", "r")
    assert check_for_rules_change(_pair(), k, p).changed is False


def test_suspended_pair_cannot_be_tradable() -> None:
    k, p = market(Venue.KALSHI, "K", "T", "r"), market(Venue.POLYMARKET, "P", "T", "r")
    approved = approve_pair(_pair(), k, p, approved_by="analyst")
    assert suspend_pair(approved, "test").is_tradable_pair is False


def test_rejected_pair_cannot_be_approved_directly() -> None:
    k, p = market(Venue.KALSHI, "K", "T", "r"), market(Venue.POLYMARKET, "P", "T", "r")
    rejected = reject_pair(_pair(), "not equivalent", "analyst")
    with pytest.raises(ApprovalError):
        approve_pair(rejected, k, p, approved_by="analyst")


# ---------------------------------------------- revision-policy equivalence classes


@pytest.mark.parametrize(
    "text",
    [
        "Settlement is based on the initial release only. Subsequent revisions are ignored.",
        "as first published by the BLS. Only the initial release is used.",
        "Uses the preliminary daily report. Later corrections do not change settlement.",
        "Revisions issued after the initial statement do not change settlement.",
    ],
)
def test_first_print_policies_all_classify_the_same(text: str) -> None:
    """Equivalent policies phrased differently must not read as a difference.

    False positives here are not harmless: a reviewer who sees spurious 'material'
    warnings learns to click past the real ones.
    """
    assert detect_revision_policy(text) == "first print governs"


@pytest.mark.parametrize(
    "text",
    [
        "Resolution uses the final corrected monthly climate report published by NOAA.",
        "The revised value is used for settlement.",
        "Uses the finalized report.",
    ],
)
def test_revised_value_policies_classify_together(text: str) -> None:
    assert detect_revision_policy(text) == "final/revised value governs"


def test_revised_policy_wins_over_an_incidental_preliminary_mention() -> None:
    """'...the final corrected report, not the preliminary one' means revised, not first-print."""
    text = (
        "Resolution uses the final corrected monthly climate report published by NOAA, "
        "not the preliminary daily report."
    )
    assert detect_revision_policy(text) == "final/revised value governs"


def test_unstated_revision_policy_is_none_not_a_guess() -> None:
    assert detect_revision_policy("Resolves Yes if the team wins.") is None


def test_equivalent_cpi_markets_have_no_material_revision_difference() -> None:
    """The CPI fixture pair: same policy, different wording, so no material difference."""
    k = market(
        Venue.KALSHI,
        "K",
        "US CPI YoY above 3.0% for August 2026",
        "Settlement is based on the initial release only. Subsequent revisions are ignored.",
        "BLS CPI release",
        datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
    )
    p = market(
        Venue.POLYMARKET,
        "P",
        "US CPI YoY above 3.0% for August 2026",
        "As first published by the Bureau of Labor Statistics. Only the initial release is used.",
        "BLS CPI release",
        datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
    )
    dimensions = {d.dimension for d in compare_rules(k, p).material}
    assert "revision_policy" not in dimensions


# ---------------------------------------------------- threshold extraction pitfalls


def test_year_over_year_is_not_a_comparison_operator() -> None:
    """The 'over' in 'year-over-year' is an idiom, not a '>' comparison."""
    value, operator, unit = extract_threshold(
        "Will US CPI year-over-year for August 2026 be above 3.0%?"
    )
    assert (value, operator, unit) == (Decimal("3.0"), ComparisonOperator.GT, "%")


def test_a_bare_year_is_not_mistaken_for_a_threshold() -> None:
    """In 'above 3.0% for August 2026', 2026 is a date, not the contract threshold."""
    value, _, _ = extract_threshold("above 3.0% for August 2026")
    assert value == Decimal("3.0")


def test_trailing_operator_takes_the_preceding_number() -> None:
    value, operator, _ = extract_threshold("Will NYC hit 90F or higher on August 22, 2026?")
    assert value == Decimal("90")
    assert operator is ComparisonOperator.GTE
