"""Durable run completeness: what a fresh process can still learn about the last one.

The defect these pin: invalidation lived only in a Python list. A run that lost an
attempt reported ``invalidated=1`` and ``is_complete_evidence=False``; reopening the same
database reported ``invalidated=0`` and ``is_complete_evidence=True``. The restart did not
merely lose the detail — it upgraded the verdict, turning a known hole into a clean report.

Recording the failure in the database cannot fix that, because the attempt is invalidated
precisely *because* the database refused it. Hence the journal: an append-only file beside
the database, written with fsync, that a failed write and a killed process both survive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_arb.paper.durability import DurabilityStage, TradeNotDurableError
from event_arb.paper.journal import RunJournal, journal_path_for
from event_arb.paper.reconciliation import reconcile_accounts
from event_arb.persistence.repositories import PaperTradeRepository
from tests.integration.test_accounting_durability import (
    make_settings,
    opened,
    reopened,
)

pytestmark = pytest.mark.anyio


async def fail_the_next_write(
    scanner: object, monkeypatch: pytest.MonkeyPatch, message: str = "storage went away"
) -> None:
    async def failing(self: PaperTradeRepository, trade: object) -> None:
        raise RuntimeError(message)

    monkeypatch.setattr(PaperTradeRepository, "record", failing)


# ------------------------------------------------------- invalidation across a restart


async def test_an_invalidated_attempt_survives_a_restart(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline gap: a restart used to report the lost attempt as no attempt at all."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        await fail_the_next_write(scanner, monkeypatch)
        with pytest.raises(TradeNotDurableError):
            await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        monkeypatch.undo()
        before = scanner.portfolio.daily_report()
        assert before.invalidated_attempts == 1
        assert not before.is_complete_evidence
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        after = restarted.portfolio.daily_report()
        assert after.invalidated_attempts == 1, "the fresh process still knows"
        assert not after.is_complete_evidence, "a restart must not upgrade the verdict"
    finally:
        await restarted.close()


async def test_the_invalidation_is_not_written_to_the_database_that_refused_it(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure record that needs the failed subsystem is not a failure record."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        await fail_the_next_write(scanner, monkeypatch)
        with pytest.raises(TradeNotDurableError):
            await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        monkeypatch.undo()
    finally:
        await scanner.close()

    journal = journal_path_for(settings.database_url)
    assert journal is not None and journal.exists()
    kinds = [json.loads(line)["kind"] for line in journal.read_text().splitlines() if line.strip()]
    assert "attempt_invalidated" in kinds

    history = RunJournal.read(journal)
    assert len(history.invalidated) == 1
    assert history.invalidated[0].stage is DurabilityStage.CHECKPOINT
    assert not history.is_clean


async def test_a_repaired_attempt_stops_counting_without_erasing_its_history(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry that works is not a permanent hole — but the failure stays on the record."""
    from event_arb.domain.enums import Outcome
    from event_arb.paper.executor import SettlementOutcomes
    from tests.integration.test_accounting_durability import RECORDED

    settings = make_settings(fixture_dir, db_url)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    scanner = await opened(settings)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        await fail_the_next_write(scanner, monkeypatch, "storage went away mid-settlement")
        with pytest.raises(RuntimeError, match="storage went away"):
            await scanner.settle_paper_trade(trade.id, outcomes)
        monkeypatch.undo()
        await scanner.settle_paper_trade(trade.id, outcomes)
        trade_id = trade.id
    finally:
        await scanner.close()

    journal = journal_path_for(settings.database_url)
    assert journal is not None
    kinds = [json.loads(line)["kind"] for line in journal.read_text().splitlines() if line.strip()]
    # Both facts are on the record: it failed, and it was repaired.
    assert "attempt_invalidated" in kinds
    assert "attempt_resolved" in kinds

    history = RunJournal.read(journal)
    assert history.invalidated == [], "a repaired attempt is not an outstanding hole"
    assert trade_id in history.resolved

    restarted = await reopened(settings)
    try:
        assert restarted.portfolio.daily_report().is_complete_evidence
    finally:
        await restarted.close()


