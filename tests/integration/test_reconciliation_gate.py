"""The gate between "the loops stayed healthy" and "the figures are evidence".

A trial can run its full duration with every loop green and still produce numbers a
restart would not reproduce. That was the shape of the reproduced defect: cash debited and
a trade published before the transaction that stored them, so the process reported a
portfolio only it could see. The run exited zero and the report read as a completed trial.

So the exit code now depends on the arithmetic as well as the loops, and `event-arb
reconcile` gives an operator the same check against a database nobody is running.
"""

from __future__ import annotations

import argparse
import json
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
from event_arb.main import _cmd_reconcile, _cmd_trial
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import PaperTradeRepository
from event_arb.persistence.schema import ensure_schema
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED, POLY_FED

pytestmark = pytest.mark.anyio


def make_settings(fixture_dir: Path, url: str, **trial: object) -> Settings:
    options: dict[str, object] = {"observation_only": True, **trial}
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
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
            **options,  # type: ignore[arg-type]
        ),
    )


def trial_args(**over: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "duration": 0.2,
        "rest_only": True,
        "observation_only": True,
        "reconciliation_json": "",
    }
    return argparse.Namespace(**{**defaults, **over})


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}"


async def prepared(settings: Settings, *, approve: bool = False) -> None:
    """Create the schema, and optionally leave an approved pair behind for the run to find.

    `_cmd_trial` builds its own scanner and restores from storage, so an approval recorded
    here is what its preflight sees — which is the point: a paper-trading run with no
    approved pair is refused rather than quietly observing.
    """
    database = Database(settings.database_url)
    await ensure_schema(database)
    if not approve:
        await database.dispose()
        return
    scanner = Scanner(settings, database=database)
    try:
        await scanner.discover()
        await scanner.restore()
        scanner.seed_watched_pairs()
        await scanner.persist_markets()
        for review in load_recorded_reviews(settings):
            for pair_id, pair in list(scanner.state.pairs.items()):
                if review.matches(
                    pair.kalshi_market_id,
                    pair.polymarket_market_id,
                    pair.polymarket_product.value,
                ):
                    await scanner.approve_and_persist(
                        pair_id,
                        approved_by=review.reviewed_by,
                        reviewer_notes=review.notes,
                        kalshi_reviewed_source=review.kalshi_source,
                        polymarket_reviewed_source=review.polymarket_source,
                    )
    finally:
        await scanner.close()


async def test_a_healthy_run_prints_a_reconciliation_and_exits_zero(
    fixture_dir: Path, db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(fixture_dir, db_url)
    await prepared(settings)
    destination = tmp_path / "reconciliation.json"

    code = await _cmd_trial(settings, trial_args(reconciliation_json=str(destination)))
    out = capsys.readouterr().out

    assert code == 0
    assert "Paper reconciliation" in out
    assert "reconciles          : True" in out
    exported = json.loads(destination.read_text())
    assert exported["reconciles"] is True
    assert exported["evidence_is_complete"] is True
    # Per account, never in aggregate: a USD gain and a USDC loss net to nothing useful.
    assert {a["account"] for a in exported["accounts"]} == {
        "kalshi:USD",
        "polymarket_global:USDC",
    }
    for account in exported["accounts"]:
        assert account["balances"] is True


async def test_a_run_that_could_not_store_an_attempt_exits_one(
    fixture_dir: Path,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loops green, books balanced, and still not evidence: an attempt went unrecorded.

    The accounts close here precisely *because* the attempt was never published. What the
    run lost is an observation, and a trial that resumes past that is reporting rates over
    a denominator it knows is short.
    """
    settings = make_settings(fixture_dir, db_url, observation_only=False)
    await prepared(settings, approve=True)

    async def failing(self: PaperTradeRepository, trade: object) -> None:
        raise RuntimeError("disk is read-only")

    monkeypatch.setattr(PaperTradeRepository, "record", failing)
    code = await _cmd_trial(settings, trial_args(duration=1.0, observation_only=False))
    monkeypatch.undo()
    out = capsys.readouterr().out

    assert code == 1
    assert "TRIAL EVIDENCE IS NOT USABLE" in out
    assert "counted in no balance, no total and no rate above" in out
    assert "an account does not balance" not in out, "the books did close; say which fault it was"


async def test_the_reconcile_command_checks_a_database_nobody_is_running(
    fixture_dir: Path, db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator's check, from a fresh process, against storage only."""
    settings = make_settings(fixture_dir, db_url)
    await prepared(settings)
    await _cmd_trial(settings, trial_args())
    capsys.readouterr()

    destination = tmp_path / "after.json"
    code = await _cmd_reconcile(settings, argparse.Namespace(json=str(destination)))
    out = capsys.readouterr().out

    assert code == 0
    assert "reconciles          : True" in out
    assert json.loads(destination.read_text())["reconciles"] is True
