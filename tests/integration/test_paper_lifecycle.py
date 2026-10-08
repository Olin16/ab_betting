"""The whole paper lifecycle against a real database: fund, execute, restart, settle.

The old suite tested each step and never the sequence, so it passed while a restart threw
the balances away and settlement credited nothing. What matters for a trial is that cash
and exposure survive the joins between the steps, so these tests always cross at least one
process boundary.

Every scanner here is constructed against a temporary SQLite file. The project database is
never touched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import pytest
from sqlalchemy.exc import OperationalError

from event_arb.config import (
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import Outcome, PaperTradeStatus, SettlementAsset
from event_arb.paper.accounts import AccountId
from event_arb.paper.executor import SettlementOutcomes
from event_arb.paper.ledger import EntryKind, LedgerEntry
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import PaperLedgerRepository, PaperTradeRepository
from event_arb.persistence.schema import ensure_schema
from event_arb.scanner import Scanner

pytestmark = pytest.mark.anyio


class RecordedEvidence(TypedDict):
    """The operator-supplied half of a settlement, spelled out so `**` stays checkable."""

    evidence: str
    recorded_by: str


RECORDED: RecordedEvidence = {
    "evidence": "venue settlement notice",
    "recorded_by": "operator",
}


def make_settings(fixture_dir: Path, url: str, **paper: object) -> Settings:
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0, **paper),  # type: ignore[arg-type]
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )


async def opened(settings: Settings) -> Scanner:
    """A scanner with markets, pairs and books loaded from the US fixture corpus."""
    database = Database(settings.database_url)
    await ensure_schema(database)
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.persist_markets()
    await scanner.persist_pairs()
    await scanner.refresh_books()
    return scanner


async def reopened(settings: Settings) -> Scanner:
    """A *fresh process's* view of the same database, restored from storage."""
    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    return scanner


def us_accounts() -> tuple[AccountId, AccountId]:
    return AccountId.parse("kalshi:USD"), AccountId.parse("polymarket_us:USD")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}"


# ---------------------------------------------------------------- restart recovery


async def test_balances_and_history_survive_a_restart(fixture_dir: Path, db_url: str) -> None:
    """Reproduced defect: one trade on disk, zero restored, balances reset to configured.

    Restore loaded markets and approvals and nothing else, so every restart handed the
    trial its starting balances back while the trades that spent them stayed stored.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    opportunity = scanner.scan().all_detected[0]
    trade = await scanner.paper_trade(opportunity)
    before = dict(scanner.portfolio.balances)
    assert trade.status is PaperTradeStatus.BOTH_LEGS_FILLED
    await scanner.close()

    restarted = await reopened(settings)

    assert len(restarted.portfolio.trades) == 1
    assert restarted.portfolio.trades[0].id == trade.id
    assert dict(restarted.portfolio.balances) == before
    report = restarted.last_portfolio_restore
    assert report is not None and report.is_complete
    await restarted.close()


async def test_restoring_twice_does_not_double_apply_the_accounting(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    await scanner.paper_trade(scanner.scan().all_detected[0])
    await scanner.close()

    restarted = await reopened(settings)
    once = dict(restarted.portfolio.balances)
    await restarted.restore_portfolio()
    await restarted.restore_portfolio()

    assert dict(restarted.portfolio.balances) == once
    assert len(restarted.portfolio.trades) == 1
    await restarted.close()


async def test_an_unhedged_position_is_restored_as_open_exposure(
    fixture_dir: Path, db_url: str
) -> None:
    """A failed hedge must still be a liability after a restart."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    trade = await scanner.paper_trade(scanner.scan().all_detected[0])
    trade.leg2 = None
    trade.set_execution_status(PaperTradeStatus.LEG2_FAILED)
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        await PaperTradeRepository(session).record(trade)
    await scanner.close()

    restarted = await reopened(settings)

    assert restarted.portfolio.total_unhedged_quantity > Decimal("0")
    exposure = restarted.portfolio.open_exposure()
    assert len(exposure) == 1
    assert exposure[0]["execution_status"] == PaperTradeStatus.LEG2_FAILED.value
    await restarted.close()


