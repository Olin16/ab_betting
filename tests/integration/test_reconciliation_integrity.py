"""Reconciliation has to check the trades, not just re-add the ledger to itself.

``opening + movements == ending`` looks like a check and is not one, because both sides
are sums of the same ledger. Delete a trade's ``trade_debit`` rows and they leave both
sides together: the identity still closes, the cash the trade spent silently reappears,
and the report says the books balance. Reproduced before the fix — one trade retained,
its two debits deleted, Kalshi cash back from 9951.2500 to 9998.2500, ``reconciles=True``.

The second source of truth is the trade rows, stored separately from the ledger. Every
test here corrupts one of the two and checks that the disagreement is found and named.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from event_arb.domain.enums import Outcome
from event_arb.paper.accounts import AccountId
from event_arb.paper.executor import ExecutionInterruptedError, SettlementOutcomes
from event_arb.paper.integrity import verify_trades
from event_arb.paper.reconciliation import reconcile_accounts
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import PaperLedgerRepository
from event_arb.scanner import Scanner
from tests.integration.test_accounting_durability import (
    RECORDED,
    fail_polymarket_book,
    make_settings,
    opened,
    reopened,
)

pytestmark = pytest.mark.anyio

US_ACCOUNTS = ("kalshi:USD", "polymarket_us:USD")


async def a_trade(settings: object) -> str:
    """Execute one funded fixture trade and return its id."""
    scanner = await opened(settings)  # type: ignore[arg-type]
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        return trade.id
    finally:
        await scanner.close()


async def corrupt(settings: object, statement: str) -> int:
    """Run one destructive statement against the disposable test database."""
    db = Database(settings.database_url)  # type: ignore[attr-defined]
    try:
        async with db.session() as session:
            result = await session.execute(sa.text(statement))
            return int(result.rowcount)  # type: ignore[attr-defined]
    finally:
        await db.dispose()


def balances(scanner: object) -> dict[str, str]:
    return {
        key: str(scanner.portfolio.balance(AccountId.parse(key)))  # type: ignore[attr-defined]
        for key in US_ACCOUNTS
    }


# --------------------------------------------------------------- the reproduced case


async def test_deleting_a_trades_debits_is_caught(fixture_dir: Path, db_url: str) -> None:
    """The exact reproduction: cash reappears, and the report must refuse to close."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)

    before = await reopened(settings)
    try:
        spent = balances(before)
    finally:
        await before.close()

    assert await corrupt(settings, "DELETE FROM paper_ledger_entries WHERE kind = 'trade_debit'")

    after = await reopened(settings)
    try:
        assert balances(after) != spent, "the corruption did move the balances"
        report = reconcile_accounts(after.portfolio)
        assert report.trades == 1, "the trade is still retained; only its cash is gone"
        assert not report.reconciles
        assert not report.evidence_is_complete
        codes = {f.code for f in report.integrity.findings}
        assert codes == {"missing_entry"}
        assert len(report.integrity.findings) == 2, "one per leg"
    finally:
        await after.close()


async def test_per_account_arithmetic_alone_still_closes(fixture_dir: Path, db_url: str) -> None:
    """Why the new check was needed: the old one cannot see this at all.

    Pinned deliberately. If this ever starts failing, the arithmetic identity has changed
    shape and the integrity check is no longer covering a different question from it.
    """
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    await corrupt(settings, "DELETE FROM paper_ledger_entries WHERE kind = 'trade_debit'")

    scanner = await reopened(settings)
    try:
        report = reconcile_accounts(scanner.portfolio)
        assert all(a.balances for a in report.accounts), (
            "opening + movements = ending still holds, because both sides lost the "
            "same entries — this is the blind spot"
        )
        assert not report.integrity.is_sound, "and this is what closes it"
    finally:
        await scanner.close()


# ------------------------------------------------------------------ other corruptions


