"""Accounting that survives failed writes, retries, cancellation and restart.

Every test here injects a failure at a specific point in a real sequence and then asserts
the **exact** balances a fresh process reads back. That is deliberate: the defects this
covers were all invisible in memory. The running process reported settled trades, credited
payouts and released holds; only a restart revealed that storage disagreed.

The rule the fixes establish, and that these tests pin: *storage is never behind what
memory has published in a way a retry cannot repair.* A write may fail, but retrying must
converge, and restarting must not silently lose cash, a hold, a payout, or exposure.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import pytest

from event_arb.config import (
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import Outcome, PaperTradeStatus, Venue
from event_arb.domain.keys import MarketKey
from event_arb.domain.models import OrderBook
from event_arb.paper.accounts import AccountId
from event_arb.paper.durability import DurabilityStage, TradeNotDurableError
from event_arb.paper.executor import ExecutionInterruptedError, SettlementOutcomes
from event_arb.paper.ledger import EntryKind, LedgerEntry
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import PaperLedgerRepository, PaperTradeRepository
from event_arb.persistence.schema import ensure_schema
from event_arb.scanner import Scanner

pytestmark = pytest.mark.anyio


class RecordedEvidence(TypedDict):
    evidence: str
    recorded_by: str


RECORDED: RecordedEvidence = {
    "evidence": "venue settlement notice",
    "recorded_by": "operator",
}


def make_settings(fixture_dir: Path, url: str, **paper: object) -> Settings:
    latencies: dict[str, object] = {"leg1_latency_ms": 0, "leg2_latency_ms": 0, **paper}
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(**latencies),  # type: ignore[arg-type]
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )


async def opened(settings: Settings) -> Scanner:
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
    """A fresh process's view of the same database."""
    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    return scanner


def us_accounts() -> tuple[AccountId, AccountId]:
    return AccountId.parse("kalshi:USD"), AccountId.parse("polymarket_us:USD")


def balances(scanner: Scanner) -> dict[str, str]:
    return {a.key: str(scanner.portfolio.balance(a)) for a in us_accounts()}


def available(scanner: Scanner) -> dict[str, str]:
    return {a.key: str(scanner.portfolio.available(a)) for a in us_accounts()}


# ------------------------------------------------------------------- settlement retries


async def test_a_settlement_retry_repairs_a_payout_its_first_attempt_failed_to_store(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced defect, end to end, asserted on the balance a restart reads.

    Settlement credits the in-memory ledger before the transaction that stores it. When
    that transaction failed, the retry found the credit already applied, had nothing new to
    write, and reported `already_settled=True`. The trade row then persisted as settled
    with its payout missing from the account — permanently, and with no error anywhere.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    _, polymarket = us_accounts()
    trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
    assert trade is not None
    before = scanner.portfolio.balance(polymarket)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)

    real_record = PaperTradeRepository.record

    async def failing(self: PaperTradeRepository, t: object) -> None:
        raise RuntimeError("trade row write failed")

    monkeypatch.setattr(PaperTradeRepository, "record", failing)
    with pytest.raises(RuntimeError, match="trade row write failed"):
        await scanner.settle_paper_trade(trade.id, outcomes)
    monkeypatch.setattr(PaperTradeRepository, "record", real_record)

    # Exactly what an operator does when they are unsure the first attempt landed.
    record = await scanner.settle_paper_trade(trade.id, outcomes)
    assert record.entries, "a retry must have something to re-persist"
    credited = scanner.portfolio.balance(polymarket)
    assert credited > before
    await scanner.close()

    restarted = await reopened(settings)

    assert restarted.portfolio.balance(polymarket) == credited, (
        "the payout must survive the restart, not just the retry"
    )
    settled = next(t for t in restarted.portfolio.trades if t.id == trade.id)
    assert settled.is_settled
    await restarted.close()


