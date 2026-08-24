"""The autonomous trial pipeline.

A trial exists to accumulate evidence that cannot be gathered by hand: how often the
second leg fails, how much detected edge survives real latency, how much unhedged exposure
accrues. These tests pin that the loops actually run, that what they produce is persisted,
and — most importantly — that automation never widens what may be traded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from event_arb.config import (
    AlertSettings,
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
    TrialSettings,
    WatchedPair,
)
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import OpportunityRepository, PaperTradeRepository
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED, POLY_FED


def make_settings(fixture_dir: Path, **trial_overrides: object) -> Settings:
    trial = TrialSettings(
        enabled=True,
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)],
        refresh_interval_seconds=0.05,
        scan_interval_seconds=0.05,
        **trial_overrides,  # type: ignore[arg-type]
    )
    return Settings(
        # Legacy Polymarket **Global** corpus. Stated explicitly so this path stays
        # clearly separated from the Polymarket US fixtures and tests.
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        trial=trial,
        # No simulated network delay: these tests assert orchestration, not latency.
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            max_book_age_ms=60_000, max_quantity=Decimal("100"), alert_cooldown_seconds=0
        ),
    )


async def prepared(settings: Settings, database: Database, approve: bool = True) -> Scanner:
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.seed_watched_pairs()
    await scanner.persist_markets()
    if approve:
        pair_id = next(iter(scanner.state.pairs))
        await scanner.approve_and_persist(pair_id, approved_by="reviewer@example.com")
    return scanner


async def run_until(
    runner: TrialRunner,
    predicate: Callable[[TrialRunner], bool],
    give_up_after: float = 5.0,
) -> None:
    """Run the trial until a condition holds, then stop.

    Polling beats sleeping a fixed duration: a wall-clock assertion is racy under load,
    and a flaky test in the subsystem that produces trial evidence is worse than no test.
    """
    await runner.start()
    try:
        deadline = asyncio.get_running_loop().time() + give_up_after
        while not predicate(runner):
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"condition not met within {give_up_after}s; stats={runner.stats.as_dict()}"
                )
            await asyncio.sleep(0.01)
    finally:
        await runner.stop()


async def run_briefly(runner: TrialRunner, seconds: float = 0.3) -> None:
    """For assertions about what must *not* happen, where there is nothing to wait for."""
    await runner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await runner.stop()


# ------------------------------------------------------------------- the pipeline


async def test_trial_scans_and_paper_trades_without_being_asked(
    fixture_dir: Path, database: Database
) -> None:
    """The whole point of step 5: evidence accrues with no operator involvement."""
    scanner = await prepared(make_settings(fixture_dir), database)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.paper_trades > 0)

    assert runner.stats.scans > 0
    assert runner.stats.confirmed_detected > 0
    assert runner.stats.paper_trades > 0
    assert runner.stats.errors == 0


async def test_evidence_is_persisted_not_just_counted(
    fixture_dir: Path, database: Database
) -> None:
    scanner = await prepared(make_settings(fixture_dir), database)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.paper_trades > 0)

    async with database.session() as session:
        assert await OpportunityRepository(session).count() > 0
        assert await PaperTradeRepository(session).count() > 0


async def test_rejections_are_retained_as_missed_opportunities(
    fixture_dir: Path, database: Database
) -> None:
    """Why a scan found nothing is evidence too."""
    scanner = await prepared(make_settings(fixture_dir), database)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.scans > 0)
    assert scanner.portfolio.daily_report().missed_opportunities


# ---------------------------------------------------- automation cannot widen scope


async def test_unapproved_pairs_are_not_paper_traded_by_default(
    fixture_dir: Path, database: Database
) -> None:
    """Automation must not quietly start trading pairs no human approved."""
    scanner = await prepared(make_settings(fixture_dir), database, approve=False)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_briefly(runner)

    assert runner.stats.opportunities_detected > 0
    assert runner.stats.confirmed_detected == 0
    assert runner.stats.paper_trades == 0


async def test_research_candidates_can_be_simulated_when_explicitly_enabled(
    fixture_dir: Path, database: Database
) -> None:
    """Opt-in evidence gathering; still never labeled arbitrage."""
    settings = make_settings(fixture_dir, paper_trade_research_candidates=True)
    scanner = await prepared(settings, database, approve=False)
    runner = TrialRunner(scanner, settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.paper_trades > 0)

    assert all(not t.opportunity.is_confirmed for t in scanner.state.paper_trades)


async def test_auto_paper_trade_can_be_switched_off(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir, auto_paper_trade=False)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.confirmed_detected > 0)

    assert runner.stats.paper_trades == 0


async def test_trial_never_makes_live_trading_possible(
    fixture_dir: Path, database: Database
) -> None:
    scanner = await prepared(make_settings(fixture_dir), database)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.scans > 0)
    assert runner.status()["live_trading_possible"] is False
    assert scanner.settings.live_trading_possible is False


# --------------------------------------------------------------------- lifecycle


async def test_stop_leaves_no_tasks_running(fixture_dir: Path, database: Database) -> None:
    scanner = await prepared(make_settings(fixture_dir), database)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await runner.start()
    assert runner.running is True
    await runner.stop()

    # Locals, not attributes: mypy carries the narrowing from the assert above across
    # stop(), which would make these checks unreachable and therefore unchecked.
    stopped: bool = runner.running
    streaming: bool = scanner.streaming
    assert stopped is False
    assert streaming is False
    leftover = [
        t for t in asyncio.all_tasks() if t.get_name().startswith("trial-") and not t.done()
    ]
    assert leftover == []


async def test_a_failing_loop_does_not_kill_the_trial(
    fixture_dir: Path, database: Database
) -> None:
    """One bad scan must be recorded and survived, not take the run down."""
    scanner = await prepared(make_settings(fixture_dir), database)
    calls = {"n": 0}
    original = scanner.scan_and_persist

    async def flaky(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient venue failure")
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    scanner.scan_and_persist = flaky  # type: ignore[method-assign]
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.errors >= 1 and r.stats.scans > 0)

    assert runner.stats.errors >= 1
    assert "transient venue failure" in (runner.stats.last_error or "")
    assert runner.stats.scans > 0  # kept going


async def test_status_reports_watched_markets_and_counters(
    fixture_dir: Path, database: Database
) -> None:
    scanner = await prepared(make_settings(fixture_dir), database)
    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await run_until(runner, lambda r: r.stats.scans > 0)

    status = runner.status()
    assert status["watched_markets"] == {"KALSHI": [KALSHI_FED], "POLYMARKET": [POLY_FED]}
    stats = status["stats"]
    assert isinstance(stats, dict)
    assert stats["scans"] > 0


async def test_alerts_are_dispatched_for_detections(fixture_dir: Path, database: Database) -> None:
    scanner = await prepared(make_settings(fixture_dir), database)
    sent: list[object] = []

    class RecordingDispatcher:
        async def dispatch(self, opportunity: object) -> None:
            sent.append(opportunity)

    runner = TrialRunner(
        scanner,
        scanner.settings,
        alerts=RecordingDispatcher(),  # type: ignore[arg-type]
        use_websockets=False,
    )
    await run_until(runner, lambda r: bool(sent))
    assert sent


async def test_alert_settings_default_to_withholding_venue_data(
    fixture_dir: Path,
) -> None:
    """Automation must not become a route around the third-party data restriction."""
    assert AlertSettings().allow_third_party_market_data is False
