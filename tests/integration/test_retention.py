"""Bounded retention on every table that grows with time.

Only orderbook_snapshots was pruned. Detections and paper trades grew without limit, which
a multi-week trial does not survive: at a 5 s cycle with two detections per cycle the
opportunities table projects to ~970,000 rows and ~1.17 GB of snapshot JSON per 28 days.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from event_arb.demo_reviews import load_recorded_reviews
from event_arb.domain.enums import Outcome, PaperTradeStatus
from event_arb.paper.executor import SettlementOutcomes
from event_arb.persistence.database import Database
from event_arb.persistence.models import OpportunityRow, PaperTradeRow
from event_arb.persistence.repositories import PaperTradeRepository
from event_arb.scanner import Scanner

pytestmark = pytest.mark.anyio


def _settings(fixture_dir: Path, **trial: object) -> Settings:
    return Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
        trial=TrialSettings(**trial),  # type: ignore[arg-type]
    )


async def _approved_scanner(settings: Settings, database: Database) -> Scanner:
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.persist_markets()
    await scanner.refresh_all_books()
    scanner.generate_pairs()
    review = load_recorded_reviews(settings)[0]
    for pair_id, pair in scanner.state.pairs.items():
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


async def _count(database: Database, model: type) -> int:
    from sqlalchemy import func, select

    async with database.session() as session:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)


async def test_old_detections_are_pruned(fixture_dir: Path, database: Database) -> None:
    settings = _settings(fixture_dir, opportunity_retention_days=7)
    scanner = await _approved_scanner(settings, database)
    await scanner.scan_and_persist()
    assert await _count(database, OpportunityRow) > 0

    # Age every row past the window.
    from sqlalchemy import update

    async with database.session() as session:
        await session.execute(
            update(OpportunityRow).values(detected_at=datetime.now(UTC) - timedelta(days=30))
        )

    removed = await scanner.prune_snapshots()
    assert removed > 0
    assert await _count(database, OpportunityRow) == 0


async def test_recent_detections_survive_pruning(fixture_dir: Path, database: Database) -> None:
    """Retention must not eat the evidence the trial exists to gather."""
    settings = _settings(fixture_dir, opportunity_retention_days=90)
    scanner = await _approved_scanner(settings, database)
    await scanner.scan_and_persist()
    before = await _count(database, OpportunityRow)

    await scanner.prune_snapshots()

    assert await _count(database, OpportunityRow) == before


async def _age_trades(database: Database, days: int = 400) -> None:
    from sqlalchemy import update

    async with database.session() as session:
        await session.execute(
            update(PaperTradeRow).values(created_at=datetime.now(UTC) - timedelta(days=days))
        )


async def test_old_settled_paper_trades_are_pruned(fixture_dir: Path, database: Database) -> None:
    """A resolved trade past the window is history, and history is bounded."""
    settings = _settings(fixture_dir, paper_trade_retention_days=30)
    scanner = await _approved_scanner(settings, database)
    result = await scanner.scan_and_persist()
    trade = await scanner.paper_trade(result.confirmed[0], simulate_latency=False)
    assert await _count(database, PaperTradeRow) == 1

    await scanner.settle_paper_trade(
        trade.id,
        SettlementOutcomes.for_event(
            Outcome.YES, evidence="fixture settlement", recorded_by="test"
        ),
    )
    assert trade.unhedged_quantity == Decimal("0")
    await _age_trades(database)

    await scanner.prune_snapshots()
    assert await _count(database, PaperTradeRow) == 0


async def test_retention_keeps_an_unresolved_position_however_old_it_is(
    fixture_dir: Path, database: Database
) -> None:
    """An open position is not history, and deleting it would hide live exposure.

    Retention used to delete by age alone, so a leg-2 failure that stayed unhedged for
    longer than the window vanished from the exposure report while the money it had spent
    stayed spent. Age is now necessary but not sufficient: a trade must also be settled
    and carry no unhedged remainder.
    """
    settings = _settings(fixture_dir, paper_trade_retention_days=30)
    scanner = await _approved_scanner(settings, database)
    result = await scanner.scan_and_persist()
    trade = await scanner.paper_trade(result.confirmed[0], simulate_latency=False)
    # Force the open, unhedged shape this test is about.
    trade.leg2 = None
    trade.set_execution_status(PaperTradeStatus.LEG2_FAILED)
    async with database.session() as session:
        await PaperTradeRepository(session).record(trade)
    await _age_trades(database)

    removed = await scanner.prune_snapshots()

    assert await _count(database, PaperTradeRow) == 1, (
        "an unhedged, unsettled trade must survive retention: it is a live position"
    )
    assert trade.unhedged_quantity > Decimal("0")
    assert removed >= 0


async def test_pruning_history_never_moves_a_balance(fixture_dir: Path, database: Database) -> None:
    """Retention compacts the ledger rather than deleting from it.

    Balances are derived from ledger entries, so deleting aged entries outright would
    change a balance as a side effect of bounding a table. Compaction replaces them with
    one carry-forward per account, which is exactly cash-neutral.
    """
    settings = _settings(fixture_dir, paper_trade_retention_days=30)
    scanner = await _approved_scanner(settings, database)
    result = await scanner.scan_and_persist()
    trade = await scanner.paper_trade(result.confirmed[0], simulate_latency=False)
    await scanner.settle_paper_trade(
        trade.id,
        SettlementOutcomes.for_event(
            Outcome.YES, evidence="fixture settlement", recorded_by="test"
        ),
    )
    before = dict(scanner.portfolio.balances)

    await _age_ledger(database)
    await _age_trades(database)
    await scanner.prune_snapshots()

    assert dict(scanner.portfolio.balances) == before

    # And the same balance is what a fresh process reads back.
    reopened = Scanner(settings, database=database)
    await reopened.restore_portfolio()
    assert dict(reopened.portfolio.balances) == before


async def _age_ledger(database: Database, days: int = 400) -> None:
    from sqlalchemy import update

    from event_arb.persistence.models import PaperLedgerEntryRow

    async with database.session() as session:
        await session.execute(
            update(PaperLedgerEntryRow).values(created_at=datetime.now(UTC) - timedelta(days=days))
        )


async def test_a_suppressed_detection_keeps_its_economics_but_not_the_blob(
    fixture_dir: Path, database: Database
) -> None:
    """The blob is ~1.2 kB and near-identical across repeats; the columns are the evidence."""
    from sqlalchemy import select

    settings = _settings(fixture_dir)
    scanner = await _approved_scanner(settings, database)
    for _ in range(3):
        await scanner.scan_and_persist()

    async with database.session() as session:
        rows = (await session.scalars(select(OpportunityRow))).all()

    announced = [r for r in rows if not r.alert_suppressed]
    suppressed = [r for r in rows if r.alert_suppressed]
    assert announced and suppressed, "expected both kinds after repeated scans"

    for row in suppressed:
        assert row.snapshot == {}, "the redundant blob must not be stored per cycle"
        # Everything needed to measure how long an opportunity persisted, and at what size.
        assert row.pair_id and row.direction
        assert row.executable_quantity > 0
        assert row.net_profit is not None
        assert row.detected_at is not None

    for row in announced:
        assert row.snapshot, "the announced row keeps the full leg detail"
