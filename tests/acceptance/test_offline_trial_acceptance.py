"""The unattended-behaviour exercise: an accelerated trial, deliberately broken, reconciled.

This is not another unit test and an aggregate test count is not a substitute for it. Each
scenario below assembles a *real* `TrialRunner` over fixture data with fresh temporary
storage, injects one failure an unattended run actually meets, lets the runtime respond,
and then re-derives every account from the ledger on disk. The question each one answers is
the same, and it is the only question that matters before a trial runs unwatched:

    after this went wrong, do the books still close, and does the run say so?

Every scenario writes a reconciliation artifact to ``evidence_dir``. Run with
``EVENT_ARB_ACCEPTANCE_OUT`` set to keep them; see ``tests/acceptance/conftest.py``.

What is deliberately *not* claimed here: none of this is evidence about hedge quality.
Fixture books cannot move, so leg 2 is graded ``unchanged_reread`` throughout, and a
scenario that ends fully hedged shows the accounting held — not that hedging would.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Iterator
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
from event_arb.domain.enums import Outcome, PaperTradeStatus, Venue
from event_arb.domain.keys import MarketKey
from event_arb.domain.models import OrderBook
from event_arb.opportunities.service import ScanResult
from event_arb.paper.accounts import AccountId
from event_arb.paper.durability import DurabilityStage
from event_arb.paper.executor import ExecutionInterruptedError, SettlementOutcomes
from event_arb.paper.reconciliation import ReconciliationReport, reconcile_accounts
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import PaperTradeRepository
from event_arb.persistence.schema import ensure_schema
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED, POLY_FED

pytestmark = pytest.mark.anyio

RECORDED = {"evidence": "fixture settlement notice", "recorded_by": "acceptance-exercise"}


# ------------------------------------------------------------------------ scaffolding


def make_settings(fixture_dir: Path, url: str, **trial: object) -> Settings:
    """An accelerated trial: real loops, real gates, intervals compressed to milliseconds."""
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        paper=PaperSettings(
            leg1_latency_ms=0,
            leg2_latency_ms=0,
            starting_usd_balance=Decimal("10000"),
            starting_usdc_balance=Decimal("10000"),
            max_trade_notional=Decimal("500"),
            max_open_notional=Decimal("2000"),
            max_open_unhedged_quantity=Decimal("200"),
        ),
        opportunity=OpportunitySettings(
            max_book_age_ms=60_000, max_quantity=Decimal("100"), alert_cooldown_seconds=0
        ),
        trial=TrialSettings(
            enabled=True,
            watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)],
            refresh_interval_seconds=0.01,
            scan_interval_seconds=0.01,
            health_check_interval_seconds=0.01,
            **trial,  # type: ignore[arg-type]
        ),
    )


async def opened(settings: Settings, *, fresh: bool) -> Scanner:
    """A process's view of the trial database, approvals and all."""
    database = Database(settings.database_url)
    if fresh:
        await ensure_schema(database)
    scanner = Scanner(settings, database=database)
    await scanner.discover()
    await scanner.restore()
    scanner.seed_watched_pairs()
    await scanner.persist_markets()
    for review in load_recorded_reviews(settings):
        for pair_id, pair in list(scanner.state.pairs.items()):
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
    await scanner.refresh_books()
    return scanner


async def run_until(
    runner: TrialRunner, done: Callable[[TrialRunner], bool], give_up_after: float = 5.0
) -> None:
    await runner.start()
    try:
        deadline = asyncio.get_running_loop().time() + give_up_after
        while asyncio.get_running_loop().time() < deadline:
            if done(runner):
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"condition not met within {give_up_after}s: {runner.stats.as_dict()}")
    finally:
        await runner.stop()


def record(evidence_dir: Path, name: str, report: ReconciliationReport, notes: list[str]) -> None:
    """Write one scenario's reconciliation, and fail the exercise if it does not close."""
    payload = report.as_dict()
    payload["scenario"] = name
    payload["notes"] = notes
    (evidence_dir / f"{name}.json").write_text(json.dumps(payload, indent=2) + "\n")
    (evidence_dir / f"{name}.txt").write_text(
        f"scenario: {name}\n"
        + "\n".join(f"  note: {n}" for n in notes)
        + "\n\n"
        + report.to_text()
        + "\n"
    )
    for account in report.accounts:
        assert account.balances, (
            f"{name}: {account.account} does not close — "
            f"{account.opening} + {account.movements} != {account.ending}"
        )
        assert account.holds_are_sane, f"{name}: {account.account} has negative reserved cash"


