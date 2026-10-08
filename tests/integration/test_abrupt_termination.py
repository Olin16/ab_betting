"""A process killed outright, and what the next one can still tell.

Every other durability test simulates failure from inside the process: an exception, a
patched repository, a cancelled task. Those cover the failures the process survives long
enough to handle, which is by definition not the failure this one is about. SIGKILL gives
no handler, no `finally`, no flush — and storage afterwards looks exactly like a clean
shutdown, because a clean shutdown is also just "the writes that happened, happened".

So this spawns a real subprocess, lets it open a run and take on real exposure, kills it
with SIGKILL, and then asks a fresh process what it knows. What makes the answer possible
is that the marker is closed by *success* rather than written by failure: a killed process
cannot record its own death, but it can fail to record its survival.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from event_arb.paper.journal import RunJournal, journal_path_for

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[2]

#: Runs in a child process. Opens a run, executes one funded fixture trade so there is
#: real durable state, signals readiness on stdout, then blocks forever waiting to be
#: killed. Nothing here ever gets to shut down cleanly.
CHILD = """
import asyncio, sys
sys.path.insert(0, {repo!r})

from decimal import Decimal
from pathlib import Path

from event_arb.config import (
    OpportunitySettings, PaperSettings, PolymarketProduct, PolymarketSettings, Settings,
)
from event_arb.persistence.database import Database
from event_arb.persistence.schema import ensure_schema
from event_arb.scanner import Scanner


async def main():
    settings = Settings(
        fixture_dir=Path({fixtures!r}),
        database_url={url!r},
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"), min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000, max_quantity=Decimal("100"),
        ),
    )
    database = Database(settings.database_url)
    await ensure_schema(database)
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.persist_markets()
    await scanner.persist_pairs()
    await scanner.refresh_books()
    run_id = scanner.begin_run(mode="paper_trading")
    trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
    print("READY " + str(run_id) + " " + str(trade.id), flush=True)
    # Block forever. The parent kills us here; nothing below ever runs.
    await asyncio.Event().wait()


asyncio.run(main())
"""


def start_child(tmp_path: Path, url: str) -> subprocess.Popen[str]:
    script = CHILD.format(repo=str(REPO), fixtures=str(REPO / "tests" / "fixtures"), url=url)
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO),
    )


def wait_for_ready(child: subprocess.Popen[str], timeout: float = 60.0) -> tuple[str, str]:
    """Read the child's readiness line, or fail with whatever it printed instead."""
    assert child.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = child.stdout.readline()
        if line.startswith("READY "):
            _, run_id, trade_id = line.split()
            return run_id, trade_id
        if line == "" and child.poll() is not None:
            break
    child.kill()
    err = child.stderr.read() if child.stderr else ""
    raise AssertionError(f"child never became ready; stderr:\n{err}")


async def test_a_killed_process_leaves_its_run_open(tmp_path: Path) -> None:
    """SIGKILL: no handler, no finally, no flush. The marker still has to survive."""
    db = tmp_path / "abrupt.db"
    url = f"sqlite+aiosqlite:///{db}"

    child = start_child(tmp_path, url)
    try:
        run_id, trade_id = wait_for_ready(child)
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=30) != 0
    finally:
        if child.poll() is None:  # pragma: no cover - only on an unexpected hang
            child.kill()
        child.wait(timeout=30)

    journal = journal_path_for(url)
    assert journal is not None and journal.exists(), "the journal outlived the process"

    history = RunJournal.read(journal)
    assert [r.run_id for r in history.unfinished] == [run_id], (
        "the run opened and never closed, which is the only trace a SIGKILL leaves"
    )
    assert not history.is_clean

    # And the work it managed to do before dying is still on disk — the kill must not
    # have cost the durable leg-1 checkpoint.
    records = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    assert any(r["kind"] == "run_started" and r["run_id"] == run_id for r in records)
    assert not any(r["kind"] == "run_finished" for r in records)
    assert trade_id


async def test_a_fresh_process_refuses_to_call_the_crashed_run_complete(
    tmp_path: Path,
) -> None:
    """The point of all of it: the next process must not report a clean run."""
    from decimal import Decimal

    from event_arb.config import (
        OpportunitySettings,
        PaperSettings,
        PolymarketProduct,
        PolymarketSettings,
        Settings,
    )
    from event_arb.paper.reconciliation import reconcile_accounts
    from event_arb.persistence.database import Database
    from event_arb.scanner import Scanner

    db = tmp_path / "abrupt.db"
    url = f"sqlite+aiosqlite:///{db}"
    child = start_child(tmp_path, url)
    try:
        run_id, _ = wait_for_ready(child)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
        child.wait(timeout=30)

    settings = Settings(
        fixture_dir=REPO / "tests" / "fixtures",
        database_url=url,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )
    scanner = Scanner(settings, database=Database(url))
    try:
        # A report, not a recovery: inspection must not touch the crashed run's holds.
        await scanner.inspect_portfolio()
        report = reconcile_accounts(scanner.portfolio)
        assert report.unfinished_runs == [run_id]
        assert not report.evidence_is_complete
        assert "UNFINISHED RUNS" in report.to_text()
        # The exposure the dead process took is still there, and still adds up.
        assert report.trades == 1
        assert report.integrity.is_sound, report.integrity.render()
    finally:
        await scanner.close()