async def test_missing_fees_are_caught(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    assert await corrupt(settings, "DELETE FROM paper_ledger_entries WHERE kind = 'fee_debit'")

    scanner = await reopened(settings)
    try:
        report = verify_trades(scanner.portfolio)
        assert not report.is_sound
        assert {f.code for f in report.findings} == {"missing_entry"}
    finally:
        await scanner.close()


async def test_a_wrong_amount_is_caught(fixture_dir: Path, db_url: str) -> None:
    """Present, plausible, and not what the trade's own fills imply."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    assert await corrupt(
        settings,
        "UPDATE paper_ledger_entries SET amount = amount - 5 WHERE kind = 'trade_debit'",
    )

    scanner = await reopened(settings)
    try:
        report = verify_trades(scanner.portfolio)
        assert not report.is_sound
        assert {f.code for f in report.findings} == {"wrong_amount"}
    finally:
        await scanner.close()


async def test_a_debit_in_the_wrong_account_is_caught(fixture_dir: Path, db_url: str) -> None:
    """The defect a total hides: the aggregate nets, the accounts do not."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    assert await corrupt(
        settings,
        "UPDATE paper_ledger_entries SET account = 'polymarket_us:USD' "
        "WHERE kind = 'trade_debit' AND account = 'kalshi:USD'",
    )

    scanner = await reopened(settings)
    try:
        report = verify_trades(scanner.portfolio)
        assert not report.is_sound
        assert "wrong_account" in {f.code for f in report.findings}
    finally:
        await scanner.close()


async def test_an_unexpected_movement_is_caught(fixture_dir: Path, db_url: str) -> None:
    """Cash that moved for a reason the trade cannot explain."""
    settings = make_settings(fixture_dir, db_url)
    trade_id = await a_trade(settings)
    assert await corrupt(
        settings,
        "INSERT INTO paper_ledger_entries "
        "(entry_id, account, kind, amount, trade_id, detail, created_at) VALUES "
        f"('{trade_id}:phantom', 'kalshi:USD', 'trade_debit', -25, '{trade_id}', "
        "'not implied by any fill', CURRENT_TIMESTAMP)",
    )

    scanner = await reopened(settings)
    try:
        report = verify_trades(scanner.portfolio)
        assert not report.is_sound
        assert "unexpected_entry" in {f.code for f in report.findings}
    finally:
        await scanner.close()


async def test_an_orphan_entry_is_caught(fixture_dir: Path, db_url: str) -> None:
    """The mirror image: a movement charged to a trade nothing retains."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    assert await corrupt(
        settings,
        "INSERT INTO paper_ledger_entries "
        "(entry_id, account, kind, amount, trade_id, detail, created_at) VALUES "
        "('ghost:leg1:cost', 'kalshi:USD', 'trade_debit', -40, 'no-such-trade', "
        "'charged to a trade that is not there', CURRENT_TIMESTAMP)",
    )

    scanner = await reopened(settings)
    try:
        report = verify_trades(scanner.portfolio)
        assert not report.is_sound
        assert "orphan_entry" in {f.code for f in report.findings}
    finally:
        await scanner.close()


async def test_a_bad_settlement_payout_is_caught(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    scanner = await opened(settings)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        await scanner.settle_paper_trade(trade.id, outcomes)
    finally:
        await scanner.close()

    assert await corrupt(
        settings,
        "UPDATE paper_ledger_entries SET amount = amount + 13 WHERE kind = 'settlement_credit'",
    )

    checked = await reopened(settings)
    try:
        report = verify_trades(checked.portfolio)
        assert not report.is_sound
        assert "wrong_amount" in {f.code for f in report.findings}
    finally:
        await checked.close()


async def test_a_missing_settlement_payout_is_caught(fixture_dir: Path, db_url: str) -> None:
    """The trade row says settled; the ledger never received the cash."""
    settings = make_settings(fixture_dir, db_url)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    scanner = await opened(settings)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        await scanner.settle_paper_trade(trade.id, outcomes)
    finally:
        await scanner.close()

    assert await corrupt(
        settings, "DELETE FROM paper_ledger_entries WHERE kind = 'settlement_credit'"
    )

    checked = await reopened(settings)
    try:
        report = verify_trades(checked.portfolio)
        assert not report.is_sound
        assert "missing_entry" in {f.code for f in report.findings}
    finally:
        await checked.close()


# ----------------------------------------------------------------------- the control


async def test_an_intact_run_is_sound(fixture_dir: Path, db_url: str) -> None:
    """A check that failed clean runs too would just move the lie."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)

    scanner = await reopened(settings)
    try:
        report = reconcile_accounts(scanner.portfolio)
        assert report.reconciles
        assert report.evidence_is_complete
        assert report.integrity.is_sound
        assert report.integrity.trades_verified == 1
        assert report.integrity.entries_checked > 0
        assert report.integrity.trades_compacted == 0
    finally:
        await scanner.close()


