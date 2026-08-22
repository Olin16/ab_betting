"""Persistence and restart recovery.

The dangerous startup operation is reinstating an approval. An approval says two contracts
were settlement-equivalent *as their rules read when a human reviewed them*. If a venue
edited its rules while the process was down, restoring that approval verbatim reinstates a
claim nobody has made about the current text. These tests pin that it cannot happen.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import OpportunitySettings, Settings, TrialSettings, WatchedPair
from event_arb.domain.enums import MatchStatus, Venue
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import (
    AuditLogRepository,
    OpportunityRepository,
    OrderBookSnapshotRepository,
    PaperTradeRepository,
)
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED, POLY_FED


@pytest.fixture
async def database() -> Database:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    return db


def make_settings(fixture_dir: Path) -> Settings:
    return Settings(
        fixture_dir=fixture_dir,
        trial=TrialSettings(
            watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
        ),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
    )


async def boot(settings: Settings, database: Database) -> Scanner:
    """Simulate a process start: restore, discover, seed, refresh."""
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.seed_watched_pairs()
    await scanner.refresh_books()
    return scanner


# ------------------------------------------------------------------ round-tripping


async def test_markets_and_pairs_survive_a_restart(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir)
    first = await boot(settings, database)
    pair_id = next(iter(first.state.pairs))
    await first.persist_markets()
    await first.persist_pairs()

    second = Scanner(settings, database=database)
    await second.restore()
    assert pair_id in second.state.pairs
    assert second.state.markets  # metadata came back from storage


async def test_approval_survives_when_rules_are_unchanged(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    first = await boot(settings, database)
    await first.persist_markets()
    pair_id = next(iter(first.state.pairs))
    await first.approve_and_persist(pair_id, approved_by="reviewer@example.com")

    second = await boot(settings, database)
    restored = second.state.pairs[pair_id]
    assert restored.is_tradable_pair is True
    assert restored.approved_by == "reviewer@example.com"
    assert restored.kalshi_rules_hash_at_approval is not None
    assert second.last_restore is not None
    assert second.last_restore.approvals_kept == 1


async def test_decimals_round_trip_exactly_through_storage(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await boot(settings, database)
    await scanner.persist_markets()

    second = Scanner(settings, database=database)
    await second.restore()
    market = second.state.markets[(Venue.KALSHI, KALSHI_FED)]
    assert market.tick_size == Decimal("0.01")
    assert isinstance(market.fee_config.taker_rate, Decimal)
    assert market.fee_config.taker_rate == Decimal("0.07")


# ------------------------------------------------- the approval-hash safety gate


async def test_rules_change_while_down_suspends_the_approval_on_restart(
    fixture_dir: Path, database: Database
) -> None:
    """The central test: a venue rewriting its rules must invalidate a stored approval."""
    settings = make_settings(fixture_dir)
    first = await boot(settings, database)
    await first.persist_markets()
    pair_id = next(iter(first.state.pairs))
    await first.approve_and_persist(pair_id, approved_by="reviewer@example.com")

    # The venue edits its rules while the process is down.
    async with database.session() as session:
        from event_arb.persistence.repositories import VenueMarketRepository

        repository = VenueMarketRepository(session)
        stored = await repository.load_all()
        edited = stored[(Venue.KALSHI, KALSHI_FED)]
        await repository.upsert(
            edited.model_copy(
                update={"rules_text": edited.rules_text + " Threshold changed to 50bps."}
            )
        )

    second = Scanner(settings, database=database)
    await second.restore()

    restored = second.state.pairs[pair_id]
    assert restored.status is MatchStatus.SUSPENDED
    assert restored.manually_approved is False
    assert restored.is_tradable_pair is False
    assert second.last_restore is not None
    assert second.last_restore.approvals_kept == 0
    assert len(second.last_restore.suspended) == 1


async def test_missing_market_metadata_suspends_rather_than_trusts(
    fixture_dir: Path, database: Database
) -> None:
    """We cannot verify what we cannot read, so the approval must not be reinstated."""
    settings = make_settings(fixture_dir)
    first = await boot(settings, database)
    await first.persist_markets()
    pair_id = next(iter(first.state.pairs))
    await first.approve_and_persist(pair_id, approved_by="reviewer@example.com")

    # Restore against a database whose market rows were never written.
    empty = Database("sqlite+aiosqlite:///:memory:")
    await empty.create_all()
    async with empty.session() as session:
        from event_arb.persistence.repositories import MarketPairRepository

        await MarketPairRepository(session).save(first.state.pairs[pair_id])

    second = Scanner(settings, database=empty)
    await second.restore()
    assert second.state.pairs[pair_id].is_tradable_pair is False
    assert second.last_restore is not None
    assert second.last_restore.approvals_kept == 0


async def test_suspension_on_restore_is_audited(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir)
    first = await boot(settings, database)
    await first.persist_markets()
    pair_id = next(iter(first.state.pairs))
    await first.approve_and_persist(pair_id, approved_by="reviewer@example.com")

    async with database.session() as session:
        from event_arb.persistence.repositories import VenueMarketRepository

        repository = VenueMarketRepository(session)
        stored = await repository.load_all()
        edited = stored[(Venue.KALSHI, KALSHI_FED)]
        await repository.upsert(edited.model_copy(update={"rules_text": "totally rewritten"}))

    second = Scanner(settings, database=database)
    await second.restore()

    async with database.session() as session:
        events = await AuditLogRepository(session).recent()
    actions = {e.action for e in events}
    assert "pair.suspended_on_restore" in actions


async def test_unapproved_pairs_restore_untouched(fixture_dir: Path, database: Database) -> None:
    settings = make_settings(fixture_dir)
    first = await boot(settings, database)
    await first.persist_pairs()

    second = Scanner(settings, database=database)
    report = await second.restore()
    assert report.suspended == []
    assert report.approvals_kept == 0


# --------------------------------------------------------------- evidence records


async def test_approval_is_recorded_in_the_audit_log_with_rule_hashes(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await boot(settings, database)
    await scanner.persist_markets()
    pair_id = next(iter(scanner.state.pairs))
    await scanner.approve_and_persist(
        pair_id, approved_by="reviewer@example.com", reviewer_notes="read both rulebooks"
    )

    async with database.session() as session:
        events = await AuditLogRepository(session).recent()
    approval = next(e for e in events if e.action == "pair.approved")
    assert approval.actor == "reviewer@example.com"
    assert approval.detail["kalshi_rules_hash"]
    assert approval.detail["reviewer_notes"] == "read both rulebooks"


async def test_opportunities_and_paper_trades_are_persisted(
    fixture_dir: Path, database: Database
) -> None:
    settings = make_settings(fixture_dir)
    scanner = await boot(settings, database)
    await scanner.persist_markets()
    pair_id = next(iter(scanner.state.pairs))
    await scanner.approve_and_persist(pair_id, approved_by="reviewer@example.com")

    result = await scanner.scan_and_persist()
    assert result.confirmed
    await scanner.paper_trade(result.confirmed[0])

    async with database.session() as session:
        assert await OpportunityRepository(session).count() >= 1
        assert await PaperTradeRepository(session).count() == 1
        events = await AuditLogRepository(session).recent()
    assert "paper_trade.executed" in {e.action for e in events}


async def test_book_snapshots_are_recorded_and_pruned(
    fixture_dir: Path, database: Database
) -> None:
    """Retention is bounded: unbounded archival is outside the venue's storage carve-out."""
    settings = make_settings(fixture_dir)
    settings.trial.snapshot_retention_hours = 0
    scanner = await boot(settings, database)
    books = await scanner.refresh_books()
    await scanner.record_books(books)

    async with database.session() as session:
        assert await OrderBookSnapshotRepository(session).count() == len(books)

    removed = await scanner.prune_snapshots()
    assert removed == len(books)
    async with database.session() as session:
        assert await OrderBookSnapshotRepository(session).count() == 0


async def test_scanner_without_a_database_still_works(fixture_dir: Path) -> None:
    """In-memory operation must remain viable for tests and demos."""
    scanner = Scanner(make_settings(fixture_dir))
    await scanner.discover()
    report = await scanner.restore()
    assert report.restored == []
    scanner.seed_watched_pairs()
    await scanner.refresh_books()
    assert await scanner.prune_snapshots() == 0
