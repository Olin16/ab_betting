"""An outage must never be reported as a quiet market.

These tests inject failure at the **adapter** — ``fetch_orderbook`` — rather than at
``Scanner.refresh_books``. That distinction is the whole point. ``refresh_books`` is the
layer that *catches* per-market fetch errors, so patching it over replaces the error
handling under test with a raise the runtime already knew how to see. Patching beneath it
exercises the real boundary, and that is where the silence was: every request could fail
and the batch still returned normally, so preflight passed, the watchdog counted clean
refreshes, and a dead feed produced a tidy report of nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from event_arb.domain.enums import Venue
from event_arb.domain.models import OrderBook, OrderBookSide
from event_arb.observability.health import HealthReport
from event_arb.persistence.database import Database
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner
from event_arb.trial_preflight import Severity, TrialMode, run_preflight
from event_arb.venues.base import MarketDataError
from tests.integration.test_trial_watchdog import make_settings, prepared, run_until

pytestmark = pytest.mark.anyio


def kill_feed(scanner: Scanner, venues: list[Venue]) -> None:
    """Make ``fetch_orderbook`` raise for the named venues, as an outage would."""

    class DeadFeed:
        async def __call__(self, market_id: str) -> OrderBook:
            raise MarketDataError("venue unreachable")

    for venue in venues:
        scanner.adapters[venue].fetch_orderbook = DeadFeed()  # type: ignore[method-assign]


def empty_feed(scanner: Scanner, venues: list[Venue]) -> None:
    """Return a syntactically valid book with no depth on either outcome."""

    class EmptyFeed:
        def __init__(self, inner: Callable[[str], Awaitable[OrderBook]]) -> None:
            self._inner = inner

        async def __call__(self, market_id: str) -> OrderBook:
            book = await self._inner(market_id)
            return book.model_copy(
                update={
                    "yes": OrderBookSide(outcome=book.yes.outcome),
                    "no": OrderBookSide(outcome=book.no.outcome),
                }
            )

    for venue in venues:
        adapter = scanner.adapters[venue]
        adapter.fetch_orderbook = EmptyFeed(adapter.fetch_orderbook)  # type: ignore[method-assign]


# ----------------------------------------------------------------- refresh outcome


async def test_healthy_refresh_reports_complete_coverage(
    fixture_dir: Path, database: Database
) -> None:
    """The control: a working feed must still look like a working feed."""
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)

    outcome = await scanner.refresh_books()

    assert outcome.is_complete
    assert len(outcome.covered) == len(outcome.required) == 2
    assert not outcome.missing
    assert outcome.reason() == ""


async def test_total_outage_is_reported_as_missing_coverage(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.KALSHI, Venue.POLYMARKET])

    outcome = await scanner.refresh_books()

    assert not outcome.is_complete
    assert len(outcome.missing) == 2
    assert len(outcome.failed) == 2
    assert not outcome.books
    assert "unreachable" in outcome.reason()


async def test_one_venue_outage_is_still_incomplete(fixture_dir: Path, database: Database) -> None:
    """Half a feed prices nothing: an arbitrage needs both legs quoted."""
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.KALSHI])

    outcome = await scanner.refresh_books()

    assert not outcome.is_complete
    assert len(outcome.covered) == 1
    assert [key[0] for key in outcome.missing] == [Venue.KALSHI]


async def test_empty_books_are_not_counted_as_coverage(
    fixture_dir: Path, database: Database
) -> None:
    """A book with no asks is a market that has stopped quoting, not a fetched book."""
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    empty_feed(scanner, [Venue.POLYMARKET])

    outcome = await scanner.refresh_books()

    assert not outcome.is_complete
    assert len(outcome.unusable) == 1
    assert "executable depth" in outcome.reason()
    # Stored and marked stale rather than dropped, so the previous, better-looking
    # snapshot cannot linger and be priced as if it were current.
    key = next(iter(outcome.unusable))
    stored = scanner.store.get(key)
    assert stored is not None
    assert stored.is_stale


async def test_coverage_recovers_when_the_feed_returns(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    healthy = {v: a.fetch_orderbook for v, a in scanner.adapters.items()}
    kill_feed(scanner, [Venue.KALSHI, Venue.POLYMARKET])

    assert not (await scanner.refresh_books()).is_complete

    for venue, fetch in healthy.items():
        scanner.adapters[venue].fetch_orderbook = fetch  # type: ignore[method-assign]

    assert (await scanner.refresh_books()).is_complete


# ---------------------------------------------------------------------- preflight


async def test_preflight_blocks_a_trading_run_on_a_dead_feed(
    fixture_dir: Path, database: Database
) -> None:
    """The gap this file exists for: preflight used to pass outright.

    Every request raising returns fast, so the freshness budget — which asks how long the
    batch took — was comfortably satisfied. Timing is not coverage.
    """
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.KALSHI, Venue.POLYMARKET])

    report = await run_preflight(scanner, settings, observation_only=False)

    assert not report.passed
    assert "feed_coverage" in [f.code for f in report.blocks]


async def test_preflight_blocks_on_a_single_venue_outage(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.POLYMARKET])

    report = await run_preflight(scanner, settings, observation_only=False)

    assert not report.passed
    assert "feed_coverage" in [f.code for f in report.blocks]


async def test_preflight_blocks_on_empty_books(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    empty_feed(scanner, [Venue.KALSHI])

    report = await run_preflight(scanner, settings, observation_only=False)

    assert not report.passed
    assert "feed_coverage" in [f.code for f in report.blocks]


async def test_observation_only_warns_rather_than_blocking(
    fixture_dir: Path, database: Database
) -> None:
    """Watching a feed fail is a legitimate thing to observe — if the report says so."""
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.KALSHI, Venue.POLYMARKET])

    report = await run_preflight(scanner, settings, observation_only=True)

    assert report.mode is TrialMode.OBSERVATION_ONLY
    coverage = [f for f in report.findings if f.code == "feed_coverage"]
    assert [f.severity for f in coverage] == [Severity.WARN]


async def test_preflight_passes_on_a_healthy_feed(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)

    report = await run_preflight(scanner, settings, observation_only=False)

    assert report.passed, report.render()
    coverage = [f for f in report.findings if f.code == "feed_coverage"]
    assert [f.severity for f in coverage] == [Severity.INFO]


# ------------------------------------------------------------------------ runtime


async def test_a_dead_feed_stops_the_trial(fixture_dir: Path, database: Database) -> None:
    """The headline: an outage must fail the run, not complete it with zero trades."""
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=3
    )
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.KALSHI, Venue.POLYMARKET])
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stopped)

    assert runner.failed
    assert runner.stats.stopped_reason is not None
    assert runner.stats.refreshes_incomplete > 0
    assert runner.stats.as_dict()["feed_was_healthy"] is False
    # And it stopped rather than accumulating a long, confident record of nothing.
    assert runner.stats.scans == 0


async def test_a_one_venue_outage_stops_the_trial(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=3
    )
    scanner = await prepared(settings, database)
    kill_feed(scanner, [Venue.KALSHI])
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stopped)

    assert runner.failed
    assert runner.stats.refreshes_incomplete > 0


async def test_a_healthy_no_opportunity_run_is_not_a_failure(
    fixture_dir: Path, database: Database
) -> None:
    """The other half of the contract: a genuinely quiet market must still pass.

    A gate that failed every no-trade run would be no more useful than one that passed
    every outage — it would just move the lie.
    """
    settings = make_settings(
        fixture_dir,
        pause_after_consecutive_failures=2,
        stop_after_consecutive_failures=3,
        auto_paper_trade=False,
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await runner.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await runner.stop()

    assert not runner.failed
    assert runner.stats.stopped_reason is None
    assert runner.stats.refreshes_incomplete == 0
    assert runner.stats.as_dict()["feed_was_healthy"] is True
    assert runner.stats.refreshes > 0
    assert runner.stats.scans > 0
    assert runner.stats.paper_trades == 0


async def test_a_recovering_feed_resumes_and_does_not_stop(
    fixture_dir: Path, database: Database
) -> None:
    """Two failed batches then recovery must not trip a stop threshold of three."""
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=3
    )
    # The health loop is the *other* escalation path and runs at its own cadence; at the
    # 20 ms the rest of this file uses it would reach its own stop threshold within a
    # tenth of a second, which is not the behaviour under test here. Its default is 30 s,
    # so give it a bound that keeps it out of the way while the refresh streak recovers.
    settings.trial.health_check_interval_seconds = 30.0
    scanner = await prepared(settings, database)
    healthy = {v: a.fetch_orderbook for v, a in scanner.adapters.items()}
    kill_feed(scanner, [Venue.KALSHI])
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await runner.start()
    try:
        await run_until_stats(runner, lambda r: r.stats.refreshes_incomplete >= 2)
        for venue, fetch in healthy.items():
            scanner.adapters[venue].fetch_orderbook = fetch  # type: ignore[method-assign]
        await run_until_stats(runner, lambda r: r.stats.scans > 0)
        # Read before shutting down: ``stop()`` sets the same event ``stopped`` reports.
        stopped_itself = runner.stopped
    finally:
        await runner.stop()

    assert not stopped_itself
    assert not runner.failed
    assert runner.stats.stopped_reason is None


async def run_until_stats(runner: TrialRunner, done, give_up_after: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """Poll an already-started runner, leaving start/stop to the caller."""
    deadline = asyncio.get_running_loop().time() + give_up_after
    while asyncio.get_running_loop().time() < deadline:
        if done(runner):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition not met within {give_up_after}s: {runner.stats.as_dict()}")


# ------------------------------------------------------------------- health loop
#
# The health loop is the second, independent escalation path. Its docstring promised
# that sustained degradation "stops the run", but the code only ever paused and
# resumed: a permanently dead feed sat paused until the clock ran out and the process
# exited zero. These pin the promise to behaviour.


async def test_sustained_degradation_stops_the_run(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=3
    )
    scanner = await prepared(settings, database)

    # Degrade what the health check reads, leaving refresh alone, so the stop can only
    # have come from the health loop.
    def degraded(now: datetime | None = None) -> HealthReport:
        return _unhealthy_report()

    scanner.health = degraded  # type: ignore[method-assign]
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stopped)

    assert runner.failed
    assert runner.stats.health_alerts > 0
    assert runner.stats.stopped_reason is not None
    assert "health" in runner.stats.stopped_reason


async def test_a_raising_health_check_is_bounded(fixture_dir: Path, database: Database) -> None:
    """A watchdog that cannot run is not a watchdog; looping on it forever hid that."""
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=2, stop_after_consecutive_failures=3
    )
    scanner = await prepared(settings, database)

    def raising(now: datetime | None = None) -> HealthReport:
        raise RuntimeError("health check itself is broken")

    scanner.health = raising  # type: ignore[method-assign]
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stopped)

    assert runner.failed
    assert runner.stats.errors > 0
    assert runner.stats.stopped_reason is not None


def _unhealthy_report() -> HealthReport:
    return HealthReport(
        healthy=False,
        generated_at=datetime.now(UTC),
        adapters=[{"venue": "KALSHI", "fresh": False}],
        books_tracked=0,
    )
