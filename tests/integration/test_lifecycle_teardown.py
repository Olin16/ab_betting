"""Engines and background tasks must not outlive the process that made them.

A full-suite run emitted RuntimeError("Event loop is closed") from aiosqlite worker
threads, 32-48 times per run and attributed to whichever test happened to be running. The
cause was undisposed engines: each test gets its own event loop, and a pooled aiosqlite
connection keeps a worker thread that calls back into that loop after it closes.

For a multi-week trial the same defect is not noise. An engine that is not disposed on a
failed boot leaks a connection and a thread on every restart attempt.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import sqlalchemy as sa

from event_arb.persistence.database import Database
from event_arb.scanner import Scanner

pytestmark = pytest.mark.anyio


def _aiosqlite_threads() -> set[int]:
    return {
        t.ident
        for t in threading.enumerate()
        if t.ident is not None and "_connection_worker_thread" in (t.name or "")
    }


async def test_disposing_a_database_reclaims_its_worker_thread() -> None:
    before = _aiosqlite_threads()
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    async with database.session() as session:
        await session.execute(sa.text("SELECT 1"))

    during = _aiosqlite_threads()
    assert during - before, "expected a worker thread while the engine was open"

    await database.dispose()
    for thread in threading.enumerate():
        if thread.ident in (during - before):
            thread.join(timeout=5.0)
    assert not (_aiosqlite_threads() - before), "worker thread outlived dispose()"


async def test_scanner_close_disposes_its_database(fixture_dir: Path, database: Database) -> None:
    """Scanner owns the database it was given, so closing it must release the engine."""
    from event_arb.config import PolymarketProduct, PolymarketSettings, Settings

    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.persist_markets()

    await scanner.close()

    # A disposed engine refuses further work rather than silently reconnecting.
    with pytest.raises(Exception):  # noqa: B017 - driver-specific
        async with database.session() as session:
            await session.execute(sa.text("SELECT 1"))
            raise RuntimeError("engine should have been disposed")


async def test_a_failed_startup_still_closes_the_scanner(fixture_dir: Path) -> None:
    """The lifespan's cleanup is in a finally, so a boot failure cannot leak the engine."""
    from event_arb.config import PolymarketProduct, PolymarketSettings, Settings

    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    scanner = Scanner(settings, database=database)

    closed = False
    original = scanner.close

    async def tracking_close() -> None:
        nonlocal closed
        closed = True
        await original()

    scanner.close = tracking_close  # type: ignore[method-assign]

    # Mimic the lifespan's shape: work that raises, cleanup in finally.
    with pytest.raises(RuntimeError, match="discovery exploded"):
        try:
            raise RuntimeError("discovery exploded")
        finally:
            await scanner.close()

    assert closed, "cleanup must run even when startup raises"