async def reconciled_from_storage(settings: Settings) -> ReconciliationReport:
    """What a fresh process reads back. The only view that proves anything durable."""
    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    try:
        restored = await scanner.restore_portfolio()
        report = reconcile_accounts(scanner.portfolio)
        if not restored.is_complete:
            report.stop_causes.append(
                f"restore incomplete: {len(restored.trades_unreadable)} unreadable trade row(s)"
            )
        return report
    finally:
        await scanner.close()


def accounts_of(report: ReconciliationReport) -> dict[str, Decimal]:
    return {a.account: a.ending for a in report.accounts}


@contextlib.contextmanager
def failing_polymarket_book(scanner: Scanner, error: BaseException) -> Iterator[None]:
    """Break the leg-2 book read — the real provider boundary — and put it back after."""
    real = scanner.store.get

    def failing(key: MarketKey) -> OrderBook | None:
        if key[0] is Venue.POLYMARKET:
            raise error
        return real(key)

    scanner.store.get = failing  # type: ignore[method-assign]
    try:
        yield
    finally:
        scanner.store.get = real  # type: ignore[method-assign]


# --------------------------------------------------------------------------- scenarios


async def test_a_dead_feed_pauses_trading_and_the_books_still_close(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Every refresh fails. The trial must stop trading, not trade on books it cannot confirm."""
    settings = make_settings(
        fixture_dir, db_url, pause_after_consecutive_failures=2, stop_after_consecutive_failures=6
    )
    scanner = await opened(settings, fresh=True)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def dead_feed(_only: object = None) -> list[OrderBook]:
        raise RuntimeError("venue feed is unreachable")

    scanner.refresh_books = dead_feed  # type: ignore[method-assign,assignment]
    try:
        await run_until(runner, lambda r: r.stats.stopped_reason is not None)
    finally:
        await scanner.close()

    assert runner.stats.stopped_reason, "a feed that never recovers has to end the run"
    report = await reconciled_from_storage(settings)
    report.stop_causes.append(str(runner.stats.stopped_reason))
    record(
        evidence_dir,
        "01-dead-feed",
        report,
        [
            "every refresh raised; the watchdog paused trading and then stopped the run",
            f"stopped_reason: {runner.stats.stopped_reason}",
            "no trade was attempted against an unconfirmed book",
        ],
    )
    assert report.trades == 0
    assert report.reconciles


async def test_a_failing_scan_stops_the_run_without_disturbing_the_accounts(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Detection itself is broken. Nothing executes, and nothing in the ledger moves."""
    settings = make_settings(
        fixture_dir, db_url, pause_after_consecutive_failures=2, stop_after_consecutive_failures=5
    )
    scanner = await opened(settings, fresh=True)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    def exploding_scan(_now: object = None) -> ScanResult:
        raise RuntimeError("scan exploded")

    scanner.scan = exploding_scan  # type: ignore[method-assign,assignment]
    try:
        await run_until(runner, lambda r: r.stats.stopped_reason is not None)
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    report.stop_causes.append(str(runner.stats.stopped_reason))
    record(
        evidence_dir,
        "02-failing-scan",
        report,
        [
            "every scan raised; the run stopped rather than reporting a quiet market",
            f"stopped_reason: {runner.stats.stopped_reason}",
        ],
    )
    assert report.trades == 0
    assert accounts_of(report) == {
        "kalshi:USD": Decimal("10000.0000"),
        "polymarket_global:USDC": Decimal("10000.0000"),
    }, "a run that detected nothing must not have moved any cash"
    assert not report.outstanding_holds


async def test_storage_that_will_not_accept_a_trade_invalidates_it_and_stops(
    fixture_dir: Path, db_url: str, evidence_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced blocker, driven through the assembled runtime.

    Cash used to be debited and the trade published before the transaction that stored
    them, so a failed write left the process trading against balances no restart could
    reproduce. Now the attempt is refused, named, and the run ends.
    """
    settings = make_settings(fixture_dir, db_url, stop_after_consecutive_failures=2)
    scanner = await opened(settings, fresh=True)
    runner = TrialRunner(scanner, settings, use_websockets=False)

    async def failing(self: PaperTradeRepository, trade: object) -> None:
        raise RuntimeError("disk is read-only")

    monkeypatch.setattr(PaperTradeRepository, "record", failing)
    try:
        await run_until(runner, lambda r: r.stats.paper_trades_invalidated > 0)
    finally:
        monkeypatch.undo()
        await scanner.close()

    assert runner.stats.paper_trades_invalidated > 0
    assert scanner.state.invalidated_attempts
    assert scanner.state.invalidated_attempts[0].stage is DurabilityStage.CHECKPOINT

    report = await reconciled_from_storage(settings)
    report.stop_causes.append(str(runner.stats.stopped_reason or "watchdog escalation pending"))
    record(
        evidence_dir,
        "03-storage-refuses-a-trade",
        report,
        [
            "the trade row write failed; the attempt was never published to the portfolio",
            f"invalidated attempts (in-process): {runner.stats.paper_trades_invalidated}",
            "storage and memory agree: both hold no trade and the opening balances",
            "the in-process run reported its own evidence as incomplete",
        ],
    )
    assert report.trades == 0, "an unstored attempt is not a position"
    assert report.reconciles
    assert accounts_of(report) == {
        "kalshi:USD": Decimal("10000.0000"),
        "polymarket_global:USDC": Decimal("10000.0000"),
    }


async def test_capital_depletion_refuses_trades_instead_of_inventing_money(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Starved accounts must refuse, with a stated reason, and never go negative."""
    settings = make_settings(fixture_dir, db_url)
    settings = settings.model_copy(
        update={
            "paper": settings.paper.model_copy(
                update={
                    "starting_usd_balance": Decimal("40"),
                    "starting_usdc_balance": Decimal("40"),
                }
            )
        }
    )
    scanner = await opened(settings, fresh=True)
    runner = TrialRunner(scanner, settings, use_websockets=False)
    try:
        await run_until(runner, lambda r: r.stats.paper_trades_refused >= 3)
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "04-capital-depletion",
        report,
        [
            "accounts opened at 40.00; the capital gate refused every attempt",
            f"refusals (in-process): {runner.stats.paper_trades_refused}",
            "a rising refusal count is not a quiet market, and the report says which",
        ],
    )
    for account in report.accounts:
        assert account.ending >= Decimal("0"), "a paper account may not go negative"
        assert account.reserved == Decimal("0.0000"), "a refused attempt holds nothing"


async def test_an_interruption_between_legs_survives_a_restart(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Leg 1 filled, leg 2 never happened, the process restarted. The position is still there."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        opportunity = scanner.scan().all_detected[0]
        with (
            failing_polymarket_book(scanner, RuntimeError("leg 2 feed vanished")),
            pytest.raises(ExecutionInterruptedError),
        ):
            await scanner.paper_trade_if_funded(opportunity)
        in_memory = {
            a.key: scanner.portfolio.balance(a)
            for a in (AccountId.parse("kalshi:USD"), AccountId.parse("polymarket_global:USDC"))
        }
        exposure = scanner.portfolio.total_unhedged_quantity
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "05-interruption-and-restart",
        report,
        [
            "the leg-2 book read raised after leg 1 had filled",
            f"in-memory balances before the restart: { {k: str(v) for k, v in in_memory.items()} }",
            "a fresh process reads the same balances and the same open exposure",
            "the attempt is carried as INTERRUPTED and excluded from hedge statistics",
        ],
    )
    assert accounts_of(report) == in_memory, "memory and storage agree across the restart"
    assert report.total_unhedged_quantity == exposure > Decimal("0")
    assert len(report.open_positions) == 1
    assert report.open_positions[0]["execution_status"] == PaperTradeStatus.INTERRUPTED.value


async def test_an_abrupt_stop_between_legs_is_recovered_by_the_checkpoint(
    fixture_dir: Path, db_url: str, evidence_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process dies before any handler runs. Only the pre-leg-2 checkpoint can save it.

    This is the crash-recovery scenario, and it is honest about its limit: it stands in for
    an abrupt death by disabling the interruption handler entirely, which is what a killed
    process amounts to. A death *before* the checkpoint leaves nothing — correctly, since a
    paper fill that reached no storage also reached no venue, so memory and disk still agree.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)

    async def never_runs(self: Scanner, interrupted: ExecutionInterruptedError) -> None:
        return None

    try:
        opportunity = scanner.scan().all_detected[0]
        monkeypatch.setattr(Scanner, "_publish_interrupted", never_runs)
        with (
            failing_polymarket_book(scanner, RuntimeError("host went away")),
            pytest.raises(ExecutionInterruptedError),
        ):
            await scanner.paper_trade_if_funded(opportunity)
    finally:
        monkeypatch.undo()
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "06-abrupt-stop",
        report,
        [
            "the interruption handler was disabled, standing in for a killed process",
            "the leg-1 checkpoint written before leg 2 is what the restart reads",
            "limitation: a death before that checkpoint leaves no record — correctly, "
            "because an unstored simulated fill also reached no venue",
        ],
    )
    assert report.total_unhedged_quantity > Decimal("0")
    assert len(report.open_positions) == 1
    assert report.open_positions[0]["execution_status"] == PaperTradeStatus.LEG1_FILLED.value


