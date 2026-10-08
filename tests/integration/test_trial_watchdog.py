"""Failure handling in the trial runtime: pause, stop, and observation-only mode.

A degraded feed used to produce one log line every five minutes and nothing else, so an
unattended trial kept paper-trading against books nothing was refreshing. Detected-count
metrics stayed plausible while the data behind them was known to be unreliable — which is
the worst kind of failure, because the report still looks like a report.
"""

from __future__ import annotations

import asyncio
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
from event_arb.orderbooks.coverage import RefreshOutcome
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
            refresh_interval_seconds=0.02,
            scan_interval_seconds=0.02,
            health_check_interval_seconds=0.02,
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
    """Run the trial until ``done`` holds, then stop it.

    The bound is a polled deadline rather than ``asyncio.timeout`` on purpose: cancelling
    the *test* would tell us nothing about the runner, and the assertion message needs the
    stats the runner reached before giving up.
    """
    await runner.start()
    try:
        deadline = asyncio.get_running_loop().time() + give_up_after
        while asyncio.get_running_loop().time() < deadline:
            if done(runner):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"condition not met within {give_up_after}s: {runner.stats.as_dict()}")
    finally:
        await runner.stop()


async def test_sustained_refresh_failure_pauses_trading(
    fixture_dir: Path, database: Database
) -> None:
    """Paused, not stopped: ingestion keeps trying and the gap is recorded as a gap."""
    settings = make_settings(fixture_dir, pause_after_consecutive_failures=2)
    scanner = await prepared(settings, database)

    async def broken() -> list[object]:
        raise RuntimeError("venue unreachable")

    scanner.refresh_books = broken  # type: ignore[method-assign,assignment]
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stats.trading_paused)

    assert runner.stats.paused_reason is not None
    assert "consecutive failures" in runner.stats.paused_reason


async def test_a_recovered_feed_resumes_trading(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir, pause_after_consecutive_failures=2)
    scanner = await prepared(settings, database)
    healthy = scanner.refresh_books
    failures = {"n": 0}

    async def flaky() -> RefreshOutcome:
        failures["n"] += 1
        if failures["n"] <= 3:
            raise RuntimeError("venue unreachable")
        return await healthy()

    scanner.refresh_books = flaky  # type: ignore[method-assign,assignment]
    runner = TrialRunner(scanner, settings, use_websockets=False)
    seen_paused = {"yes": False}

    def paused_then_recovered(r: TrialRunner) -> bool:
        # One session, not two: stopping a runner is final, so the pause and the recovery
        # have to be observed within the same run.
        seen_paused["yes"] = seen_paused["yes"] or r.stats.trading_paused
        return seen_paused["yes"] and not r.stats.trading_paused and r.stats.scans > 0

    await run_until(runner, paused_then_recovered)

    assert seen_paused["yes"], "the failing phase must have paused trading"
    assert not runner.stats.trading_paused
    assert runner.stats.consecutive_failures == 0


async def test_prolonged_failure_stops_the_trial(fixture_dir: Path, database: Database) -> None:
    """Every further cycle spends rate-limit budget to record nothing usable."""
    settings = make_settings(
        fixture_dir, pause_after_consecutive_failures=1, stop_after_consecutive_failures=3
    )
    scanner = await prepared(settings, database)

    async def broken() -> list[object]:
        raise RuntimeError("venue unreachable")

    scanner.refresh_books = broken  # type: ignore[method-assign,assignment]
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stats.stopped_reason is not None)

    assert runner.stats.stopped_reason is not None
    assert "consecutive failures" in runner.stats.stopped_reason


async def test_a_paused_trial_keeps_scanning_but_executes_nothing(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await prepared(settings, database)

    # Degraded ingestion is the case where this genuinely happens: the refresh still
    # returns something, so scanning continues, but the feed behind it is not trustworthy
    # enough to execute against.
    def unhealthy() -> list[str]:
        return ["KALSHI"]

    runner = TrialRunner(scanner, settings, use_websockets=False)
    runner._check_health = unhealthy  # type: ignore[method-assign,assignment]

    await run_until(runner, lambda r: r.stats.trading_paused and r.stats.scans >= 2)

    assert runner.stats.paper_trades == 0, "a paused trial executes nothing"
    assert runner.stats.scans >= 2, "but it keeps scanning, so the gap is recorded"
    assert runner.stats.trading_paused_cycles > 0
    assert runner.stats.health_alerts > 0


async def test_observation_only_executes_nothing(fixture_dir: Path, database: Database) -> None:
    """The explicit mode: a run that observes because it was told to."""
    settings = make_settings(fixture_dir, observation_only=True)
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stats.scans >= 2)

    assert runner.stats.confirmed_detected > 0, "it must still detect"
    assert runner.stats.paper_trades == 0, "and must still execute nothing"
    assert runner.status()["observation_only"] is True


async def test_the_health_loop_runs_on_its_own_cadence(
    fixture_dir: Path, database: Database
) -> None:
    """Health used to share the 300 s retention loop, so a dead feed went unnoticed.

    The documented stop condition is measured in minutes, which a five-minute check
    cannot serve. The two intervals are now separate settings.
    """
    settings = make_settings(fixture_dir)
    assert settings.trial.health_check_interval_seconds < 1.0
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await runner.start()
    try:
        names = {t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("trial-")}
        assert "trial-health" in names
        assert "trial-maintenance" in names
    finally:
        await runner.stop()

    leftover = [
        t for t in asyncio.all_tasks() if t.get_name().startswith("trial-") and not t.done()
    ]
    assert leftover == []


async def test_a_refused_trade_is_counted_separately_from_a_failure(
    fixture_dir: Path, database: Database
) -> None:
    """Out of simulated funds is not the same as a quiet market, and not an error either."""
    settings = make_settings(fixture_dir)
    settings = settings.model_copy(
        update={
            "paper": settings.paper.model_copy(
                update={
                    "starting_usd_balance": Decimal("0"),
                    "starting_usdc_balance": Decimal("0"),
                }
            )
        }
    )
    scanner = await prepared(settings, database)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    await run_until(runner, lambda r: r.stats.paper_trades_refused > 0)

    assert runner.stats.paper_trades == 0
    assert runner.stats.errors == 0, "a refusal is a decision, not a failure"
    assert not runner.stats.trading_paused
