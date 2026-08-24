"""Freshness derived from measured refresh cost, not a guessed constant."""

from __future__ import annotations

import pytest

from event_arb.config import (
    OpportunitySettings,
    Settings,
    TrialSettings,
    WatchedPair,
)
from event_arb.orderbooks.freshness import RefreshTiming


def watched(count: int) -> list[WatchedPair]:
    return [
        WatchedPair(kalshi_market_id=f"K{i}", polymarket_market_id=f"P{i}") for i in range(count)
    ]


# ----------------------------------------------------------- measured timing


def test_timing_starts_empty() -> None:
    timing = RefreshTiming()
    assert timing.batches_observed == 0
    assert timing.worst_batch_seconds == 0.0


def test_timing_tracks_the_worst_batch_not_the_last() -> None:
    """A bound must accommodate the slowest batch, not the luckiest one."""
    timing = RefreshTiming()
    timing.record(2.0, 4)
    timing.record(0.5, 4)
    assert timing.last_batch_seconds == 0.5
    assert timing.worst_batch_seconds == 2.0


def test_required_age_is_the_batch_plus_tolerance_not_a_whole_cycle() -> None:
    """Refresh immediately precedes scan, so the cycle interval is not part of book age.

    Including it demanded a bound several times larger than reality, which pushes an
    operator to raise max_book_age_ms — loosening the check that catches stale prices.
    """
    timing = RefreshTiming()
    timing.record(1.5, 6)
    assert timing.required_age_ms(clock_tolerance_seconds=0.5) == 2000


def test_a_measured_batch_to_scan_lag_supersedes_the_batch_alone() -> None:
    """The persist step between refresh and scan adds real age; measure it."""
    timing = RefreshTiming()
    timing.record(1.5, 6)
    timing.record_scan_lag(1.9)
    assert timing.worst_book_age_seconds == 1.9
    assert timing.required_age_ms(clock_tolerance_seconds=0.5) == 2400


def test_the_worst_lag_is_kept_not_the_most_recent() -> None:
    timing = RefreshTiming()
    timing.record(1.0, 4)
    timing.record_scan_lag(2.5)
    timing.record_scan_lag(1.1)
    assert timing.worst_batch_to_scan_seconds == 2.5
    assert timing.scans_observed == 2


# -------------------------------------------------------------- one pair


def test_one_pair_needs_a_small_budget() -> None:
    """Two markets refreshed serially; the bound should stay modest."""
    trial = TrialSettings(
        watched_pairs=watched(1),
        refresh_interval_seconds=5.0,
        scan_interval_seconds=5.0,
        assumed_request_seconds=0.4,
    )
    assert trial.estimated_batch_seconds() == pytest.approx(0.8)
    # 0.8s batch + 0.5s default clock tolerance. Previously 6300ms, which included a whole
    # 5s cycle that the refresh-then-scan ordering makes impossible.
    assert trial.required_book_age_ms() == 1300


def test_many_pairs_need_a_larger_budget() -> None:
    """Serial refresh means the bound must grow with the watched set."""
    one = TrialSettings(watched_pairs=watched(1)).required_book_age_ms()
    ten = TrialSettings(watched_pairs=watched(10)).required_book_age_ms()
    assert ten > one


def test_a_slow_response_raises_the_requirement() -> None:
    trial = TrialSettings(watched_pairs=watched(1))
    fast = trial.required_book_age_ms(observed_batch_seconds=0.2)
    slow = trial.required_book_age_ms(observed_batch_seconds=6.0)
    assert slow > fast


# --------------------------------------------------------------- warnings


def test_a_budget_that_cannot_be_met_warns() -> None:
    settings = Settings(
        opportunity=OpportunitySettings(max_book_age_ms=1000),
        trial=TrialSettings(watched_pairs=watched(5)),
    )
    warning = settings.validate_freshness_budget()
    assert warning is not None
    assert "watch fewer pairs" in warning


def test_a_coherent_budget_does_not_warn() -> None:
    settings = Settings(
        opportunity=OpportunitySettings(max_book_age_ms=30_000),
        trial=TrialSettings(watched_pairs=watched(2)),
    )
    assert settings.validate_freshness_budget() is None


def test_the_warning_states_whether_it_is_measured_or_estimated() -> None:
    settings = Settings(
        opportunity=OpportunitySettings(max_book_age_ms=100),
        trial=TrialSettings(watched_pairs=watched(1)),
    )
    assert "estimated" in (settings.validate_freshness_budget() or "")
    assert "measured" in (settings.validate_freshness_budget(3.0) or "")


def test_measured_timing_can_reveal_a_budget_the_estimate_thought_fine() -> None:
    """The point of measuring: a slow venue invalidates a bound that looked adequate."""
    settings = Settings(
        opportunity=OpportunitySettings(max_book_age_ms=7000),
        trial=TrialSettings(watched_pairs=watched(1)),
    )
    assert settings.validate_freshness_budget() is None
    assert settings.validate_freshness_budget(observed_batch_seconds=10.0) is not None


def test_strict_maximum_age_is_still_configurable_and_enforced() -> None:
    """Measuring informs the bound; it never removes it."""
    settings = Settings(opportunity=OpportunitySettings(max_book_age_ms=1234))
    assert settings.opportunity.max_book_age_ms == 1234


def test_max_book_age_must_be_positive() -> None:
    with pytest.raises(ValueError):
        OpportunitySettings(max_book_age_ms=0)


def test_default_configuration_is_coherent_for_a_watched_trial() -> None:
    """A default trial with a couple of pairs should not warn out of the box."""
    settings = Settings(
        opportunity=OpportunitySettings(max_book_age_ms=8000),
        trial=TrialSettings(watched_pairs=watched(2)),
    )
    assert settings.validate_freshness_budget() is None
