"""What the trial does when its own machinery fails, end to end through the real loops.

Every test here drives a complete `TrialRunner` cycle rather than calling the watchdog's
counters directly. That distinction is the point: the previous suite exercised
`_note_failure` in isolation and passed, while the assembled runtime reset the counter
between the refresh and the scan and therefore never escalated at all.

The failures injected are the ones an unattended run actually meets — a dead feed, a
database that stops accepting writes, execution that raises every cycle — and each test
asserts on the outcome an operator would read: paused, stopped, and with what reason.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import (
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
    TrialSettings,
    WatchedPair,
)
from event_arb.demo_reviews import load_recorded_reviews
from event_arb.persistence.database import Database
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED, POLY_FED

pytestmark = pytest.mark.anyio


def make_settings(fixture_dir: Path, **trial_overrides: object) -> Settings:
    return Settings(
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            max_book_age_ms=60_000, max_quantity=Decimal("100"), alert_cooldown_seconds=0
        ),
        trial=TrialSettings(
            enabled=True,
            watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)],
            refresh_interval_seconds=0.01,
            scan_interval_seconds=0.01,
            health_check_interval_seconds=0.01,
            **trial_overrides,  # type: ignore[arg-type]
        ),
    )


async def prepared(settings: Settings, database: Database) -> Scanner:
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.seed_watched_pairs()
    await scanner.persist_markets()
    for review in load_recorded_reviews(settings):
        for pair_id, pair in list(scanner.state.pairs.items()):
            if review.matches(
                pair.kalshi_market_id, pair.polymarket_market_id, pair.polymarket_product.value
            ):
                await scanner.approve_and_persist(
                    pair_id,
                    approved_by=review.reviewed_by,
                    reviewer_notes=review.notes,
                    kalshi_reviewed_source=review.kalshi_source,
                    polymarket_reviewed_source=review.polymarket_source,
                )
    return scanner


async def run_until(runner: TrialRunner, done, give_up_after: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """Run the assembled trial until ``done`` holds, then stop it."""
    await runner.start()
    try:
        deadline = asyncio.get_running_loop().time() + give_up_after
        while asyncio.get_running_loop().time() < deadline:
            if done(runner):
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"condition not met within {give_up_after}s: {runner.stats.as_dict()}")
    finally:
        await runner.stop()


# ------------------------------------------- failures are counted against their own loop


async def test_a_failing_scan_escalates_even_though_every_refresh_succeeds(
    fixture_dir: Path, database: Database
) -> None:
    """The reproduced defect: one shared counter, reset by the refresh that precedes it.

    A cycle refreshes and then scans. The success of the refresh cleared the whole failure
    streak, so a scan failing on *every* cycle never got past a count of one. Fifteen
    consecutive failed scans produced `consecutive_failures=1`, no pause, and no stop — a
    trial collecting nothing while reporting itself healthy.
    """
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=4
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def failing_scan() -> None:
        raise RuntimeError("scan exploded")

    runner._scan_once = failing_scan  # type: ignore[method-assign,assignment]

    await run_until(runner, lambda r: r.stats.stopped_reason is not None)

    assert runner.stats.refreshes > 0, "the refresh must genuinely have been succeeding"
    assert runner.stats.stopped_reason is not None
    assert "scan" in runner.stats.stopped_reason
    assert runner.stats.consecutive_failures_by_loop.get("scan", 0) >= 4


async def test_a_failing_paper_trade_escalates_even_though_scanning_succeeds(
    fixture_dir: Path, database: Database
) -> None:
    """Execution failures fed the same shared counter, and were erased the same way.

    Execution failures also escalate straight to a stop rather than pausing first. A pause
    suppresses execution, which is the only thing that could advance an execution failure
    streak: the count froze one below the stop threshold and the trial sat paused forever,
    detecting opportunities it would never attempt and never reaching a verdict.
    """
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=3
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def failing_execution(*args: object, **kwargs: object) -> None:
        raise RuntimeError("execution exploded")

    scanner.paper_trade_if_funded = failing_execution  # type: ignore[method-assign,assignment]

    await run_until(runner, lambda r: r.stats.stopped_reason is not None)

    assert runner.stats.scans > 0, "scanning must genuinely have been succeeding"
    assert "paper_trade" in (runner.stats.stopped_reason or "")
    assert not runner.stats.trading_paused, (
        "an execution-failure pause would freeze the streak below the stop threshold"
    )


async def test_one_loops_recovery_does_not_forgive_another_loops_failures(
    fixture_dir: Path, database: Database
) -> None:
    """Streaks are per loop, so a healthy refresh cannot clear a failing scan's history."""
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=50, stop_after_consecutive_failures=99
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await runner._note_failure("scan")
    await runner._note_failure("scan")
    runner._note_success("refresh")

    assert runner.stats.consecutive_failures_by_loop["scan"] == 2
    assert runner.stats.consecutive_failures == 2

    runner._note_success("scan")

    assert "scan" not in runner.stats.consecutive_failures_by_loop
    assert runner.stats.consecutive_failures == 0


