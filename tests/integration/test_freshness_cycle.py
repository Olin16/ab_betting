"""Refresh and scan must be one coordinated cycle, not two racing timers."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import (
    OpportunitySettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
    TrialSettings,
)
from event_arb.domain.enums import RejectionReason
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner


def settings_for(fixture_dir: Path, **trial: object) -> Settings:
    return Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        trial=TrialSettings(
            **{
                "enabled": True,
                "refresh_interval_seconds": 0.05,
                "scan_interval_seconds": 0.05,
                **trial,
            }  # type: ignore[arg-type]
        ),
        opportunity=OpportunitySettings(
            max_book_age_ms=60_000, max_quantity=Decimal("100"), alert_cooldown_seconds=0
        ),
    )


# ------------------------------------------------------------- budget validation


def test_default_freshness_bound_is_flagged_as_too_tight() -> None:
    """2s bound against a 5s poll cycle cannot be satisfied; say so rather than pretend."""
    warning = Settings().validate_freshness_budget()
    assert warning is not None
    assert "below the" in warning


def test_a_coherent_budget_produces_no_warning() -> None:
    settings = Settings(
        opportunity=OpportunitySettings(max_book_age_ms=10_000),
        trial=TrialSettings(refresh_interval_seconds=5.0, scan_interval_seconds=5.0),
    )
    assert settings.validate_freshness_budget() is None


def test_required_bound_accounts_for_cycle_plus_latency() -> None:
    trial = TrialSettings(
        refresh_interval_seconds=5.0,
        scan_interval_seconds=3.0,
        expected_request_latency_seconds=2.0,
    )
    # Cycle is the longer of the two intervals, plus request latency.
    assert trial.cycle_interval_seconds == 5.0
    assert trial.required_book_age_ms() == 7000


# ------------------------------------------------------------- cycle ordering


async def test_scan_never_runs_before_the_first_refresh(fixture_dir: Path) -> None:
    """A slow refresh must delay the scan, not let it read pre-refresh books."""
    scanner = Scanner(settings_for(fixture_dir))
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))

    order: list[str] = []
    original_refresh = scanner.refresh_books
    original_scan = scanner.scan_and_persist

    async def slow_refresh(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        order.append("refresh-start")
        await asyncio.sleep(0.1)
        result = await original_refresh(*args, **kwargs)  # type: ignore[arg-type]
        order.append("refresh-end")
        return result

    async def tracked_scan(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        order.append("scan")
        return await original_scan(*args, **kwargs)  # type: ignore[arg-type]

    scanner.refresh_books = slow_refresh  # type: ignore[method-assign]
    scanner.scan_and_persist = tracked_scan  # type: ignore[method-assign]

    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await runner.start()
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while order.count("scan") < 2:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"cycle did not run twice; order={order}")
            await asyncio.sleep(0.01)
    finally:
        await runner.stop()

    # Every scan must be immediately preceded by a completed refresh.
    for index, event in enumerate(order):
        if event == "scan":
            assert order[index - 1] == "refresh-end", f"scan ran out of order: {order}"


async def test_a_failed_refresh_skips_the_scan(fixture_dir: Path) -> None:
    """Scanning unverified books would report on state we could not confirm."""
    scanner = Scanner(settings_for(fixture_dir))
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))

    scans = {"n": 0}
    original_scan = scanner.scan_and_persist

    async def failing_refresh(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("venue unreachable")

    async def counted_scan(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        scans["n"] += 1
        return await original_scan(*args, **kwargs)  # type: ignore[arg-type]

    scanner.refresh_books = failing_refresh  # type: ignore[method-assign]
    scanner.scan_and_persist = counted_scan  # type: ignore[method-assign]

    runner = TrialRunner(scanner, scanner.settings, use_websockets=False)
    await runner.start()
    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while runner.stats.errors < 2:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("refresh never failed twice")
            await asyncio.sleep(0.01)
    finally:
        await runner.stop()

    assert scans["n"] == 0, "scan ran despite an unverified refresh"
    assert runner.stats.refreshes == 0


async def test_stale_books_are_rejected_rather_than_priced(fixture_dir: Path) -> None:
    """The failure mode must stay 'missed opportunity', never 'priced an old book'."""
    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        opportunity=OpportunitySettings(max_book_age_ms=1, max_quantity=Decimal("100")),
    )
    scanner = Scanner(settings)
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books()
    await asyncio.sleep(0.02)

    result = scanner.scan()
    assert result.opportunities == []
    assert any(r.reason is RejectionReason.BOOK_STALE for r in result.rejections)


@pytest.mark.parametrize("interval", [0.05, 0.1])
async def test_cycle_uses_the_longer_of_the_two_intervals(
    fixture_dir: Path, interval: float
) -> None:
    settings = settings_for(
        fixture_dir, refresh_interval_seconds=interval, scan_interval_seconds=0.01
    )
    assert settings.trial.cycle_interval_seconds == interval