async def test_a_settled_run_is_sound(fixture_dir: Path, db_url: str) -> None:
    settings = make_settings(fixture_dir, db_url)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    scanner = await opened(settings)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        await scanner.settle_paper_trade(trade.id, outcomes)
    finally:
        await scanner.close()

    checked = await reopened(settings)
    try:
        report = verify_trades(checked.portfolio)
        assert report.is_sound, report.render()
        assert report.trades_verified == 1
    finally:
        await checked.close()


async def test_genuine_leg1_only_exposure_verifies_as_sound(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real interruption implies leg-1 entries only, and must not read as a missing leg 2.

    Injected through the provider boundary the executor actually uses, so the trade is
    interrupted exactly where a real one would be: after leg 1 has filled.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        # Detect first: the injected failure covers every read of the Polymarket book,
        # and detection is one of them.
        opportunity = scanner.scan().all_detected[0]
        fail_polymarket_book(scanner, monkeypatch, RuntimeError("process interrupted"))
        with pytest.raises(ExecutionInterruptedError):
            await scanner.paper_trade_if_funded(opportunity)
        monkeypatch.undo()
    finally:
        await scanner.close()

    checked = await reopened(settings)
    try:
        assert checked.portfolio.total_unhedged_quantity > 0, (
            "the interruption has to leave open exposure or this tests nothing"
        )
        report = verify_trades(checked.portfolio)
        assert report.is_sound, report.render()
        assert report.trades_verified == 1
    finally:
        await checked.close()


async def test_a_lost_leg2_entry_on_a_hedged_trade_is_caught(
    fixture_dir: Path, db_url: str
) -> None:
    """The other side of it: the row claims leg 2 filled, so its cash must be there."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    assert await corrupt(
        settings, "DELETE FROM paper_ledger_entries WHERE entry_id LIKE '%:leg2:%'"
    )

    checked = await reopened(settings)
    try:
        report = verify_trades(checked.portfolio)
        assert not report.is_sound
        assert {f.code for f in report.findings} == {"missing_entry"}
    finally:
        await checked.close()


async def test_compacted_history_is_not_reported_as_corruption(
    fixture_dir: Path, db_url: str
) -> None:
    """Retention deliberately removes per-trade detail. Flagging it would train operators
    to ignore the check, so a compacted trade is counted as unverified, not as corrupt."""
    from datetime import UTC, datetime, timedelta

    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)

    scanner = await reopened(settings)
    try:
        cutoff = datetime.now(UTC) + timedelta(hours=1)
        removed = scanner.portfolio.ledger.compact_before(cutoff, keep_trade_ids=frozenset())
        assert removed > 0
        report = verify_trades(scanner.portfolio)
        assert report.is_sound, report.render()
        assert report.trades_compacted == 1
        assert report.trades_verified == 0
        assert report.compaction_boundary == cutoff
        assert "compacted before" in report.render()
    finally:
        await scanner.close()


async def test_compaction_does_not_excuse_a_trade_above_the_boundary(
    fixture_dir: Path, db_url: str
) -> None:
    """The exemption must not become a blanket amnesty for everything after it, either."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)

    # An old compaction boundary, and a *recent* trade that has lost one entry. The
    # boundary is real but predates the loss, so it cannot account for it.
    await corrupt(
        settings,
        "INSERT INTO paper_ledger_entries "
        "(entry_id, account, kind, amount, trade_id, detail, created_at) VALUES "
        "('carry:old:kalshi:USD', 'kalshi:USD', 'carry_forward', 0, '', "
        "'net of entries pruned long ago', '2020-01-01 00:00:00')",
    )
    assert await corrupt(
        settings,
        "DELETE FROM paper_ledger_entries WHERE kind = 'trade_debit' AND account = 'kalshi:USD'",
    )

    scanner = await reopened(settings)
    try:
        report = verify_trades(scanner.portfolio)
        assert report.compaction_boundary is not None
        assert not report.is_sound, "a loss after the boundary is still a loss"
        assert "missing_entry" in {f.code for f in report.findings}
    finally:
        await scanner.close()


async def test_holds_remain_a_balance_question(fixture_dir: Path, db_url: str) -> None:
    """Reservations net to zero and are never compacted, so they stay with the arithmetic."""
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)

    scanner = await reopened(settings)
    try:
        report = reconcile_accounts(scanner.portfolio)
        assert all(a.holds_are_sane for a in report.accounts)
        assert Decimal(report.accounts[0].as_dict()["reserved"]) >= 0  # type: ignore[arg-type]
    finally:
        await scanner.close()