# ------------------------------------------------------------------ unfinished runs


async def test_a_run_that_never_finished_is_visible_to_the_next_process(
    fixture_dir: Path, db_url: str
) -> None:
    """An abrupt death leaves storage looking exactly like a clean shutdown.

    Nothing in the database distinguishes them, which is why the marker has to be a thing
    a clean shutdown *closes* rather than a thing a crash writes: a killed process gets no
    chance to write anything.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        run_id = scanner.begin_run(mode="paper_trading")
        assert run_id is not None
        # No finish_run(): this is the process being killed.
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert [r.run_id for r in restarted.journal_history.unfinished] == [run_id]
        report = restarted.portfolio.daily_report()
        assert report.unfinished_runs == 1
        assert not report.is_complete_evidence
        assert "without a clean shutdown" in report.evidence_caveat
    finally:
        await restarted.close()


async def test_a_clean_shutdown_leaves_nothing_outstanding(fixture_dir: Path, db_url: str) -> None:
    """The control: an orderly run must not poison its own database."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        scanner.begin_run(mode="paper_trading")
        scanner.finish_run(reconciled=True)
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert restarted.journal_history.unfinished == []
        assert restarted.journal_history.is_clean
        assert restarted.portfolio.daily_report().is_complete_evidence
    finally:
        await restarted.close()


async def test_an_unfinished_run_fails_the_reconciliation_gate(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        scanner.begin_run(mode="paper_trading")
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        report = reconcile_accounts(restarted.portfolio)
        assert not report.evidence_is_complete
        assert len(report.unfinished_runs) == 1
        assert "UNFINISHED RUNS" in report.to_text()
    finally:
        await restarted.close()


async def test_acknowledging_a_run_clears_the_gate_without_deleting_the_record(
    fixture_dir: Path, db_url: str
) -> None:
    """The gate going green must still leave a named person and a reason behind."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        run_id = scanner.begin_run(mode="paper_trading")
        assert run_id is not None
    finally:
        await scanner.close()

    reviewer = await reopened(settings)
    try:
        assert reviewer.portfolio.daily_report().unfinished_runs == 1
        assert reviewer.acknowledge_run(run_id, by="operator", note="host was rebooted")
    finally:
        await reviewer.close()

    after = await reopened(settings)
    try:
        assert after.journal_history.unfinished == []
        assert after.portfolio.daily_report().is_complete_evidence
    finally:
        await after.close()

    # The crash itself was never removed — only annotated.
    journal = journal_path_for(settings.database_url)
    assert journal is not None
    records = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    assert any(r["kind"] == "run_started" and r["run_id"] == run_id for r in records)
    ack = next(r for r in records if r["kind"] == "run_acknowledged")
    assert ack["acknowledged_run"] == run_id
    assert ack["by"] == "operator"
    assert ack["note"] == "host was rebooted"


async def test_a_damaged_journal_line_counts_as_damage(fixture_dir: Path, db_url: str) -> None:
    """A journal we cannot fully read is itself a reason to distrust the evidence."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        scanner.begin_run(mode="paper_trading")
        scanner.finish_run()
        journal = scanner.journal
        assert journal is not None
    finally:
        await scanner.close()

    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json\n")

    history = RunJournal.read(journal.path)
    assert history.unreadable_lines == 1
    assert not history.is_clean
    assert "unreadable" in history.summary()


async def test_a_database_with_no_file_has_no_journal(fixture_dir: Path) -> None:
    """In-memory runs have no restart to survive, so they get no sidecar."""
    assert journal_path_for("sqlite+aiosqlite:///:memory:") is None
    assert journal_path_for("postgresql+asyncpg://host/db") is None