async def test_settling_twice_after_a_clean_first_attempt_still_credits_once(
    fixture_dir: Path, db_url: str
) -> None:
    """Re-persisting the full entry set must not become a second credit."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    _, polymarket = us_accounts()
    trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
    assert trade is not None
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)

    await scanner.settle_paper_trade(trade.id, outcomes)
    once = scanner.portfolio.balance(polymarket)
    again = await scanner.settle_paper_trade(trade.id, outcomes)

    assert again.already_settled
    assert scanner.portfolio.balance(polymarket) == once
    await scanner.close()

    restarted = await reopened(settings)
    assert restarted.portfolio.balance(polymarket) == once
    credits = [
        e
        for e in restarted.portfolio.ledger.entries
        if e.kind is EntryKind.SETTLEMENT_CREDIT and e.trade_id == trade.id
    ]
    assert len(credits) == len({e.entry_id for e in credits}), "no duplicated credit rows"
    await restarted.close()


async def test_a_ledger_write_failure_during_settlement_is_repaired_by_a_retry(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same transaction, failing on its own."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    _, polymarket = us_accounts()
    trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
    assert trade is not None
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)

    real = PaperLedgerRepository.record_many

    async def failing(self: PaperLedgerRepository, entries: list[object]) -> None:
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(PaperLedgerRepository, "record_many", failing)
    with pytest.raises(RuntimeError, match="ledger write failed"):
        await scanner.settle_paper_trade(trade.id, outcomes)
    monkeypatch.setattr(PaperLedgerRepository, "record_many", real)

    await scanner.settle_paper_trade(trade.id, outcomes)
    credited = scanner.portfolio.balance(polymarket)
    await scanner.close()

    restarted = await reopened(settings)
    assert restarted.portfolio.balance(polymarket) == credited
    await restarted.close()


async def test_a_failed_settlement_write_is_reported_rather_than_only_raised(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settlement is the one place memory may lead storage, so the lead has to be visible.

    The credit is idempotent and a repeat converges, but until the operator repeats it the
    run is quoting cash a restart would not find. An exception at a command line is not a
    record of that; the report has to say so.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        assert scanner.portfolio.daily_report().is_complete_evidence

        async def failing(self: PaperLedgerRepository, entries: list[object]) -> int:
            raise RuntimeError("ledger write failed")

        monkeypatch.setattr(PaperLedgerRepository, "record_many", failing)
        with pytest.raises(RuntimeError, match="ledger write failed"):
            await scanner.settle_paper_trade(
                trade.id, SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
            )
        monkeypatch.undo()

        divergence = scanner.state.invalidated_attempts
        assert len(divergence) == 1
        assert divergence[0].stage is DurabilityStage.SETTLEMENT
        assert "restart" in divergence[0].consequence
        assert not scanner.portfolio.daily_report().is_complete_evidence

        # The repeat an operator runs. It converges, and the run stops being flagged.
        await scanner.settle_paper_trade(
            trade.id, SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
        )
        assert scanner.state.invalidated_attempts == []
        assert scanner.portfolio.daily_report().is_complete_evidence
    finally:
        await scanner.close()


async def test_a_restart_straight_after_a_failed_settlement_shows_it_unsettled(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No retry, immediate restart: storage is authoritative and says not settled.

    That is the correct answer rather than a lost payout — the trade is still open, the
    operator settles it again, and the deterministic entry ids make that credit once.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        traded = balances(scanner)

        async def failing(self: PaperTradeRepository, t: object) -> None:
            raise RuntimeError("storage went away")

        monkeypatch.setattr(PaperTradeRepository, "record", failing)
        with pytest.raises(RuntimeError, match="storage went away"):
            await scanner.settle_paper_trade(trade.id, outcomes)
        monkeypatch.undo()
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert balances(restarted) == traded, "the uncommitted credit is simply not there"
        recovered = next(t for t in restarted.portfolio.trades if t.id == trade.id)
        assert not recovered.is_settled
        assert recovered in restarted.unsettled_trades()

        record = await restarted.settle_paper_trade(trade.id, outcomes)
        assert not record.already_settled
        credited = balances(restarted)
        assert credited != traded
    finally:
        await restarted.close()

    third = await reopened(settings)
    try:
        assert balances(third) == credited, "credited exactly once, and it stuck"
    finally:
        await third.close()


# --------------------------------------------------------------- reservations and holds


async def test_a_failed_reservation_write_does_not_strand_the_hold(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisting the hold sat *above* the try, so its failure skipped the cleanup.

    The hold stayed in the in-memory ledger with nothing to release it, reserving that
    cash against a trade that had not even been attempted — for as long as the process ran.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    opportunity = scanner.scan().all_detected[0]
    before = available(scanner)

    async def failing(self: PaperLedgerRepository, entries: list[object]) -> None:
        raise RuntimeError("reservation write failed")

    monkeypatch.setattr(PaperLedgerRepository, "record_many", failing)
    with pytest.raises(RuntimeError, match="reservation write failed"):
        await scanner.paper_trade_if_funded(opportunity)

    for account in us_accounts():
        assert scanner.portfolio.ledger.reserved(account) == Decimal("0.0000"), (
            "a hold whose write failed must not outlive the attempt"
        )
    assert available(scanner) == before
    await scanner.close()


async def test_a_hold_is_released_exactly_once_across_a_failed_release_write(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release write may fail; the next start must still reap the hold."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    opportunity = scanner.scan().all_detected[0]
    funding = scanner.portfolio.funding_for(opportunity)

    stranded = scanner.portfolio.reserve("hold:interrupted-run", funding)
    async with scanner.database.session() as session:  # type: ignore[union-attr]
        await PaperLedgerRepository(session).record_many(stranded)
    expected = balances(scanner)
    await scanner.close()

    restarted = await reopened(settings)

    assert available(restarted) == expected, "the reaped hold frees exactly what it held"
    for account in us_accounts():
        assert restarted.portfolio.ledger.reserved(account) == Decimal("0.0000")
    await restarted.close()

    third = await reopened(settings)
    assert available(third) == expected, "reaping is idempotent across restarts"
    await third.close()


# ------------------------------------------------------- interruption between the legs
#
# These inject through the **real** provider boundary. `paper_trade` builds its book
# provider as a local closure, so there is no `_book_for_leg` to patch — an earlier
# version of these tests patched that name with `raising=False` and therefore executed an
# ordinary, fully hedged trade while asserting nothing. The boundary that actually exists
# is the store read the closure performs for each leg, so failing that for Polymarket
# only interrupts the attempt exactly where a real one would: after leg 1 has filled.


def fail_polymarket_book(
    scanner: Scanner, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Make the leg-2 book read raise, leaving leg 1's read untouched."""
    real_get = scanner.store.get

    def failing(key: MarketKey) -> OrderBook | None:
        if key[0] is Venue.POLYMARKET:
            raise error
        return real_get(key)

    monkeypatch.setattr(scanner.store, "get", failing)