# ------------------------------------------------- inspection must not be a mutation
#
# `event-arb reconcile` documented itself as read-only and called `restore_portfolio`,
# which reaps holds left by a dead process and persists the release. So inspecting a
# database wrote to it — and inspecting one while a trial was running released the
# reservations that trial was *currently* using. Reporting and recovery are separate
# operations and only one of them belongs in a command an operator runs to read a number.


async def digest(settings: object) -> list[tuple[str, str, str, str]]:
    """Every ledger row, as a comparable snapshot of what is actually stored."""
    db = Database(settings.database_url)  # type: ignore[attr-defined]
    try:
        async with db.session() as session:
            rows = await session.execute(
                sa.text(
                    "SELECT entry_id, account, kind, amount FROM paper_ledger_entries "
                    "ORDER BY entry_id"
                )
            )
            return [(r[0], r[1], r[2], str(r[3])) for r in rows]
    finally:
        await db.dispose()


async def test_inspection_leaves_storage_byte_for_byte_unchanged(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    await a_trade(settings)
    before = await digest(settings)

    scanner = Scanner(settings, database=Database(settings.database_url))
    try:
        await scanner.inspect_portfolio()
        reconcile_accounts(scanner.portfolio)
    finally:
        await scanner.close()

    assert await digest(settings) == before


async def test_inspection_does_not_release_an_outstanding_hold(
    fixture_dir: Path, db_url: str
) -> None:
    """The hold belongs to whoever took it — possibly a trial running right now."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        opportunity = scanner.scan().all_detected[0]
        funding = scanner.portfolio.funding_for(opportunity)
        held = scanner.portfolio.reserve("hold:still-running", funding)
        async with scanner.database.session() as session:  # type: ignore[union-attr]
            await PaperLedgerRepository(session).record_many(held)
    finally:
        await scanner.close()

    before = await digest(settings)

    inspector = Scanner(settings, database=Database(settings.database_url))
    try:
        await inspector.inspect_portfolio()
        report = reconcile_accounts(inspector.portfolio)
        assert report.outstanding_holds, "the hold is reported, not reaped"
    finally:
        await inspector.close()

    assert await digest(settings) == before, "inspecting must not free another run's cash"


async def test_restore_still_reaps_a_stale_hold(fixture_dir: Path, db_url: str) -> None:
    """The recovery path keeps its behaviour: a start *should* reclaim dead cash."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        opportunity = scanner.scan().all_detected[0]
        funding = scanner.portfolio.funding_for(opportunity)
        held = scanner.portfolio.reserve("hold:dead-process", funding)
        async with scanner.database.session() as session:  # type: ignore[union-attr]
            await PaperLedgerRepository(session).record_many(held)
    finally:
        await scanner.close()

    starter = Scanner(settings, database=Database(settings.database_url))
    try:
        restored = await starter.restore_portfolio()
        assert restored.stale_holds_released, "a restart reclaims what a dead run held"
    finally:
        await starter.close()