async def test_a_settlement_whose_write_failed_is_repaired_across_a_restart(
    fixture_dir: Path, db_url: str, evidence_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trade, fail the settlement write, restart, settle again. Credited exactly once."""
    settings = make_settings(fixture_dir, db_url)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    scanner = await opened(settings, fresh=True)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        trade_id = trade.id

        async def failing(self: PaperTradeRepository, t: object) -> None:
            raise RuntimeError("storage went away mid-settlement")

        monkeypatch.setattr(PaperTradeRepository, "record", failing)
        with pytest.raises(RuntimeError, match="storage went away"):
            await scanner.settle_paper_trade(trade_id, outcomes)
        monkeypatch.undo()
        assert scanner.state.invalidated_attempts[0].stage is DurabilityStage.SETTLEMENT
    finally:
        await scanner.close()

    # A fresh process: storage is authoritative and says the trade is still open.
    restarted = await opened(settings, fresh=False)
    try:
        recovered = next(t for t in restarted.portfolio.trades if t.id == trade_id)
        assert not recovered.is_settled, "an uncommitted credit is simply not there"
        await restarted.settle_paper_trade(trade_id, outcomes)
        credited = {
            a.key: restarted.portfolio.balance(a)
            for a in (AccountId.parse("kalshi:USD"), AccountId.parse("polymarket_global:USDC"))
        }
    finally:
        await restarted.close()

    # And once more, to prove the repeat is a no-op rather than a second credit.
    again = await opened(settings, fresh=False)
    try:
        record_again = await again.settle_paper_trade(trade_id, outcomes)
        assert record_again.already_settled
    finally:
        await again.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "07-settlement-failure-and-restart",
        report,
        [
            "the settlement write failed and the run recorded the divergence",
            "the restart found the trade unsettled, which is storage being authoritative",
            "settling again credited the payout; settling a third time credited nothing",
            f"credited balances: { {k: str(v) for k, v in credited.items()} }",
        ],
    )
    assert accounts_of(report) == credited
    assert report.settled_trades == 1
    assert report.evidence_is_complete


async def test_a_healthy_accelerated_run_reconciles_end_to_end(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """The control. Nothing injected, so any imbalance here is the machinery's own.

    It also fixes what the exercise does *not* show: every trade below is graded
    ``unchanged_reread`` because a fixture book cannot move between the legs.
    """
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    runner = TrialRunner(scanner, settings, use_websockets=False)
    try:
        await run_until(runner, lambda r: r.stats.paper_trades >= 2)
        stats = runner.stats.as_dict()
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "08-healthy-run",
        report,
        [
            "no failure injected; the assembled runtime traded against fixture books",
            f"trial stats: {stats}",
            "leg-2 evidence is unchanged_reread throughout: a fixture book cannot move, "
            "so nothing here is evidence about hedging under adverse movement",
        ],
    )
    assert report.trades >= 2
    assert report.reconciles
    assert report.evidence_is_complete
    assert set(report.evidence) == {"unchanged_reread"}, (
        "a fixture run must not claim measured evidence"
    )