async def test_an_interruption_between_the_legs_keeps_the_exposure_it_created(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leg 1 filled, then the process was interrupted. The position is real.

    `execute` awaits leg 2's book after leg 1 has already filled, and a cancellation there
    used to propagate bare — taking the trade with it. Nothing recorded the fill, so
    simulated exposure that had genuinely been taken simply stopped existing: the one
    direction of error that understates risk.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        opportunity = scanner.scan().all_detected[0]
        fail_polymarket_book(scanner, monkeypatch, asyncio.CancelledError())

        # A cancellation stays a cancellation. See the shutdown test below.
        with pytest.raises(asyncio.CancelledError):
            await scanner.paper_trade_if_funded(opportunity)

        attempt = scanner.state.paper_trades[-1]
        assert attempt.execution_status is PaperTradeStatus.INTERRUPTED
        assert attempt.leg1 is not None and attempt.leg1.filled_quantity > Decimal("0"), (
            "the interruption has to land after a leg-1 fill or it tests nothing"
        )
        open_exposure = scanner.portfolio.total_unhedged_quantity
        assert open_exposure == attempt.leg1.filled_quantity
        expected = balances(scanner)
        spent = attempt.leg1.gross_cost + attempt.kalshi_fees
        assert scanner.portfolio.balance(us_accounts()[0]) == Decimal("10000") - spent, (
            "leg 1's cash and fees are spent; only leg 2's are not"
        )
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert restarted.portfolio.total_unhedged_quantity == open_exposure, (
            "an interrupted attempt must be rebuilt as open exposure, not lost"
        )
        assert balances(restarted) == expected, "memory and a restart agree to the cent"
        carried = [
            t
            for t in restarted.portfolio.trades
            if t.execution_status is PaperTradeStatus.INTERRUPTED
        ]
        assert len(carried) == 1, "the attempt is recorded, and recorded as incomplete"
        assert carried[0].leg2 is None
        for account in us_accounts():
            assert restarted.portfolio.ledger.reserved(account) == Decimal("0.0000"), (
                "the hold is released even though the attempt never finished"
            )
    finally:
        await restarted.close()


async def test_an_interrupted_attempt_is_not_counted_as_hedge_evidence(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leg 2 was never attempted, so the attempt says nothing about hedging."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        # Detect first: the injected failure covers every read of the Polymarket book,
        # and detection is one of them.
        opportunity = scanner.scan().all_detected[0]
        fail_polymarket_book(scanner, monkeypatch, RuntimeError("process interrupted"))

        with pytest.raises(ExecutionInterruptedError):
            await scanner.paper_trade_if_funded(opportunity)

        trade = scanner.state.paper_trades[-1]
        # INTERRUPTED, specifically not LEG2_FAILED: that status means the hedge *was*
        # tried and the book was unusable, which is evidence about hedging. This is the
        # absence of evidence, and the report below is where the difference shows up.
        assert trade.execution_status is PaperTradeStatus.INTERRUPTED
        assert any("never tried" in note for note in trade.notes)

        report = scanner.portfolio.daily_report()
        assert report.fully_hedged == 0, "an attempt with no leg 2 is not a held hedge"
        assert report.leg2_failures == 0, "nor is it a failed one — leg 2 did not happen"
        assert report.total_unhedged_quantity == trade.leg1.filled_quantity  # type: ignore[union-attr]
    finally:
        await scanner.close()


async def test_an_operator_shutdown_between_the_legs_is_not_an_execution_failure(
    fixture_dir: Path, db_url: str
) -> None:
    """Cancelling the running task must arrive at the caller as a cancellation.

    `execute` wraps the cancellation in `ExecutionInterruptedError` so the partial exposure
    reaches a caller that can store it. Once it has been stored, re-raising the wrapper
    instead of the cancellation would turn an orderly stop into a trial error — a
    consecutive-failure count and a non-zero exit for a shutdown the operator asked for.
    """
    settings = make_settings(fixture_dir, db_url, leg2_latency_ms=200)
    scanner = await opened(settings)
    try:
        opportunity = scanner.scan().all_detected[0]
        task = asyncio.ensure_future(
            scanner.paper_trade_if_funded(opportunity, simulate_latency=True)
        )
        # Long enough for leg 1 to fill and checkpoint, short enough to land in leg 2's wait.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert scanner.state.paper_trades, "the leg-1 fill survived the cancellation"
        attempt = scanner.state.paper_trades[-1]
        assert attempt.execution_status is PaperTradeStatus.INTERRUPTED
        assert not scanner.state.invalidated_attempts, (
            "a clean shutdown is not a durability failure"
        )
        expected = balances(scanner)
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert balances(restarted) == expected
        assert restarted.portfolio.total_unhedged_quantity > Decimal("0")
    finally:
        await restarted.close()


async def test_an_abrupt_stop_between_the_legs_still_restores_the_leg1_exposure(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint, not the exception handler, is what makes the exposure durable.

    Recording the exposure only in the `except` clause is a recovery path that assumes the
    failure it recovers from did not happen: a process killed outright between the legs
    never reaches it. Here the handler is disabled entirely, standing in for a process
    that died before it could run, and the leg-1 checkpoint alone has to carry the
    position across the restart.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)

    async def never_runs(self: Scanner, interrupted: ExecutionInterruptedError) -> None:
        return None

    try:
        opportunity = scanner.scan().all_detected[0]
        monkeypatch.setattr(Scanner, "_publish_interrupted", never_runs)
        fail_polymarket_book(scanner, monkeypatch, RuntimeError("host went away"))
        with pytest.raises(ExecutionInterruptedError):
            await scanner.paper_trade_if_funded(opportunity)
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert restarted.portfolio.total_unhedged_quantity > Decimal("0"), (
            "the checkpoint written before leg 2 is what a restart reads"
        )
        carried = [
            t
            for t in restarted.portfolio.trades
            if t.execution_status is PaperTradeStatus.LEG1_FILLED
        ]
        assert len(carried) == 1
        assert carried[0].leg2 is None
    finally:
        await restarted.close()


# ------------------------------------------------------ writes that storage will not take


async def test_a_trade_whose_write_fails_is_never_published_to_the_portfolio(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced blocker, asserted from both sides of a restart.

    Cash was debited and the trade appended to the portfolio *before* the transaction that
    stored them. With the trade write failing, memory held one trade and balances of
    9951.2500 / 9960.5900; reopening the same database showed zero trades and 10000.0000
    in both accounts, and the restore report called that complete.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        before = balances(scanner)

        async def failing(self: PaperTradeRepository, t: object) -> None:
            raise RuntimeError("trade row write failed")

        monkeypatch.setattr(PaperTradeRepository, "record", failing)
        with pytest.raises(TradeNotDurableError):
            await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        monkeypatch.undo()

        assert scanner.portfolio.trades == [], "an unstored attempt is not a position"
        assert balances(scanner) == before, "and its cash was never spent"
        for account in us_accounts():
            assert scanner.portfolio.ledger.reserved(account) == Decimal("0.0000")

        assert len(scanner.state.invalidated_attempts) == 1, (
            "the lost observation is named rather than resumed past"
        )
        invalid = scanner.state.invalidated_attempts[0]
        assert invalid.stage is DurabilityStage.CHECKPOINT
        assert "trade row write failed" in invalid.error
        report = scanner.portfolio.daily_report()
        assert not report.is_complete_evidence
        assert "INCOMPLETE" in report.evidence_caveat
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert balances(restarted) == before, "memory and the restart agree"
        assert restarted.portfolio.trades == []
        assert restarted.portfolio.total_unhedged_quantity == Decimal("0")
    finally:
        await restarted.close()


async def test_a_failed_write_after_a_clean_checkpoint_invalidates_only_that_attempt(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint landed and the completing write did not: leg 1's exposure stands.

    This is the case where invalidation must *not* be a rollback. The leg-1 entries are
    already committed — possibly interleaved with another coroutine's — so unwinding them
    would destroy durable state belonging to something else. What the run loses is leg 2's
    accounting, and the attempt stays on the books as unhedged exposure, which is the
    conservative direction.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        real = PaperTradeRepository.record
        calls = {"n": 0}

        async def failing_after_checkpoint(self: PaperTradeRepository, t: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                await real(self, t)
                return
            raise RuntimeError("storage went away mid-attempt")

        monkeypatch.setattr(PaperTradeRepository, "record", failing_after_checkpoint)
        with pytest.raises(TradeNotDurableError):
            await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        monkeypatch.undo()

        assert len(scanner.state.invalidated_attempts) == 1
        assert scanner.state.invalidated_attempts[0].stage is DurabilityStage.TRADE
        checkpointed = scanner.portfolio.trades[-1]
        assert checkpointed.execution_status is PaperTradeStatus.LEG1_FILLED
        assert checkpointed.leg2 is None, "leg 2's accounting is what was lost"
        expected = balances(scanner)
        exposure = scanner.portfolio.total_unhedged_quantity
        assert exposure > Decimal("0")
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert balances(restarted) == expected, "leg 1's committed cash is not rolled back"
        assert restarted.portfolio.total_unhedged_quantity == exposure
    finally:
        await restarted.close()


async def test_a_transient_write_failure_is_repaired_by_the_retry_without_re_executing(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One blip, then the same write lands. The trade is stored exactly once."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        real = PaperLedgerRepository.record_many
        calls = {"n": 0}

        async def failing_once(self: PaperLedgerRepository, entries: list[object]) -> int:
            calls["n"] += 1
            if calls["n"] == 2:  # the leg-1 checkpoint's write, after the reservation's
                raise RuntimeError("database is locked")
            written: int = await real(self, entries)
            return written

        monkeypatch.setattr(PaperLedgerRepository, "record_many", failing_once)
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        monkeypatch.undo()

        assert trade is not None, "a retried write must not lose the attempt"
        assert not scanner.state.invalidated_attempts
        expected = balances(scanner)
    finally:
        await scanner.close()

    restarted = await reopened(settings)
    try:
        assert balances(restarted) == expected
        stored = [t for t in restarted.portfolio.trades if t.id == trade.id]
        assert len(stored) == 1, "the retry converged rather than duplicating"
        for account in us_accounts():
            entries = [
                e
                for e in restarted.portfolio.ledger.entries
                if e.account == account and e.trade_id == trade.id
            ]
            assert len(entries) == len({e.entry_id for e in entries}), "no doubled debit"
    finally:
        await restarted.close()


async def test_a_failed_partial_write_leaves_neither_half_behind(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trade row and its cash entries are one transaction or they are nothing.

    A stored trade whose ledger entries never landed reconstructs as a position with no
    cost; the reverse reconstructs as cash spent on nothing. Either is a portfolio that
    cannot be reconciled, so the write that half-failed must leave the database exactly
    as it found it.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    try:
        before = balances(scanner)

        real = PaperLedgerRepository.record_many

        async def failing(self: PaperLedgerRepository, entries: list[LedgerEntry]) -> int:
            # Only the trade's own entries. The reservation is written through the same
            # method first, and failing that would abort before a trade row was ever
            # added — which is a different, already-covered case.
            if any(e.kind is EntryKind.TRADE_DEBIT for e in entries):
                raise RuntimeError("ledger write failed after the trade row was added")
            written: int = await real(self, entries)
            return written

        monkeypatch.setattr(PaperLedgerRepository, "record_many", failing)
        with pytest.raises(TradeNotDurableError):
            await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        monkeypatch.undo()

        async with scanner.database.session() as session:  # type: ignore[union-attr]
            assert await PaperTradeRepository(session).count() == 0, (
                "the trade row rolled back with the entries it belongs to"
            )
        assert balances(scanner) == before
    finally:
        await scanner.close()


# ----------------------------------------------------------- nothing is lost or doubled


async def test_a_full_sequence_of_failures_leaves_the_books_reconciled(
    fixture_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trade, failed settlement, retry, restart — and every figure still adds up.

    The invariant: a restored balance equals the opening balance plus every ledger entry,
    and nothing is held against a trade that is finished.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings)
    trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
    assert trade is not None
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)

    async def failing(self: PaperTradeRepository, t: object) -> None:
        raise RuntimeError("storage blip")

    real = PaperTradeRepository.record
    monkeypatch.setattr(PaperTradeRepository, "record", failing)
    with pytest.raises(RuntimeError):
        await scanner.settle_paper_trade(trade.id, outcomes)
    monkeypatch.setattr(PaperTradeRepository, "record", real)
    await scanner.settle_paper_trade(trade.id, outcomes)

    expected = balances(scanner)
    await scanner.close()

    restarted = await reopened(settings)

    assert balances(restarted) == expected
    for account in us_accounts():
        assert restarted.portfolio.ledger.reserved(account) == Decimal("0.0000"), (
            "a finished trade holds nothing"
        )
        # Balances are derived, so this is the arithmetic the whole ledger rests on.
        entries = [
            e
            for e in restarted.portfolio.ledger.entries
            if e.account == account and e.kind.moves_cash
        ]
        assert restarted.portfolio.balance(account) == sum(e.amount for e in entries)
    await restarted.close()
