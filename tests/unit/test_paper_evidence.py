"""A hedge result is only evidence if the leg-2 book was really re-observed.

Before this, fixture replay re-read an unchanged file and reported a 100% hedge rate,
which reads exactly like a measured result and is not one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from event_arb.config import PaperSettings
from event_arb.paper.evidence import EvidenceGrade, grade_leg2
from event_arb.paper.portfolio import DailyReport


def test_only_a_refetch_that_moved_counts_as_measured() -> None:
    assert grade_leg2(refetched=True, changed=True, modelled=False) is EvidenceGrade.MEASURED


@pytest.mark.parametrize(
    ("refetched", "changed"),
    [(False, False), (False, True), (True, False)],
)
def test_anything_short_of_an_observed_move_is_not_measured(refetched: bool, changed: bool) -> None:
    """Never looking and looking at something that did not move are both non-evidence."""
    grade = grade_leg2(refetched=refetched, changed=changed, modelled=False)
    assert grade is EvidenceGrade.UNCHANGED_REREAD
    assert grade.supports_hedge_claims is False


def test_a_modelled_leg_is_never_promoted_to_measured() -> None:
    """A synthetic move describes the model, however real the re-fetch around it was."""
    assert grade_leg2(refetched=True, changed=True, modelled=True) is EvidenceGrade.MODELLED
    assert EvidenceGrade.MODELLED.supports_hedge_claims is False


def _report(**evidence: int) -> DailyReport:
    from datetime import date

    report = DailyReport(day=date(2026, 8, 24))
    report.evidence = dict(evidence)
    report.trades = sum(evidence.values())
    report.fully_hedged = report.trades
    return report


def test_a_perfect_hedge_rate_on_unmeasured_trades_says_so() -> None:
    report = _report(unchanged_reread=3)
    assert report.hedge_success_rate == Decimal("100.0000")
    assert report.hedge_rate_is_measured is False
    assert "does not show hedging surviving adverse movement" in report.evidence_caveat


def test_one_unmeasured_trade_taints_the_whole_rate() -> None:
    """A rate mixing measured and unmeasured trades cannot be quoted as measured."""
    report = _report(measured=9, unchanged_reread=1)
    assert report.hedge_rate_is_measured is False
    assert "1 of 10" in report.evidence_caveat


def test_a_fully_measured_run_may_claim_its_rate() -> None:
    report = _report(measured=4)
    assert report.hedge_rate_is_measured is True
    assert "measured: every leg 2" in report.evidence_caveat


def test_an_adverse_rate_without_a_source_is_refused() -> None:
    """An uncalibrated rate that flatters the strategy reads just like a measurement."""
    with pytest.raises(ValidationError, match="leg2_adverse_model_source is required"):
        PaperSettings(leg2_adverse_model_pct=Decimal("0.3"))


def test_an_adverse_rate_with_a_recorded_source_is_accepted() -> None:
    settings = PaperSettings(
        leg2_adverse_model_pct=Decimal("0.3"),
        leg2_adverse_model_source="KXFED/pmus book deltas, 2026-08-01..08-20, n=412",
    )
    assert settings.leg2_adverse_model_pct == Decimal("0.3")


@pytest.mark.parametrize("bad", [Decimal("-0.1"), Decimal("1.5")])
def test_an_out_of_range_adverse_rate_is_refused(bad: Decimal) -> None:
    with pytest.raises(ValidationError, match="between 0 and 1"):
        PaperSettings(leg2_adverse_model_pct=bad, leg2_adverse_model_source="s")
