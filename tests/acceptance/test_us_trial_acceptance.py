"""The deployment target, proved offline: Kalshi production + Polymarket **US**, USD both legs.

The existing acceptance exercise runs on the Polymarket **Global** corpus and settles one
leg in USDC. It is good evidence about the runtime and no evidence at all about the thing
we are actually preparing to run, which is Kalshi against Polymarket US with US dollars on
both sides. Those differ in the places accounting defects hide: two accounts that are both
``USD`` but are *not* the same money, a different fee schedule, a different tick size, and
a different identifier space behind the same ``Venue.POLYMARKET``.

So this file exercises the US target through the same assembled runtime — fresh schema,
recorded approval, startup preflight, refresh, funded execution, interruption, restart,
settlement and reconciliation — and keeps the Global file as legacy coverage rather than
as the proof for a trial that will not use it.

**The pair is synthetic.** These are fixture contracts written to settle identically for
this exercise. Nothing here asserts that two *real* contracts are equivalent; that remains
an operator's reading of two rulebooks and is the one input no test can supply.

What this does not establish: fixture books cannot move, so every leg 2 re-reads an
unchanged book and is graded as such. A scenario ending fully hedged shows the accounting
held, not that hedging would.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
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
from event_arb.domain.enums import (
    MatchStatus,
    Outcome,
    PaperTradeStatus,
    SettlementAsset,
    Venue,
    VenueProduct,
)
from event_arb.domain.keys import MarketKey
from event_arb.domain.models import OrderBook
from event_arb.paper.accounts import AccountId
from event_arb.paper.executor import ExecutionInterruptedError, SettlementOutcomes
from event_arb.paper.reconciliation import ReconciliationReport, reconcile_accounts
from event_arb.persistence.database import Database
from event_arb.persistence.schema import ensure_schema, inspect_schema
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner
from event_arb.trial_preflight import run_preflight, run_static_preflight
from tests.acceptance.test_offline_trial_acceptance import accounts_of, record, run_until

pytestmark = pytest.mark.anyio

KALSHI_FED = "KXFEDDECISION-26SEP-CUT25"
US_FED = "fed-cuts-25bps-september-2026"

RECORDED = {"evidence": "fixture settlement notice", "recorded_by": "us-acceptance-exercise"}

#: Both accounts the US target draws on. Both are USD and they are not the same money —
#: which is exactly the confusion a per-account reconciliation exists to catch.
KALSHI_USD = "kalshi:USD"
POLY_US_USD = "polymarket_us:USD"


def make_settings(fixture_dir: Path, url: str, **trial: object) -> Settings:
    """The US target, with trial intervals compressed to milliseconds."""
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
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
            watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=US_FED)],
            refresh_interval_seconds=0.01,
            scan_interval_seconds=0.01,
            health_check_interval_seconds=0.01,
            **trial,  # type: ignore[arg-type]
        ),
    )


async def opened(settings: Settings, *, fresh: bool) -> Scanner:
    """A process's view of the US trial database, recorded approval and all."""
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


async def reconciled_from_storage(settings: Settings) -> ReconciliationReport:
    """What a fresh process reads back — inspected, never recovered."""
    scanner = Scanner(settings, database=Database(settings.database_url))
    try:
        restored = await scanner.inspect_portfolio()
        report = reconcile_accounts(scanner.portfolio)
        if not restored.is_complete:
            report.stop_causes.append(
                f"restore incomplete: {len(restored.trades_unreadable)} unreadable trade row(s)"
            )
        return report
    finally:
        await scanner.close()


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


# ------------------------------------------------------------------ schema and scope