async def test_a_repeated_persist_writes_one_row_not_two(fixture_dir: Path, db_url: str) -> None:
    """Stable trade ids: the same trade reaching storage twice is one row.

    Without them, a retried write doubled the exposure a restart reconstructed.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    trade = await scanner.paper_trade(scanner.scan().all_detected[0])
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        await PaperTradeRepository(session).record(trade)
        await PaperTradeRepository(session).record(trade)
        assert await PaperTradeRepository(session).count() == 1
    await scanner.close()


async def test_an_unreadable_trade_row_is_reported_not_silently_dropped(
    fixture_dir: Path, db_url: str
) -> None:
    """Dropping a row we cannot read would understate exposure — the costly direction."""
    from sqlalchemy import update

    from event_arb.persistence.models import PaperTradeRow

    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    await scanner.paper_trade(scanner.scan().all_detected[0])
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        await session.execute(update(PaperTradeRow).values(opportunity_snapshot={}))
    await scanner.close()

    restarted = await reopened(settings)
    report = restarted.last_portfolio_restore

    assert report is not None
    assert not report.is_complete
    assert report.trades_unreadable
    await restarted.close()


# --------------------------------------------------------------- capital enforcement


async def test_an_unfunded_account_refuses_the_trade_with_a_persisted_reason(
    fixture_dir: Path, db_url: str
) -> None:
    from sqlalchemy import select

    from event_arb.persistence.models import AuditLogRow

    settings = make_settings(
        fixture_dir, db_url, starting_usd_balance=Decimal("0"), starting_usdc_balance=Decimal("0")
    )
    scanner = await opened(settings)

    result = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])

    assert result is None
    assert scanner.portfolio.rejections[-1].reason.value == "insufficient_paper_funds"
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        rows = (
            await session.scalars(
                select(AuditLogRow).where(AuditLogRow.action == "paper_trade.refused")
            )
        ).all()
    assert len(rows) == 1, "the refusal must be durable, not only in memory"
    assert "short" in str(rows[0].detail)
    await scanner.close()


async def test_concurrent_attempts_cannot_overspend_one_account(
    fixture_dir: Path, db_url: str
) -> None:
    """Two coroutines passing the same check against the same cash was the race.

    The check and the reservation are taken under one lock, so the second attempt sees
    the first one's hold rather than the pre-trade balance.
    """
    import asyncio

    kalshi, polymarket = us_accounts()
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    detected = scanner.scan().all_detected
    opportunity = detected[0]
    funding = scanner.portfolio.funding_for(opportunity)
    # Fund each account for exactly one trade.
    scanner.portfolio.ledger._entries.clear()
    scanner.portfolio.ledger._order.clear()
    scanner.portfolio._explicit_opening = dict(funding.requirements)

    outcomes = await asyncio.gather(
        scanner.paper_trade_if_funded(opportunity),
        scanner.paper_trade_if_funded(opportunity),
    )

    assert sum(1 for o in outcomes if o is not None) == 1, "only one may be funded"
    assert scanner.portfolio.balance(kalshi) >= Decimal("0")
    assert scanner.portfolio.balance(polymarket) >= Decimal("0")
    await scanner.close()


async def test_capital_runs_out_as_trades_are_taken(fixture_dir: Path, db_url: str) -> None:
    """Sequential trades must deplete the account. For a long time they did not.

    The concurrent case above was guarded, so the gate looked healthy; the sequential case
    was not tested at all. Automated trading holds cash under the *opportunity's* id and
    then recorded the trade under the *trade's* id, and `record` released unconditionally —
    so each trade wrote a release for a hold nobody took, cancelling its own debit in
    `available` to the cent. An unattended trial could take unlimited trades against an
    account funded for two, which is precisely the exposure the capital gate exists to
    refuse.
    """
    kalshi, polymarket = us_accounts()
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    funding = scanner.portfolio.funding_for(scanner.scan().all_detected[0])
    # Fund each account for exactly two trades, then ask for four.
    scanner.portfolio.ledger._entries.clear()
    scanner.portfolio.ledger._order.clear()
    scanner.portfolio._explicit_opening = {
        account: amount * 2 for account, amount in funding.requirements.items()
    }

    taken = []
    for _ in range(4):
        # A fresh scan each time, so every attempt is its own opportunity with its own
        # detection instant — and therefore its own reservation id.
        opportunity = scanner.scan().all_detected[0]
        taken.append(await scanner.paper_trade_if_funded(opportunity))

    assert sum(1 for t in taken if t is not None) == 2, "an account funded for two buys two"
    assert taken[2] is None and taken[3] is None, "the gate must refuse once the cash is gone"
    assert scanner.portfolio.available(kalshi) >= Decimal("0")
    assert scanner.portfolio.available(polymarket) >= Decimal("0")
    for account in (kalshi, polymarket):
        assert scanner.portfolio.ledger.reserved(account) == Decimal("0.0000"), (
            "every hold taken must be given back exactly once"
        )
    await scanner.close()


@pytest.mark.parametrize("raised", [RuntimeError("execution blew up"), asyncio.CancelledError()])
async def test_a_failing_cleanup_write_never_replaces_the_error_it_is_unwinding(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch, raised: BaseException
) -> None:
    """The release write runs in a `finally`, so its own failure used to win.

    That is how an orderly shutdown got rewritten as a trial error: stopping the runner
    cancelled a paper trade mid-write, the cancellation invalidated the connection, and the
    cleanup write then raised a database error *over* the `CancelledError`. The loop's
    `except asyncio.CancelledError: raise` never saw it, so a clean stop landed in the
    final report as a failure — complete with a consecutive-failure count no operator
    could explain.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    opportunity = scanner.scan().all_detected[0]

    async def blew_up(*args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(scanner, "paper_trade", blew_up)

    real_record_many = PaperLedgerRepository.record_many

    async def failing_release(self: PaperLedgerRepository, entries: list[LedgerEntry]) -> None:
        if any(entry.kind is EntryKind.RELEASE for entry in entries):
            raise OperationalError("no such table: paper_ledger_entries", {}, Exception())
        await real_record_many(self, entries)

    monkeypatch.setattr(PaperLedgerRepository, "record_many", failing_release)

    with pytest.raises(type(raised)) as caught:
        await scanner.paper_trade_if_funded(opportunity)

    assert caught.value is raised, "the cleanup's own failure must not become the error"
    # The cash is still given back in memory, which is what the next start replays against.
    for account in us_accounts():
        assert scanner.portfolio.ledger.reserved(account) == Decimal("0.0000")
    await scanner.close()


async def test_a_hold_left_by_an_interrupted_run_is_reaped_on_the_next_start(
    fixture_dir: Path, db_url: str
) -> None:
    """A process killed between reserving and releasing must not strand cash forever.

    Retention deliberately never compacts an unreleased reservation, so a stranded hold is
    permanent: each interrupted run would shrink the account's usable capital again, and
    the only symptom would be the gate refusing trades the balance plainly covers.
    """
    kalshi, _ = us_accounts()
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    opportunity = scanner.scan().all_detected[0]
    funding = scanner.portfolio.funding_for(opportunity)

    # Exactly what an interrupted run leaves behind: the hold on disk, no release.
    stranded = scanner.portfolio.reserve("hold:from-a-process-that-died", funding)
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        await PaperLedgerRepository(session).record_many(stranded)
    balance_before = scanner.portfolio.balance(kalshi)
    assert scanner.portfolio.available(kalshi) < balance_before
    await scanner.close()

    restarted = await reopened(settings)

    assert restarted.portfolio.available(kalshi) == balance_before
    reaped = restarted.last_portfolio_restore
    assert reaped is not None and reaped.stale_holds_released, "the reaping is reported"
    # And the reaping is durable, or the next start would re-read the same dead hold.
    async with restarted.database.session() as session:  # type: ignore[union-attr]
        stored = await PaperLedgerRepository(session).load_all()
    assert any(e.entry_id.endswith(":release:kalshi:USD") for e in stored)
    await restarted.close()

    again = await reopened(settings)
    second_start = again.last_portfolio_restore
    assert second_start is not None and not second_start.stale_holds_released, (
        "nothing left to reap"
    )
    assert again.portfolio.available(kalshi) == balance_before
    await again.close()


# ---------------------------------------------------- settlement across a restart


async def test_settlement_credits_cash_that_then_survives_a_restart(
    fixture_dir: Path, db_url: str
) -> None:
    """The full loop, which is the only thing that proves the parts join up."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    trade = await scanner.paper_trade(scanner.scan().all_detected[0])
    before = scanner.portfolio.balance(SettlementAsset.USD)

    record = await scanner.settle_paper_trade(
        trade.id, SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
    )
    # Which venue is paid depends on the direction the fixture detected, so the assertion
    # is on the money rather than on a leg: a fully hedged trade pays $1 a contract, once.
    assert record.credited == trade.hedged_quantity
    settled_balances = dict(scanner.portfolio.balances)
    settled_report = scanner.portfolio.daily_report().as_dict()
    assert scanner.portfolio.balance(SettlementAsset.USD) == before + record.credited
    await scanner.close()

    restarted = await reopened(settings)
    restored_report = restarted.portfolio.daily_report().as_dict()

    assert dict(restarted.portfolio.balances) == settled_balances
    # Everything derived from trades survives. ``missed_opportunities`` deliberately does
    # not: rejections are this process's scan artifacts, not positions, and restoring them
    # would report a market's behaviour during a window this process never observed.
    survives = [
        "trades_opened",
        "fully_hedged",
        "partially_hedged",
        "leg2_failures",
        "hedge_success_rate_pct",
        "execution_pnl_estimate",
        "settled_trades",
        "settled_pnl",
        "settled_pnl_by_account",
        "total_fees",
        "evidence",
    ]
    assert {k: restored_report[k] for k in survives} == {k: settled_report[k] for k in survives}
    assert restarted.portfolio.trades[0].is_settled
    assert restarted.portfolio.trades[0].execution_status is PaperTradeStatus.BOTH_LEGS_FILLED
    await restarted.close()


async def test_settling_the_same_trade_twice_across_a_restart_credits_once(
    fixture_dir: Path, db_url: str
) -> None:
    """An operator repeating a command they were unsure about must not double the cash."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    trade = await scanner.paper_trade(scanner.scan().all_detected[0])
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
    await scanner.settle_paper_trade(trade.id, outcomes)
    after_first = dict(scanner.portfolio.balances)
    await scanner.close()

    restarted = await reopened(settings)
    again = await restarted.settle_paper_trade(trade.id, outcomes)

    assert again.already_settled
    assert dict(restarted.portfolio.balances) == after_first
    async with restarted.database.session() as session:  # type: ignore[union-attr]
        entries = await PaperLedgerRepository(session).load_all()
    credits = [e for e in entries if e.kind.value == "settlement_credit"]
    # One entry per account — the losing leg records a zero credit, which is the audit
    # trail saying it settled and paid nothing — and re-settling adds none.
    assert len(credits) == 2
    assert len({e.entry_id for e in credits}) == 2
    await restarted.close()


async def test_settling_an_unknown_trade_is_refused(fixture_dir: Path, db_url: str) -> None:
    from event_arb.paper.settlement import SettlementError

    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    with pytest.raises(SettlementError):
        await scanner.settle_paper_trade(
            "no-such-trade", SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
        )
    await scanner.close()


async def test_unsettled_trades_are_listable_for_the_operator(
    fixture_dir: Path, db_url: str
) -> None:
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    trade = await scanner.paper_trade(scanner.scan().all_detected[0])
    assert [t.id for t in scanner.unsettled_trades()] == [trade.id]

    await scanner.settle_paper_trade(
        trade.id, SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
    )

    assert scanner.unsettled_trades() == []
    await scanner.close()


# ------------------------------------------------------------ the complete lifecycle


async def test_a_full_lifecycle_never_loses_or_invents_cash(fixture_dir: Path, db_url: str) -> None:
    """Fund, enforce, execute, persist, restart, settle, report — and reconcile the cash.

    The single assertion that matters: at every stage, every account's balance equals its
    opening balance plus the sum of its ledger entries. Nothing is derived from a running
    total that could drift, and nothing appears that no entry explains.
    """
    kalshi, polymarket = us_accounts()
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    opening = {
        kalshi: scanner.portfolio.balance(kalshi),
        polymarket: scanner.portfolio.balance(polymarket),
    }

    trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
    assert trade is not None

    def reconciles(portfolio_scanner: Scanner) -> bool:
        ledger = portfolio_scanner.portfolio.ledger
        return all(
            ledger.balance(account)
            == sum(
                (e.amount for e in ledger.entries if e.account == account and e.kind.moves_cash),
                Decimal("0"),
            )
            for account in (kalshi, polymarket)
        )

    assert reconciles(scanner)
    # Cost came out of both accounts, each for its own leg.
    assert scanner.portfolio.balance(kalshi) < opening[kalshi]
    assert scanner.portfolio.balance(polymarket) < opening[polymarket]
    assert scanner.portfolio.balance(SettlementAsset.USDC) == Decimal("0.0000"), (
        "a US trade must never touch a USDC balance"
    )
    await scanner.close()

    restarted = await reopened(settings)
    assert reconciles(restarted)

    await restarted.settle_paper_trade(
        trade.id,
        SettlementOutcomes.for_event(Outcome.YES, **RECORDED),
        settled_at=datetime.now(UTC),
    )
    assert reconciles(restarted)

    report = restarted.portfolio.daily_report()
    assert report.settled_trades == 1
    assert report.fully_hedged == 1, "settling must not erase the fill classification"
    assert restarted.portfolio.total_unhedged_quantity == Decimal("0.0000")
    await restarted.close()

    final = await reopened(settings)
    assert reconciles(final)
    assert final.portfolio.daily_report().settled_pnl == report.settled_pnl
    await final.close()