async def test_a_refused_trade_is_not_a_failure(fixture_dir: Path, database: Database) -> None:
    """Running out of simulated money is a result. Stopping the trial for it is not."""
    settings = make_settings(fixture_dir, stop_after_consecutive_failures=2)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def always_refused(*args: object, **kwargs: object) -> None:
        return None

    scanner.paper_trade_if_funded = always_refused  # type: ignore[method-assign,assignment]

    await run_until(runner, lambda r: r.stats.paper_trades_refused >= 3)

    assert runner.stats.stopped_reason is None
    assert not runner.stats.trading_paused


# ------------------------------------------------- the watchdog cannot be blocked by I/O


async def test_a_dead_audit_log_cannot_stop_the_watchdog_stopping(
    fixture_dir: Path, database: Database
) -> None:
    """A failure handler must not depend on the subsystem whose failure summoned it.

    The stop event was set *after* an audit write. With storage down — the very thing that
    had been failing — `_note_failure` raised on the way to stopping, leaving the run
    going with `stopped_reason` already set: reporting itself stopped while still spending
    rate-limit budget on a feed nobody was recording.
    """
    settings = make_settings(fixture_dir, stop_after_consecutive_failures=1)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def dead_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit storage unavailable")

    scanner.audit = dead_audit  # type: ignore[method-assign,assignment]

    await runner._note_failure("refresh")

    assert runner._stopping.is_set(), "the stop must not need storage to happen"
    assert runner.stats.stopped_reason is not None
    assert not runner.running


async def test_a_dead_audit_log_cannot_stop_the_watchdog_pausing(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=1, stop_after_consecutive_failures=99
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def dead_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit storage unavailable")

    scanner.audit = dead_audit  # type: ignore[method-assign,assignment]

    await runner._note_failure("refresh")

    assert runner.stats.trading_paused
    assert runner.stats.paused_reason is not None


async def test_a_storage_outage_during_a_running_trial_stops_it(
    fixture_dir: Path, database: Database
) -> None:
    """The whole runtime, with the database refusing every write mid-run."""
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=4
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def dead_storage(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is gone")

    scanner.scan_and_persist = dead_storage  # type: ignore[method-assign,assignment]
    scanner.audit = dead_storage  # type: ignore[method-assign,assignment]

    await run_until(runner, lambda r: not r.running)

    assert runner.stats.stopped_reason is not None
    assert runner.failed, "a storage outage must fail the run, not complete it"


# --------------------------------------------------- a dead task wakes the whole trial


async def test_a_task_that_dies_stops_the_trial_and_marks_it_failed(
    fixture_dir: Path, database: Database
) -> None:
    """The loops swallow their own errors, so a task that ends at all ended unexpectedly.

    Nothing noticed. The remaining tasks kept running and any caller waiting on the runner
    kept waiting, so a trial with no scanning at all still looked like a trial.
    """
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    # A real task, ended by a real exception, then handed to the real callback — rather
    # than reaching into the runner's state, which would prove only that the assertion
    # matches the assignment.
    async def dies() -> None:
        raise RuntimeError("loop died")

    dead: asyncio.Task[None] = asyncio.create_task(dies(), name="trial-cycle")
    with pytest.raises(RuntimeError):
        await dead

    runner._task_finished(dead)

    assert runner.failed
    assert runner.task_failed
    assert "trial-cycle" in (runner.stats.stopped_reason or "")
    assert "loop died" in (runner.stats.stopped_reason or "")
    assert not runner.running


async def test_wait_until_stopped_returns_when_the_watchdog_gives_up(
    fixture_dir: Path, database: Database
) -> None:
    """What the CLI now waits on. It used to wait on an Event nothing ever set."""
    settings = make_settings(fixture_dir, stop_after_consecutive_failures=1)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def broken() -> list[object]:
        raise RuntimeError("venue unreachable")

    scanner.refresh_books = broken  # type: ignore[method-assign,assignment]
    await runner.start()
    try:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5.0):
                await runner.wait_until_stopped()
        stopped = runner.stopped
    finally:
        await runner.stop()

    assert stopped, "a watchdog stop must wake the caller"
    assert runner.failed


async def test_wait_until_stopped_reports_a_healthy_run_that_merely_ran_out_of_time(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)
    await runner.start()
    try:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(0.05):
                await runner.wait_until_stopped()
        stopped = runner.stopped
    finally:
        await runner.stop()

    assert not stopped, "the duration elapsing is not the runner stopping itself"
    assert not runner.failed
