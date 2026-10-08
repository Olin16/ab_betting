"""The gate a live-data paper trial has to pass, and that both entry points share.

Each of these was previously prose in a document or a warning in a log. A warning on an
unattended run scrolls past, and the symptom of ignoring it is not an error — it is a
tidy report of nothing found. So each is now a check that fails closed, and each test
below names the failure it prevents.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

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
from event_arb.demo_reviews import load_recorded_reviews
from event_arb.persistence.database import Database
from event_arb.persistence.schema import SCHEMA_REVISION, ensure_schema, inspect_schema
from event_arb.scanner import Scanner
from event_arb.trial_preflight import Severity, TrialMode, run_preflight
from tests.conftest import KALSHI_FED

pytestmark = pytest.mark.anyio

US_FED = "fed-cuts-25bps-september-2026"


def make_settings(fixture_dir: Path, url: str, *, watched: bool = True, **over: object) -> Settings:
    pairs = (
        [WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=US_FED)] if watched else []
    )
    trial_over = {k: v for k, v in over.items() if k in TrialSettings.model_fields}
    top_over = {k: v for k, v in over.items() if k not in TrialSettings.model_fields}
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
        trial=TrialSettings(enabled=True, watched_pairs=pairs, **trial_over),  # type: ignore[arg-type]
        **top_over,  # type: ignore[arg-type]
    )


async def prepared(settings: Settings, *, approve: bool = True) -> Scanner:
    database = Database(settings.database_url)
    await ensure_schema(database)
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.seed_watched_pairs()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.persist_markets()
    if approve:
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
    await scanner.persist_pairs()
    return scanner


def as_live(settings: Settings, **over: object) -> Settings:
    """A live-mode view of the same configuration.

    Scanner state is always built on the fixture corpus — recorded reviews are fixture-only
    by design, and a live Scanner would construct real venue clients. The preflight reads
    the *settings* for its live-mode rules and the scanner only for state, so handing it a
    live copy exercises those rules without any of that.
    """
    return settings.model_copy(update={"data_source": DataSource.LIVE, **over})


def codes(report: object, severity: Severity) -> set[str]:
    return {f.code for f in report.findings if f.severity is severity}  # type: ignore[attr-defined]


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'preflight.db'}"


# ------------------------------------------------------------------- watched scope


async def test_a_live_trial_without_watched_pairs_is_refused(
    fixture_dir: Path, db_url: str
) -> None:
    """The one configuration that turns a bounded trial into a venue-wide listing.

    With no watched pairs, discovery falls through to an unbounded ``GET /markets`` and
    the trial then generates pairs from whatever came back — overbroad, and silently
    truncated because pagination is unimplemented.
    """
    settings = make_settings(fixture_dir, db_url, watched=False)
    scanner = await prepared(settings, approve=False)

    report = await run_preflight(scanner, as_live(settings), measure_refresh=False)

    assert not report.passed
    assert "watched_scope" in codes(report, Severity.BLOCK)
    await scanner.close()


async def test_a_fixture_run_without_watched_pairs_only_warns(
    fixture_dir: Path, db_url: str
) -> None:
    """Fixture demos legitimately pair the whole corpus; live runs may not."""
    settings = make_settings(fixture_dir, db_url, watched=False)
    scanner = await prepared(settings)

    report = await run_preflight(scanner, settings, measure_refresh=False)

    assert "watched_scope" in codes(report, Severity.WARN)
    assert "watched_scope" not in codes(report, Severity.BLOCK)
    await scanner.close()


# ---------------------------------------------------------------- pair readiness


async def test_no_approved_pair_blocks_a_trading_run(fixture_dir: Path, db_url: str) -> None:
    """It used to print a note and run anyway, reporting an empty trial as a finished one."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings, approve=False)

    report = await run_preflight(scanner, settings, measure_refresh=False)

    assert not report.passed
    assert "pair_readiness" in codes(report, Severity.BLOCK)
    await scanner.close()


async def test_observation_only_is_an_explicit_mode_that_needs_no_approval(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings, approve=False)

    report = await run_preflight(scanner, settings, observation_only=True, measure_refresh=False)

    assert report.mode is TrialMode.OBSERVATION_ONLY
    assert "pair_readiness" not in codes(report, Severity.BLOCK)
    await scanner.close()