async def test_a_fresh_database_migrates_to_the_current_revision(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """A trial starts on its own database, not on one left over from something else."""
    database = Database(db_url)
    try:
        created = await ensure_schema(database)
        assert not created.problem, created.problem
        state = await inspect_schema(database)
        assert state.revision, "a fresh trial database must report a revision"
        assert not state.problem
    finally:
        await database.dispose()

    (evidence_dir / "us-00-schema.txt").write_text(
        f"fresh US trial database migrated cleanly\n  revision: {state.revision}\n"
    )


async def test_the_us_product_resolves_to_us_markets_and_usd_accounts(
    fixture_dir: Path, db_url: str
) -> None:
    """Both legs are USD, and the two USD accounts are still distinct money."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        pair = next(p for p in scanner.state.pairs.values() if p.polymarket_market_id == US_FED)
        assert pair.polymarket_product is VenueProduct.POLYMARKET_US
        opportunity = scanner.scan().all_detected[0]
        assert opportunity.polymarket_leg.settlement_asset is SettlementAsset.USD
        assert opportunity.kalshi_leg.settlement_asset is SettlementAsset.USD
        assert AccountId.parse(KALSHI_USD) != AccountId.parse(POLY_US_USD)
    finally:
        await scanner.close()


async def test_the_recorded_us_approval_survives_a_restart(fixture_dir: Path, db_url: str) -> None:
    """Approval is the human gate; a restart must neither lose it nor invent it."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        approved = [p for p in scanner.state.pairs.values() if p.status is MatchStatus.APPROVED]
        assert len(approved) == 1
        pair_id = approved[0].id
    finally:
        await scanner.close()

    restarted = await opened(settings, fresh=False)
    try:
        restored = restarted.state.pairs[pair_id]
        assert restored.status is MatchStatus.APPROVED
        assert restored.polymarket_market_id == US_FED
    finally:
        await restarted.close()


async def test_preflight_passes_for_the_us_target(fixture_dir: Path, db_url: str) -> None:
    """The gate both entry points run, against the configuration the trial will use."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        static = await run_static_preflight(settings, scanner.database, observation_only=False)
        assert static.passed, static.render()
        full = await run_preflight(scanner, settings, observation_only=False, static=static)
        assert full.passed, full.render()
        codes = {f.code for f in full.findings}
        assert "feed_coverage" in codes, "the US feed must be checked for coverage too"
    finally:
        await scanner.close()


# ------------------------------------------------------------------------- execution


async def test_a_healthy_us_run_reconciles_end_to_end(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """The deployment target, running unattended, reconciled from a fresh process."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    runner = TrialRunner(scanner, settings, use_websockets=False)
    try:
        await run_until(runner, lambda r: r.stats.paper_trades >= 1)
        stats = runner.stats.as_dict()
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "us-01-healthy-run",
        report,
        [
            "Kalshi production shape + Polymarket US, USD on both legs",
            f"refreshes: {stats['refreshes']} (incomplete: {stats['refreshes_incomplete']})",
            f"paper trades: {stats['paper_trades']}",
            "every account re-derived from the ledger on disk by a fresh process",
        ],
    )
    assert report.trades >= 1
    assert report.reconciles
    assert report.evidence_is_complete
    assert report.integrity.is_sound, report.integrity.render()
    assert set(accounts_of(report)) == {KALSHI_USD, POLY_US_USD}
    assert stats["feed_was_healthy"] is True


async def test_a_funded_us_trade_debits_the_two_usd_accounts_separately(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """The defect a single USD total would hide: each leg draws on its own account."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        assert trade.execution_status is PaperTradeStatus.BOTH_LEGS_FILLED
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "us-02-funded-execution",
        report,
        [
            "one funded US trade; both legs settled in USD on separate accounts",
            f"ending balances: { {a.account: str(a.ending) for a in report.accounts} }",
        ],
    )
    balances = accounts_of(report)
    assert balances[KALSHI_USD] < Decimal("10000")
    assert balances[POLY_US_USD] < Decimal("10000")
    assert report.reconciles
    assert report.integrity.is_sound


async def test_a_us_interruption_between_legs_survives_a_restart(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Leg 1 filled, leg 2 never tried. The exposure is real and must outlive the process."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        opportunity = scanner.scan().all_detected[0]
        with (
            failing_polymarket_book(scanner, RuntimeError("US feed died between legs")),
            pytest.raises(ExecutionInterruptedError),
        ):
            await scanner.paper_trade_if_funded(opportunity)
        interrupted = scanner.state.paper_trades[-1]
        assert interrupted.execution_status is PaperTradeStatus.INTERRUPTED
        exposure = interrupted.unhedged_quantity
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "us-03-interruption-restart",
        report,
        [
            "leg 1 filled on Kalshi; the Polymarket US book read raised before leg 2",
            f"unhedged quantity carried across the restart: {exposure}",
            "a fresh process rebuilt the exposure rather than losing it",
        ],
    )
    assert report.total_unhedged_quantity == exposure
    assert exposure > 0
    assert report.reconciles
    assert report.integrity.is_sound, report.integrity.render()


# ------------------------------------------------------------------------ settlement


async def test_a_us_trade_settles_with_aligned_outcomes(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Both venues call the same side the winner."""
    settings = make_settings(fixture_dir, db_url)
    outcomes = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    assert outcomes.kalshi is Outcome.YES and outcomes.polymarket is Outcome.YES

    scanner = await opened(settings, fresh=True)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        settled = await scanner.settle_paper_trade(trade.id, outcomes)
        credited = settled.credited
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "us-04-settlement-aligned",
        report,
        [
            "aligned outcomes: Kalshi YES and Polymarket US YES name the same result",
            f"credited: {credited}",
        ],
    )
    assert report.settled_trades == 1
    assert report.reconciles
    assert report.evidence_is_complete
    assert report.integrity.is_sound, report.integrity.render()


