"""What the real entry points do before they touch a venue or an incompatible database.

The preflight module already had thorough tests, and they all passed while both callers
ran it too late to matter: `event-arb trial` and the dashboard each discovered markets
first and consulted the gate afterwards. A scope check that runs after the venue-wide
request it exists to prevent is decoration.

So these tests drive `_cmd_trial` and `create_app` themselves, with discovery mocked to
*count awaits*. The assertion that matters in most of them is `await_count == 0`.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from event_arb.api.app import create_app
from event_arb.config import (
    DataSource,
    IngestionMode,
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
    TrialSettings,
    WatchedPair,
)
from event_arb.main import _cmd_trial
from event_arb.persistence.database import Database
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED

pytestmark = pytest.mark.anyio

US_FED = "fed-cuts-25bps-september-2026"


def live_settings(
    fixture_dir: Path, url: str, *, watched: bool = False, **over: object
) -> Settings:
    """Live-mode settings. No test here is permitted to reach a venue: discovery is mocked."""
    pairs = (
        [WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=US_FED)] if watched else []
    )
    return Settings(
        data_source=DataSource.LIVE,
        ingestion_mode=IngestionMode.REST_POLLING,
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
        trial=TrialSettings(enabled=True, watched_pairs=pairs),
        **over,  # type: ignore[arg-type]
    )


def trial_args(**over: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "duration": 0.2,
        "rest_only": True,
        "observation_only": False,
        "reconciliation_json": "",
    }
    defaults.update(over)
    return argparse.Namespace(**defaults)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'gates.db'}"


# ------------------------------------------------------------------- the CLI entry point


async def test_the_cli_refuses_an_unscoped_live_trial_before_discovering_anything(
    fixture_dir: Path, db_url: str
) -> None:
    """The reproduced defect: one venue-wide discovery, awaited, and then refused.

    `Scanner.discover` falls back to catalogue discovery when no scope is configured, so
    the request the scope check exists to prevent is exactly the one that had already been
    made by the time the check ran.
    """
    settings = live_settings(fixture_dir, db_url, watched=False)

    with patch.object(Scanner, "discover", new_callable=AsyncMock) as discover:
        exit_code = await _cmd_trial(settings, trial_args())

    assert exit_code == 1
    assert discover.await_count == 0, "no venue request may precede the scope refusal"


async def test_the_cli_refuses_an_old_schema_before_reading_the_new_tables(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """Reading tables a migration added, against a database that predates it, is not a check."""
    from sqlalchemy import text

    url = f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}"
    stale = Database(url)
    async with stale.session() as session:
        # A populated database stamped at an older revision: exactly what an operator
        # pointing the trial at last month's file has.
        await session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await session.execute(text("INSERT INTO alembic_version VALUES ('3ea85fd4182a')"))
        await session.execute(text("CREATE TABLE venue_markets (market_key VARCHAR PRIMARY KEY)"))
        await session.execute(text("INSERT INTO venue_markets VALUES ('kalshi:OLD')"))
    await stale.dispose()

    settings = live_settings(fixture_dir, url, watched=True)

    with patch.object(Scanner, "discover", new_callable=AsyncMock) as discover:
        exit_code = await _cmd_trial(settings, trial_args())

    assert exit_code == 1
    assert discover.await_count == 0, "an unusable database must be refused before discovery"


async def test_the_cli_reports_a_watchdog_stop_as_a_failed_run(
    fixture_dir: Path, db_url: str
) -> None:
    """A trial that gave up must not exit zero with a summary that reads like a full run."""
    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        database_url=db_url,
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
        trial=TrialSettings(
            enabled=True,
            observation_only=True,
            refresh_interval_seconds=0.01,
            scan_interval_seconds=0.01,
            health_check_interval_seconds=0.01,
            stop_after_consecutive_failures=1,
        ),
    )

    async def broken(*args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("venue unreachable")

    # Refreshing fails from the first cycle, so the watchdog stops the run almost at once.
    # `--duration` is long enough that returning early can only be the runner waking it.
    with patch.object(Scanner, "refresh_books", new=broken):
        exit_code = await _cmd_trial(settings, trial_args(duration=30.0))

    assert exit_code == 1, "a watchdog stop must reach the process exit status"


async def test_the_cli_exits_zero_when_a_healthy_run_simply_finishes(
    fixture_dir: Path, db_url: str
) -> None:
    """The other half of the contract: a completed observation run is a success."""
    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        database_url=db_url,
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
        trial=TrialSettings(
            enabled=True,
            observation_only=True,
            refresh_interval_seconds=0.01,
            scan_interval_seconds=0.01,
            health_check_interval_seconds=0.01,
        ),
    )

    exit_code = await _cmd_trial(settings, trial_args(duration=0.2))

    assert exit_code == 0


# ------------------------------------------------------------- the dashboard entry point


def dashboard(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def test_the_dashboard_refuses_an_unscoped_live_trial_before_discovering_anything(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """The dashboard had the same ordering defect, and no note printed to anyone."""
    settings = live_settings(
        fixture_dir, f"sqlite+aiosqlite:///{tmp_path / 'dash.db'}", watched=False
    )

    with (
        patch.object(Scanner, "discover", new_callable=AsyncMock) as discover,
        TestClient(create_app(settings)) as client,
    ):
        # Startup completed; the dashboard is serving, just not trialling.
        assert client.get("/health").status_code == 200

    assert discover.await_count == 0, "no venue request may precede the scope refusal"


def test_the_dashboard_starts_no_trial_when_the_static_gate_fails(
    fixture_dir: Path, tmp_path: Path
) -> None:
    settings = live_settings(
        fixture_dir, f"sqlite+aiosqlite:///{tmp_path / 'dash2.db'}", watched=False
    )

    with (
        patch.object(Scanner, "discover", new_callable=AsyncMock),
        TestClient(create_app(settings)) as client,
    ):
        # `TestClient.app` is typed as a bare ASGI callable; this one is the FastAPI
        # instance `create_app` returned, and its `state` is what the gate writes to.
        app = cast(FastAPI, client.app)
        static = app.state.static_preflight
        assert not static.passed
        assert "watched_scope" in {f.code for f in static.blocks}
        assert getattr(app.state, "trial", None) is None


def test_a_fixture_dashboard_still_starts_normally(fixture_dir: Path) -> None:
    """The gate must not break the offline path every other dashboard test relies on."""
    settings = Settings(
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
    )
    with TestClient(create_app(settings, Scanner(settings))) as client:
        assert client.get("/").status_code == 200