async def test_watching_a_pair_is_not_approving_it(fixture_dir: Path, db_url: str) -> None:
    """Configuration selects what to watch; only a human approves."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings, approve=False)
    assert settings.trial.watched_pairs, "the pair is watched"
    assert not [p for p in scanner.state.pairs.values() if p.is_tradable_pair]

    report = await run_preflight(scanner, settings, measure_refresh=False)

    assert "pair_readiness" in codes(report, Severity.BLOCK)
    await scanner.close()


async def test_an_approved_pair_passes(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)

    report = await run_preflight(scanner, settings, measure_refresh=False)

    assert report.passed, report.render()
    await scanner.close()


# ------------------------------------------------------------------- database schema


async def test_an_old_schema_produces_an_actionable_result(fixture_dir: Path, db_url: str) -> None:
    """``create_all()`` does not migrate, so a stale database must be named, not started."""
    from sqlalchemy import text

    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)
    async with scanner.database.engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(text("UPDATE alembic_version SET version_num = '2484faa40d3a'"))

    report = await run_preflight(scanner, as_live(settings), measure_refresh=False)

    assert "database_schema" in codes(report, Severity.BLOCK)
    finding = next(f for f in report.findings if f.code == "database_schema")
    assert "2484faa40d3a" in finding.message and SCHEMA_REVISION in finding.message
    assert "alembic upgrade head" in finding.remedy
    await scanner.close()


async def test_a_fresh_database_is_created_and_stamped_at_head(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    assert (await inspect_schema(database)).is_empty

    state = await ensure_schema(database)

    assert state.is_current and state.revision == SCHEMA_REVISION
    await database.dispose()


async def test_ensure_schema_never_migrates_a_populated_database(tmp_path: Path) -> None:
    """Altering a database with data in it is a decision, not a side effect of starting."""
    from sqlalchemy import text

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await ensure_schema(database)
    async with database.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num = 'older'"))

    state = await ensure_schema(database)

    assert not state.is_current
    assert state.problem is not None and "alembic upgrade head" in state.problem
    await database.dispose()


# ---------------------------------------------------------------- paper accounts


async def test_an_unfunded_account_blocks_a_trading_run(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
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
    scanner = await prepared(settings)

    report = await run_preflight(scanner, settings, measure_refresh=False)

    assert "paper_accounts" in codes(report, Severity.BLOCK)
    await scanner.close()


async def test_the_limits_in_force_are_reported(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)

    report = await run_preflight(scanner, settings, measure_refresh=False)

    limits = next(f for f in report.findings if f.code == "paper_limits")
    assert "max_open_unhedged_quantity" in limits.message
    await scanner.close()


# ------------------------------------------------------------------ ingestion mode


async def test_websocket_on_polymarket_us_is_refused(fixture_dir: Path, db_url: str) -> None:
    """Both venues authenticate their handshake, so streaming needs credentials."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)

    report = await run_preflight(
        scanner,
        as_live(settings, ingestion_mode=IngestionMode.WEBSOCKET),
        measure_refresh=False,
    )

    assert "ingestion_mode" in codes(report, Severity.BLOCK)
    await scanner.close()


# ------------------------------------------------------------------ portfolio state


async def test_a_partly_readable_portfolio_blocks_trading(fixture_dir: Path, db_url: str) -> None:
    """Trading on exposure we could only partly read understates it."""
    from sqlalchemy import update

    from event_arb.persistence.models import PaperTradeRow

    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)
    await scanner.refresh_books()
    await scanner.paper_trade(scanner.scan().all_detected[0])
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        await session.execute(update(PaperTradeRow).values(opportunity_snapshot={}))
    await scanner.restore_portfolio()

    report = await run_preflight(scanner, settings, measure_refresh=False)

    assert "portfolio_restore" in codes(report, Severity.BLOCK)
    await scanner.close()


# ---------------------------------------------------------------- freshness budget


async def test_the_freshness_budget_is_judged_on_a_measured_refresh(
    fixture_dir: Path, db_url: str
) -> None:
    """A pessimistic startup estimate must not be what refuses a workable configuration.

    Blocking on the estimate would make raising ``max_book_age_ms`` look like the fix, and
    raising that bound is precisely how genuinely stale prices stop being rejected.
    """
    settings = make_settings(fixture_dir, db_url)
    settings = settings.model_copy(
        update={"opportunity": settings.opportunity.model_copy(update={"max_book_age_ms": 900})}
    )
    scanner = await prepared(settings)

    # The estimate for one pair (2 markets x 0.4 s + 0.5 s = 1300 ms) exceeds 900 ms.
    assert settings.validate_freshness_budget() is not None
    report = await run_preflight(scanner, settings, measure_refresh=True)

    # Fixture refreshes are effectively instant, so the measurement clears the bound.
    assert "freshness_budget" not in codes(report, Severity.BLOCK), report.render()
    await scanner.close()


async def test_a_genuinely_unachievable_budget_blocks(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
    settings = settings.model_copy(
        update={"opportunity": settings.opportunity.model_copy(update={"max_book_age_ms": 1})}
    )
    scanner = await prepared(settings)

    report = await run_preflight(scanner, settings, measure_refresh=True)

    assert "freshness_budget" in codes(report, Severity.BLOCK)
    finding = next(f for f in report.findings if f.code == "freshness_budget")
    assert "watching fewer pairs" in finding.remedy
    await scanner.close()


async def test_a_refresh_that_cannot_run_blocks_rather_than_reporting_a_quiet_market(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)

    async def broken() -> list[object]:
        raise RuntimeError("venue unreachable")

    scanner.refresh_books = broken  # type: ignore[method-assign,assignment]
    report = await run_preflight(scanner, settings, measure_refresh=True)

    assert "freshness_budget" in codes(report, Severity.BLOCK)
    assert "venue unreachable" in report.render()
    await scanner.close()


# ---------------------------------------------------------------- safety posture


async def test_the_preflight_states_that_live_trading_is_impossible(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await prepared(settings)

    report = await run_preflight(scanner, settings, measure_refresh=False)

    posture = next(f for f in report.findings if f.code == "live_trading_possible")
    assert posture.severity is Severity.INFO
    assert "refuses every submission" in posture.message
    await scanner.close()


async def test_a_failed_preflight_renders_every_blocking_reason(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url, watched=False)
    scanner = await prepared(settings, approve=False)

    report = await run_preflight(scanner, as_live(settings), measure_refresh=False)
    rendered = report.render()

    assert "PREFLIGHT FAILED" in rendered
    assert "nothing was started" in rendered
    for finding in report.blocks:
        assert finding.code in rendered
    await scanner.close()