async def test_a_us_trade_settles_with_inverted_outcomes(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """The reviewer recorded that Polymarket YES means the opposite of Kalshi YES.

    An inverted pair is not a different code path in execution — it is a statement a human
    made at approval time, and settlement has to honour it. Getting this backwards pays out
    on the losing side of one leg, which reconciles perfectly while being entirely wrong,
    so the payout split is what this checks rather than merely that the books close.
    """
    settings = make_settings(fixture_dir, db_url)
    aligned = SettlementOutcomes.for_event(Outcome.YES, **RECORDED)  # type: ignore[arg-type]
    inverted = SettlementOutcomes.for_event(
        Outcome.YES,
        outcomes_inverted=True,
        **RECORDED,  # type: ignore[arg-type]
    )
    assert inverted.kalshi is Outcome.YES
    assert inverted.polymarket is Outcome.NO, "inversion has to reach the Polymarket leg"
    assert inverted.polymarket is not aligned.polymarket

    scanner = await opened(settings, fresh=True)
    try:
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        settled = await scanner.settle_paper_trade(trade.id, inverted)
        result = settled.trade.settlement_result or {}
        payouts = result.get("payouts_by_account") or {}
        assert isinstance(payouts, dict)
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "us-05-settlement-inverted",
        report,
        [
            "inverted outcomes: the reviewer recorded that Polymarket US YES is Kalshi NO",
            f"payouts by account: {payouts}",
            "settlement honoured the recorded inversion rather than assuming alignment",
        ],
    )
    assert set(payouts) <= {KALSHI_USD, POLY_US_USD}
    assert report.settled_trades == 1
    assert report.reconciles
    assert report.integrity.is_sound, report.integrity.render()


# --------------------------------------------------------------------------- failure


async def test_a_dead_us_feed_stops_the_run_and_the_books_still_close(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """Injected at the adapter, so the error-swallowing boundary is the thing under test."""
    settings = make_settings(
        fixture_dir, db_url, pause_after_consecutive_failures=2, stop_after_consecutive_failures=4
    )
    scanner = await opened(settings, fresh=True)

    class DeadFeed:
        async def __call__(self, market_id: str) -> OrderBook:
            raise RuntimeError("US venue feed is unreachable")

    for adapter in scanner.adapters.values():
        adapter.fetch_orderbook = DeadFeed()  # type: ignore[method-assign]

    runner = TrialRunner(scanner, settings, use_websockets=False)
    try:
        await run_until(runner, lambda r: r.stats.stopped_reason is not None)
        stats = runner.stats.as_dict()
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    report.stop_causes.append(str(stats["stopped_reason"]))
    record(
        evidence_dir,
        "us-06-dead-feed",
        report,
        [
            "every fetch_orderbook raised; the batch still returned, as it always did",
            f"incomplete refreshes: {stats['refreshes_incomplete']}",
            f"stopped_reason: {stats['stopped_reason']}",
            "an outage was reported as an outage, not as a quiet market",
        ],
    )
    assert stats["feed_was_healthy"] is False
    assert report.trades == 0
    assert report.reconciles


async def test_an_abruptly_ended_us_run_is_not_reported_as_complete(
    fixture_dir: Path, db_url: str, evidence_dir: Path
) -> None:
    """A run that opened and never closed leaves the next process unable to call it clean."""
    settings = make_settings(fixture_dir, db_url)
    scanner = await opened(settings, fresh=True)
    try:
        run_id = scanner.begin_run(mode="paper_trading")
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        # No finish_run(): this is the process dying.
    finally:
        await scanner.close()

    report = await reconciled_from_storage(settings)
    record(
        evidence_dir,
        "us-07-unfinished-run",
        report,
        [
            "the run opened a journal marker and never closed it",
            f"unfinished run: {run_id}",
            "the trade it did make is intact; the run's completeness is not asserted",
        ],
    )
    assert report.unfinished_runs == [run_id]
    assert not report.evidence_is_complete
    assert report.reconciles, "the accounts still close; it is the evidence that is partial"
    assert report.integrity.is_sound
