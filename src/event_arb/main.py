"""Command-line entrypoint.

Commands:

``scan``     discover markets, generate pairs, detect opportunities, print the result
``paper``    the above, plus a simulated two-leg execution for each opportunity
``serve``    run the dashboard
``pairs``    list candidate pairs with their rule differences
``settle``   record a paper trade's real outcome and credit its payouts
``doctor``   print configuration and confirm the safety posture

All commands default to fixture data and require no credentials or network access.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from event_arb.config import (
    DataSource,
    IngestionMode,
    PolymarketProduct,
    Settings,
    get_settings,
)
from event_arb.demo_reviews import DemoReviewError
from event_arb.domain.enums import MatchStatus, Venue
from event_arb.domain.keys import kalshi_key, market_key, pair_market_keys, polymarket_key
from event_arb.domain.models import VenueMarket
from event_arb.matching.approval import ApprovalError
from event_arb.observability.logging import configure_logging, get_logger
from event_arb.paper.journal import RunJournal
from event_arb.paper.reconciliation import reconcile_accounts
from event_arb.scanner import Scanner

__all__ = ["main"]

log = get_logger(__name__)


def _write_export(path: str, payload: str) -> None:
    Path(path).write_text(payload)


def _banner(settings: Settings) -> None:
    print("=" * 78)
    print("event-arb — cross-venue prediction-market scanner")
    print(f"  data source     : {settings.data_source.value}")
    print(f"  trading enabled : {settings.trading_enabled}")
    print("  live trading    : IMPOSSIBLE (Phase 7 not implemented)")
    print("=" * 78)


async def _prepare(settings: Settings, replay_recorded_reviews: bool) -> Scanner:
    # Checked before anything is constructed or fetched. Validating inside the replay step
    # meant a live run discovered markets first and only then refused — doing the network
    # work for a command that was never going to be allowed to finish.
    if replay_recorded_reviews and settings.data_source is not DataSource.FIXTURE:
        raise DemoReviewError(
            "--replay-recorded-reviews is fixture-only and this run is configured for "
            f"{settings.data_source.value} data. Nothing was requested."
        )

    scanner = Scanner(settings)
    await scanner.discover()
    # Pairs first: the book refresh is scoped to the markets those pairs reference.
    scanner.seed_watched_pairs()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books()

    if replay_recorded_reviews:
        _apply_recorded_reviews(scanner)
    return scanner


def _apply_recorded_reviews(scanner: Scanner) -> None:
    """Replay reviews a human already recorded for the fixture corpus.

    This is not automated approval. It does not consult the rule comparison to decide
    anything; it matches exact fixture market ids against reviews a person wrote down, and
    hands the recorded sources to the ordinary approval path — which still applies every
    readiness check, and still refuses if the pair is not ready. See
    :mod:`event_arb.demo_reviews` for why the old --auto-approve flag was removed.
    """
    from event_arb.demo_reviews import load_recorded_reviews

    reviews = load_recorded_reviews(scanner.settings)
    if not reviews:
        print("\n  No recorded reviews for this fixture corpus; nothing is approved.")
        return

    applied = 0
    for pair_id, pair in list(scanner.state.pairs.items()):
        if pair.status is not MatchStatus.SUGGESTED:
            continue
        match = next(
            (
                review
                for review in reviews
                if review.matches(
                    pair.kalshi_market_id,
                    pair.polymarket_market_id,
                    pair.polymarket_product.value,
                )
            ),
            None,
        )
        if match is None:
            continue
        try:
            scanner.approve(
                pair_id,
                approved_by=match.reviewed_by,
                reviewer_notes=match.notes,
                kalshi_reviewed_source=match.kalshi_source,
                polymarket_reviewed_source=match.polymarket_source,
            )
        except ApprovalError as exc:
            # A recorded review does not override readiness. If the pair is not ready, it
            # stays a research candidate and the reason is printed rather than swallowed.
            print(f"\n  Recorded review for {pair_id[:12]} not applied: {exc}")
            continue
        applied += 1

    print(f"\n  Replayed {applied} recorded fixture review(s). Live data is never eligible.")


def _print_scan(scanner: Scanner) -> None:
    result = scanner.scan()

    print(f"\nMarkets: {len(scanner.state.markets)}   Pairs: {len(scanner.state.pairs)}")
    approved = [p for p in scanner.state.pairs.values() if p.is_tradable_pair]
    print(f"Approved pairs: {len(approved)}")

    print(f"\n--- Opportunities ({len(result.opportunities)}) ---")
    if not result.opportunities:
        print("  none")
    for opportunity in result.opportunities:
        tag = "CONFIRMED" if opportunity.is_confirmed else "RESEARCH CANDIDATE"
        print(f"\n  [{tag}] pair={opportunity.pair_id[:12]} {opportunity.direction.value}")
        print(
            f"    Kalshi     : buy {opportunity.kalshi_leg.outcome.value} "
            f"{opportunity.kalshi_leg.quantity} @ VWAP ${opportunity.kalshi_leg.vwap} "
            f"(worst ${opportunity.kalshi_leg.worst_price}, "
            f"{opportunity.kalshi_leg.levels_consumed} level(s), "
            f"age {opportunity.kalshi_leg.book_age_ms}ms)"
        )
        print(
            f"    Polymarket : buy {opportunity.polymarket_leg.outcome.value} "
            f"{opportunity.polymarket_leg.quantity} @ VWAP "
            f"${opportunity.polymarket_leg.vwap} "
            f"(worst ${opportunity.polymarket_leg.worst_price}, "
            f"{opportunity.polymarket_leg.levels_consumed} level(s), "
            f"age {opportunity.polymarket_leg.book_age_ms}ms)"
        )
        print(f"    Gross spread        : ${opportunity.gross_spread}")
        print(f"    Acquisition cost    : ${opportunity.gross_acquisition_cost}")
        print(f"    Kalshi fees         : ${opportunity.kalshi_fees}")
        print(f"    Polymarket fees     : ${opportunity.polymarket_fees}")
        print(f"    Slippage buffer     : ${opportunity.slippage_allowance}")
        print(f"    Operational buffer  : ${opportunity.operational_cost_allowance}")
        print(f"    USDC conversion     : ${opportunity.usdc_conversion_allowance}")
        print(f"    Expected payout     : ${opportunity.expected_payout}")
        print(
            f"    NET PROFIT          : ${opportunity.net_profit} "
            f"({opportunity.return_on_capital}% on capital)"
        )
        print(f"    Detected at         : {opportunity.detected_at.isoformat()}")
        print(
            f"    Risk flags          : "
            f"{', '.join(f.value for f in opportunity.risk_flags) or 'none'}"
        )

    print(f"\n--- Rejections ({len(result.rejections)}) ---")
    for rejection in result.rejections:
        print(
            f"  {rejection.pair_id[:12]} {rejection.direction.value}: "
            f"{rejection.reason.value} — {rejection.detail}"
        )

    if result.suspended_pairs:
        print(f"\n--- Auto-suspended pairs ({len(result.suspended_pairs)}) ---")
        for pair in result.suspended_pairs:
            print(f"  {pair.id[:12]}: {pair.reviewer_notes.splitlines()[-1]}")


async def _cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    _banner(settings)
    scanner = await _prepare(settings, args.replay_recorded_reviews)
    _print_scan(scanner)
    await scanner.close()
    return 0


async def _cmd_paper(settings: Settings, args: argparse.Namespace) -> int:
    _banner(settings)
    scanner = await _prepare(settings, args.replay_recorded_reviews)
    _print_scan(scanner)

    result = scanner.state.last_scan
    targets = (
        result.opportunities
        if result and args.include_candidates
        else (result.confirmed if result else [])
    )
    print(f"\n--- Paper trading ({len(targets)} opportunity/ies) ---")
    for opportunity in targets:
        trade = await scanner.paper_trade(opportunity)
        print(f"\n  trade {trade.id[:8]} status={trade.status.value}")
        print(f"    hedged   : {trade.hedged_quantity}")
        print(
            f"    unhedged : {trade.unhedged_quantity} "
            f"({trade.unhedged_venue.value if trade.unhedged_venue else 'none'})"
        )
        print(f"    cost     : ${trade.total_cost}   fees: ${trade.total_fees}")
        print(
            f"    P&L      : ${trade.realized_pnl_if_hedged} "
            f"(detected ${trade.opportunity.net_profit})"
        )
        for note in trade.notes:
            print(f"    note     : {note}")

    report = scanner.portfolio.daily_report()
    print("\n--- Daily report ---")
    for key, value in report.as_dict().items():
        print(f"  {key}: {value}")

    # The hedge rate is the number a reader will act on, so the limits of the evidence
    # behind it are printed next to it rather than left in a JSON field.
    if report.trades and not report.hedge_rate_is_measured:
        print(f"\n  !! hedge_success_rate_pct is {report.evidence_caveat}")

    if args.export:
        # A one-shot CLI write; offloaded to a thread so the event loop is never blocked.
        await asyncio.to_thread(_write_export, args.export, scanner.portfolio.to_json())
        print(f"\nExported paper results to {args.export}")

    await scanner.close()
    return 0


async def _cmd_pairs(settings: Settings, args: argparse.Namespace) -> int:
    _banner(settings)
    scanner = await _prepare(settings, replay_recorded_reviews=False)
    from event_arb.matching.rules_diff import compare_rules

    for pair in sorted(scanner.state.pairs.values(), key=lambda p: p.semantic_score, reverse=True):
        kalshi_k, polymarket_k = pair_market_keys(pair)
        kalshi = scanner.state.markets[kalshi_k]
        polymarket = scanner.state.markets[polymarket_k]
        comparison = compare_rules(kalshi, polymarket)
        print(f"\npair {pair.id[:12]}  score={pair.semantic_score}  status={pair.status.value}")
        print(f"  Kalshi     : {kalshi.title}")
        print(f"  Polymarket : {polymarket.title}")
        if not comparison.differences:
            print("  no rule differences detected")
        for difference in comparison.differences:
            marker = "MATERIAL" if difference.is_material else "note    "
            print(
                f"  [{marker}] {difference.dimension}: "
                f"{difference.kalshi_value} vs {difference.polymarket_value}"
            )
            print(f"             {difference.explanation}")
    await scanner.close()
    return 0


async def _cmd_trial(settings: Settings, args: argparse.Namespace) -> int:
    """Run the autonomous paper trial until interrupted."""
    _banner(settings)

    from event_arb.alerts import AlertDispatcher
    from event_arb.persistence.database import Database
    from event_arb.persistence.schema import ensure_schema
    from event_arb.runtime import TrialRunner
    from event_arb.trial_preflight import run_preflight, run_static_preflight

    live = settings.data_source is DataSource.LIVE
    observation_only = args.observation_only or settings.trial.observation_only
    if observation_only:
        # Forced off in the settings the runner reads, not merely checked at the call
        # site, so every path that consults auto_paper_trade agrees.
        settings = settings.model_copy(
            update={"trial": settings.trial.model_copy(update={"observation_only": True})}
        )

    database = Database(settings.database_url)
    # Creates a schema only when there is none. A populated database at an older revision
    # is reported, not silently left half-migrated by create_all().
    schema = await ensure_schema(database)
    if schema.problem and live:
        print(f"\n  DATABASE NOT READY: {schema.problem}")
        await database.dispose()
        return 1

    # Before the first venue request, not after it. The scope check exists to stop a live
    # run discovering a whole venue, so running it after discovery would refuse the run
    # having already made the request it was refusing.
    static = await run_static_preflight(settings, database, observation_only=observation_only)
    print()
    print(static.render())
    if not static.passed:
        await database.dispose()
        return 1

    scanner = Scanner(settings, database=database)

    await scanner.discover()
    report = await scanner.restore()
    if report.suspended:
        print(f"\n  {len(report.suspended)} approval(s) suspended on restore:")
        for pair, reason in report.suspended:
            print(f"    {pair.id[:12]}: {reason}")
    scanner.seed_watched_pairs()
    if not settings.trial.watched_pairs:
        # Fixture demos may pair the whole corpus. A live run may not, and the preflight
        # blocks before reaching here, so this can no longer widen a live trial's scope.
        if live:
            print("\n  refusing to generate pairs from discovery in a live trial")
            await scanner.close()
            return 1
        scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.persist_markets()
    await scanner.persist_pairs()

    preflight = await run_preflight(
        scanner, settings, observation_only=observation_only, static=static
    )
    print()
    print(preflight.render())
    if not preflight.passed:
        await scanner.close()
        return 1

    restored = scanner.last_portfolio_restore
    if restored is not None:
        print(f"\n  restored portfolio: {restored.as_dict()}")

    # From here this process can attempt trades, so from here an abrupt death is
    # unexplained. Only the clean shutdown in the `finally` below closes this marker;
    # a kill, a crash or a host going away leaves it open for the next start to find.
    run_id = scanner.begin_run(
        mode="observation_only" if observation_only else "paper_trading",
        data_source=settings.data_source.value,
        duration_seconds=args.duration,
    )
    if run_id is not None:
        print(f"  run id: {run_id} (journal: {scanner.journal.path})")  # type: ignore[union-attr]

    runner = TrialRunner(
        scanner,
        settings,
        alerts=AlertDispatcher(settings.alerts),
        # WebSocket ingestion needs an authenticated handshake on both venues, so it is
        # opt-in via ingestion mode and never implied by merely running live.
        use_websockets=(
            settings.data_source is DataSource.LIVE
            and settings.ingestion_mode is IngestionMode.WEBSOCKET
            and not args.rest_only
        ),
    )
    await runner.start()
    print(f"\n  trial running; scanning every {settings.trial.scan_interval_seconds}s")
    print("  press Ctrl-C to stop\n")

    interrupted = False
    try:
        # Wait for the *runner*, not merely for the clock. This used to sleep for the
        # duration or block on an Event nothing ever set, so a watchdog stop or a dead
        # background task left the process running to the end of its schedule and then
        # exited zero — the one outcome that makes an unattended failure look like a
        # completed run.
        # `args.duration` of None means no deadline, which asyncio.timeout accepts
        # directly — so a bounded run and an open-ended one take the same path.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(args.duration):
                await runner.wait_until_stopped()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # An operator stopping the run is not a failure.
        interrupted = True
    finally:
        await runner.stop()
        print("\n--- Trial summary ---")
        for key, value in runner.stats.as_dict().items():
            print(f"  {key}: {value}")
        report_daily = scanner.portfolio.daily_report()
        print("\n--- Daily paper report ---")
        for key, value in report_daily.as_dict().items():
            print(f"  {key}: {value}")
        # The arithmetic under the report. Printed unconditionally, because a rate is only
        # worth reading once the accounts it came from are known to close.
        causes = [c for c in (runner.stats.stopped_reason, runner.stats.last_error) if c]
        reconciliation = reconcile_accounts(scanner.portfolio, causes)
        print()
        print(reconciliation.to_text())
        if args.reconciliation_json:
            destination = Path(args.reconciliation_json)
            await asyncio.to_thread(
                destination.write_text, json.dumps(reconciliation.as_dict(), indent=2) + "\n"
            )
            print(f"\n  reconciliation written: {destination}")
        reconciled = reconciliation.reconciles and reconciliation.evidence_is_complete
        # Closing the run marker is the last thing an orderly shutdown does. Reaching it
        # is what distinguishes this run from one that was killed mid-attempt.
        scanner.finish_run(
            stopped_reason=runner.stats.stopped_reason,
            interrupted=interrupted,
            paper_trades=runner.stats.paper_trades,
            reconciled=reconciled,
        )
        await scanner.close()

    if not reconciled:
        # A run whose books do not close, or that lost an attempt to a failed write, has
        # not produced evidence — whether or not its loops stayed healthy. The two causes
        # are named separately because they call for different things: one is an
        # arithmetic fault to investigate, the other a gap in the record to disclose.
        print("\n  TRIAL EVIDENCE IS NOT USABLE")
        if not reconciliation.reconciles:
            print("    an account does not balance against its own ledger entries")
        if not reconciliation.evidence_is_complete:
            print(
                f"    {len(reconciliation.invalidated)} attempt(s) could not be stored and "
                "are counted in no balance, no total and no rate above"
            )
        print("    see docs/operations.md, 'Accounting durability'")
        return 1
    if interrupted or not runner.failed:
        return 0
    print("\n  TRIAL ENDED EARLY")
    if runner.task_failed:
        print(f"    a background task died: {runner.stats.last_error}")
    if runner.stats.stopped_reason:
        print(f"    reason: {runner.stats.stopped_reason}")
    print("    the evidence above is incomplete; do not read it as a finished run")
    return 1


async def _cmd_candidates(settings: Settings, args: argparse.Namespace) -> int:
    """Bounded candidate research. Never approves, never trades, never reads a book."""
    _banner(settings)

    from event_arb.candidates import (
        CredentialsWouldBeSentError,
        DiscoveryLimits,
        build_request_plan,
        research_candidates,
        top_slugs_for_recheck,
    )
    from event_arb.persistence.database import Database

    limits = DiscoveryLimits(
        kalshi_limit=args.kalshi_limit,
        polymarket_limit=args.polymarket_limit,
        min_score=Decimal(str(args.min_score)),
        max_per_market=args.max_per_market,
        max_results=args.max_results,
        recheck_top=args.recheck_top,
    )

    live = settings.data_source is DataSource.LIVE
    if live:
        settings.validate_live_market_data()

    # The database is opened first: dispositions must be restored *before* generation, or
    # the run would propose pairs a human has already decided about.
    database = Database(settings.database_url)
    await database.create_all()
    scanner = Scanner(settings, database=database)

    # The plan is derived from the adapters that will serve the requests, so it cannot
    # drift from what is actually sent.
    plan = build_request_plan(
        limits,
        scanner.adapters[Venue.KALSHI],
        scanner.adapters[Venue.POLYMARKET],
        scanner.polymarket_product,
    )

    print()
    print(plan.render())
    print()

    if live:
        if plan.sends_any_credentials and not args.authenticated_mode:
            await scanner.close()
            raise CredentialsWouldBeSentError(
                "credentials are configured, so this run would transmit them, but the "
                "plan above is the credential-free one. Re-run with --authenticated-mode "
                "to authorize that explicitly, or unset the venue credentials."
            )
        print("  MODE: LIVE - the requests above will be sent to the venues.")
        if not args.yes_i_authorize_live_requests:
            answer = await asyncio.to_thread(
                input, "  Proceed with live requests? Type 'yes' to continue: "
            )
            if answer.strip().lower() != "yes":
                print("  Aborted. No request was made.")
                await scanner.close()
                return 1
    else:
        print("  MODE: FIXTURE - no network request will be made.")

    # Read-only. restore() would re-verify every approval against markets this run has
    # not fetched, and suspend them all — a research command must not be able to revoke a
    # human decision. See Scanner.load_dispositions.
    existing = await scanner.load_dispositions()

    kalshi_markets, kalshi_cursor = await _discover_limited(
        scanner, Venue.KALSHI, limits.kalshi_limit
    )
    polymarket_markets, polymarket_cursor = await _discover_limited(
        scanner, Venue.POLYMARKET, limits.polymarket_limit
    )

    report = research_candidates(
        kalshi_markets, polymarket_markets, limits, existing_pairs=existing
    )
    report.pagination_cursors_ignored = sum(
        1 for cursor in (kalshi_cursor, polymarket_cursor) if cursor
    )

    # Targeted existence recheck. Deliberately narrow: it asks the venue whether the top
    # ids still resolve, and records nothing else. The returned metadata is not used, so
    # this can never be read as having re-validated the candidate itself.
    if limits.recheck_top > 0 and report.records:
        slugs = top_slugs_for_recheck(report, limits)
        confirmed = await _recheck_market_ids(scanner.adapters[Venue.POLYMARKET], slugs)
        checked_at = datetime.now(UTC)
        for index, record in enumerate(report.records):
            if record.pair.polymarket_market_id in confirmed:
                report.records[index] = replace(
                    record, existence_rechecked=True, existence_rechecked_at=checked_at
                )
        report.existence_rechecked = sum(1 for r in report.records if r.existence_rechecked)

    # Only the capped shortlist is persisted; source catalogues stay in memory.
    await _persist_shortlist(scanner, report, kalshi_markets + polymarket_markets)

    print("\n--- Research summary ---")
    for key, value in report.as_dict().items():
        print(f"  {key}: {value}")

    print(f"\n--- Shortlist ({len(report.records)}) ---")
    for record in sorted(report.records, key=lambda r: r.score, reverse=True):
        state = "READY" if record.readiness.is_ready else "BLOCKED"
        mark = " [id rechecked]" if record.existence_rechecked else ""
        print(f"\n  [{state}]{mark} score={record.score}  pair={record.pair.id[:12]}")
        print(f"    Kalshi     : {record.kalshi_title[:70]}")
        print(f"    Polymarket : {record.polymarket_title[:70]}")
        if record.comparison.material:
            dims = ", ".join(sorted({d.dimension for d in record.comparison.material}))
            print(f"    material   : {dims}")
        for code in record.readiness.blocker_codes:
            print(f"    blocker    : {code}")

    if report.pagination_cursors_ignored:
        print(
            f"\n  {report.pagination_cursors_ignored} pagination cursor(s) returned and "
            "deliberately NOT followed; re-run with a higher limit to see more."
        )

    print("\n  NOTE: every entry above is a research candidate, never arbitrage.")
    print("  Nothing was approved, no order book was read, and no trade was simulated.")
    await scanner.close()
    return 0


async def _recheck_market_ids(adapter: object, market_ids: list[str]) -> set[str]:
    """Existence-only checks, using no-body reads when the adapter supports them."""
    market_exists = getattr(adapter, "market_exists", None)
    if callable(market_exists):
        confirmed: set[str] = set()
        for market_id in market_ids:
            if await market_exists(market_id):
                confirmed.add(market_id)
        return confirmed
    refetched = await adapter.discover_markets(market_ids=market_ids)  # type: ignore[attr-defined]
    return {market.venue_market_id for market in refetched}


async def _persist_shortlist(
    scanner: Scanner, report: object, catalogue: list[VenueMarket]
) -> None:
    """Store the capped shortlist, and only ever as *new* suggested pairs.

    Existence is checked against storage rather than against memory, and pairs that already
    exist are skipped entirely rather than rewritten. A research run therefore cannot
    revoke, approve, downgrade, or otherwise alter a disposition a human recorded — the
    write path has no way to express it.
    """
    records = report.records  # type: ignore[attr-defined]

    # The legs the shortlist actually references, and nothing else. Without these the pair
    # survives a restart but the dashboard has no titles or rules to show, so it reports
    # "market metadata missing" for a candidate it is holding.
    wanted = {
        key
        for record in records
        for key in (
            kalshi_key(record.pair.kalshi_market_id),
            polymarket_key(record.pair.polymarket_product, record.pair.polymarket_market_id),
        )
    }
    legs = [market for market in catalogue if market_key(market) in wanted]
    markets_stored = await scanner.persist_markets_subset(legs)

    stored = await scanner.persist_new_pairs(record.pair for record in records)
    await scanner.audit(
        "candidates-discover",
        "candidates.shortlist_persisted",
        detail={
            "retained": stored,
            "market_legs_persisted": markets_stored,
            "kalshi_markets_seen": report.kalshi_markets_seen,  # type: ignore[attr-defined]
            "polymarket_markets_seen": report.polymarket_markets_seen,  # type: ignore[attr-defined]
            "parse_failures": report.parse_failures,  # type: ignore[attr-defined]
            "rate_limited": report.rate_limited,  # type: ignore[attr-defined]
            "cursors_ignored": report.pagination_cursors_ignored,  # type: ignore[attr-defined]
        },
    )


async def _discover_limited(
    scanner: Scanner, venue: Venue, limit: int
) -> tuple[list[VenueMarket], str | None]:
    """One bounded page from one venue, plus any cursor we are declining to follow.

    Returning the cursor rather than acting on it keeps "we fetched more than you
    authorised" out of the code's hands.
    """
    adapter = scanner.adapters.get(venue)
    if adapter is None:
        return [], None
    markets = await adapter.discover_markets(limit=limit)
    cursor = getattr(adapter, "last_pagination_cursor", None)
    return markets[:limit], cursor if isinstance(cursor, str) and cursor else None


async def _cmd_doctor(settings: Settings, _args: argparse.Namespace) -> int:
    _banner(settings)
    print("\nConfiguration:")
    print(f"  database_url        : {settings.database_url}")
    print(f"  fixture_dir         : {settings.fixture_dir}")
    print(f"  kalshi environment  : {settings.kalshi.environment.value}")
    print(
        f"  kalshi credentials  : "
        f"{'configured' if settings.kalshi.has_credentials else 'not configured'}"
    )
    print(f"  min net profit      : ${settings.opportunity.min_net_profit_usd}")
    print(f"  min return          : {settings.opportunity.min_return_pct}%")
    print(f"  max book age        : {settings.opportunity.max_book_age_ms}ms")
    print(f"  slippage buffer     : {settings.opportunity.slippage_buffer_pct}%")
    print(f"  polymarket product  : {settings.polymarket.product.value}")
    print(f"  ingestion mode      : {settings.ingestion_mode.value}")
    print(f"  USDC haircut        : {settings.polymarket.usdc_haircut_pct}%")

    budget = settings.validate_freshness_budget()
    print(f"  poll cycle          : {settings.trial.cycle_interval_seconds}s")
    print(
        f"  est refresh batch   : {settings.trial.estimated_batch_seconds():.2f}s "
        f"({len(settings.trial.watched_pairs) * 2 or 'no'} watched market(s))"
    )
    print(
        f"  required book age   : {settings.trial.required_book_age_ms()}ms "
        "(refresh batch + clock tolerance)"
    )
    print("                        the cycle interval is not a term: the runtime refreshes")
    print("                        and then scans within one cycle, and skips the scan when")
    print("                        the refresh fails, so no book survives into a later scan")
    print(f"  freshness budget    : {'OK' if budget is None else 'TOO TIGHT'}")
    if budget:
        print(f"    {budget}")
        print("    Reduce the refresh batch (fewer watched markets) before raising the")
        print("    bound: raising it loosens the check that catches genuinely stale books.")

    print("\nMarket-data access:")
    print("  Kalshi public REST      : available without credentials")
    print(
        "  Kalshi WebSocket        : requires authentication "
        "(handshake is signed even for public channels)"
    )
    if settings.polymarket.product is PolymarketProduct.US:
        print("  Polymarket US REST      : available without credentials (public gateway)")
        print("  Polymarket US WebSocket : requires the authenticated API — unavailable here")
    else:
        print("  Polymarket Global       : requires eligibility confirmation")
    credentialled = settings.ingestion_mode is IngestionMode.WEBSOCKET
    print(
        f"  selected ingestion      : {settings.ingestion_mode.value}"
        f"{' (needs credentials)' if credentialled else ' (credential-free)'}"
    )

    print("\nSafety:")
    print(f"  trading_enabled setting : {settings.trading_enabled}")
    print(f"  live trading possible   : {settings.live_trading_possible}")
    from event_arb.execution.disabled import DisabledExecutionAdapter, LiveTradingDisabledError

    adapter = DisabledExecutionAdapter(trading_enabled=settings.trading_enabled)
    try:
        await adapter.get_order(Venue.KALSHI, "probe")
    except LiveTradingDisabledError:
        print("  execution adapter       : refuses all order operations (verified)")
    else:  # pragma: no cover - would indicate a broken safety guarantee
        print("  execution adapter       : DID NOT REFUSE — DO NOT RUN THIS BUILD")
        return 1

    if settings.data_source is DataSource.LIVE:
        try:
            settings.validate_live_market_data()
        except ValueError as exc:
            print(f"\n  LIVE MARKET DATA BLOCKED: {exc}")
            return 1
        print("\n  NOTE: live market data is enabled (read-only). Confirm you have")
        print("  reviewed each venue's developer terms and rate limits.")
    return 0


async def _cmd_settle(settings: Settings, args: argparse.Namespace) -> int:
    """Record the real outcome of a paper trade and credit its payouts.

    Deliberately operator-driven. The outcome is entered by a named person alongside the
    settlement authority they read, because an automatic outcome feed that is wrong, late,
    or disagreeing between venues would silently rewrite the trial's own P&L — and a first
    one-pair trial does not need that surface to reconcile a settlement.

    Nothing here can place an order. It moves simulated cash in the local ledger only.
    """
    from event_arb.domain.enums import Outcome
    from event_arb.paper.executor import SettlementOutcomes
    from event_arb.paper.settlement import SettlementError
    from event_arb.persistence.database import Database

    _banner(settings)
    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    try:
        # Cash and history first. Crediting a payout into freshly-invented starting
        # balances would be worse than not crediting it at all.
        await scanner.restore_portfolio()

        if args.list:
            open_trades = scanner.unsettled_trades()
            print(f"\n  {len(open_trades)} unsettled paper trade(s)\n")
            for trade in open_trades:
                print(
                    f"    {trade.id}  pair={trade.pair_id[:12]}  "
                    f"execution={trade.execution_status.value}  "
                    f"hedged={trade.hedged_quantity}  unhedged={trade.unhedged_quantity}  "
                    f"opened={trade.created_at.isoformat()}"
                )
            if not open_trades:
                print("    (none)")
            return 0

        if not args.trade_id:
            print("\n  --trade-id is required unless --list is given.")
            return 1
        if not args.recorded_by.strip():
            print("\n  --recorded-by is required: a settlement is attributable or it is not")
            print("  evidence. Name the person recording it.")
            return 1
        if not args.evidence.strip():
            print("\n  --evidence is required: record the settlement authority and the")
            print("  document you read, so the entry can be audited later.")
            return 1

        kalshi_outcome = Outcome(args.kalshi_outcome.upper())
        polymarket_outcome = (
            Outcome(args.polymarket_outcome.upper()) if args.polymarket_outcome else None
        )
        if polymarket_outcome is None:
            # Derived through the reviewer-recorded inversion flag, never inferred from
            # the titles. Pass --polymarket-outcome explicitly to record a genuine
            # cross-venue settlement discrepancy.
            known = next((t for t in scanner.portfolio.trades if t.id == args.trade_id), None)
            # Read from storage, not from `scanner.state.pairs`: this command restores the
            # portfolio and nothing else, so that map is always empty here. The lookup
            # silently missed every time and the flag fell back to False, settling an
            # inverted pair as though it were aligned — a wrong payout, with no symptom.
            pair = await scanner.stored_pair(known.pair_id) if known else None
            if pair is None and not args.outcomes_inverted:
                print("\n  CANNOT ESTABLISH OUTCOME ALIGNMENT")
                print(f"    no stored pair for trade {args.trade_id!r}, so whether Polymarket")
                print("    YES corresponds to Kalshi YES is unknown. Assuming they align is")
                print("    how an inverted pair gets paid the wrong way round, so this refuses.")
                print("\n    Give the outcome explicitly with --polymarket-outcome YES|NO,")
                print("    or state the inversion with --outcomes-inverted.")
                return 1
            inverted = bool(args.outcomes_inverted or (pair and pair.outcomes_inverted))
            print(
                f"  outcome alignment   : {'inverted' if inverted else 'aligned'} "
                f"({'operator-stated' if args.outcomes_inverted else 'from the reviewed pair'})"
            )
            outcomes = SettlementOutcomes.for_event(
                kalshi_outcome,
                outcomes_inverted=inverted,
                evidence=args.evidence,
                recorded_by=args.recorded_by,
            )
        else:
            outcomes = SettlementOutcomes(
                kalshi=kalshi_outcome,
                polymarket=polymarket_outcome,
                evidence=args.evidence,
                recorded_by=args.recorded_by,
            )

        print(f"\n  Kalshi resolves     : {outcomes.kalshi.value}")
        print(f"  Polymarket resolves : {outcomes.polymarket.value}")
        print(f"  recorded by         : {args.recorded_by}")
        print(f"  evidence            : {args.evidence}")

        if args.dry_run:
            print("\n  DRY RUN: nothing was written.")
            return 0

        try:
            record = await scanner.settle_paper_trade(args.trade_id, outcomes)
        except SettlementError as exc:
            print(f"\n  SETTLEMENT REFUSED: {exc}")
            return 1

        if record.already_settled:
            print("\n  Already settled; no payout was credited a second time.")
        print(f"\n  credited  : {record.credited}")
        print(f"  final P&L : {record.final_pnl}")
        print(f"  hedge held: {record.hedge_held}")
        result = record.trade.settlement_result or {}
        print(f"  payouts   : {result.get('payouts_by_account')}")
        print(f"\n  balances  : {scanner.portfolio.balances}")
        return 0
    finally:
        await scanner.close()


async def _cmd_reconcile(settings: Settings, args: argparse.Namespace) -> int:
    """Recompute every paper account from its stored ledger and check that it closes.

    Read-only, and now actually so. It used to call ``restore_portfolio``, which reaps
    holds left by a dead process and persists the release — so a command documented as
    inspection wrote to the database, and running it against a *live* trial released the
    reservations that trial was using. Recovery and reporting are separate operations,
    and only one of them belongs in a command an operator runs to look at a number.

    It re-derives each balance from the entries on disk, and independently checks each
    retained trade's cash against the ledger that should hold it, so it answers the
    question a running process cannot answer about itself: are the figures this trial
    reported reproducible from storage?

    Exits non-zero when they are not — an account does not balance, a trade's cash is
    missing, the run recorded an attempt it could not store, or an earlier run died
    without shutting down. Each means the evidence has a hole in it, and an acceptance
    gate should fail rather than read the rates above it.
    """
    from event_arb.persistence.database import Database

    _banner(settings)
    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    try:
        restored = await scanner.inspect_portfolio()
        report = reconcile_accounts(scanner.portfolio)
        if not restored.is_complete:
            report.stop_causes.append(
                f"portfolio restore incomplete: {len(restored.trades_unreadable)} stored "
                "trade(s) could not be rebuilt, so exposure may be understated"
            )
        print()
        print(report.to_text())
        if args.json:
            destination = Path(args.json)
            await asyncio.to_thread(
                destination.write_text, json.dumps(report.as_dict(), indent=2) + "\n"
            )
            print(f"\n  written: {destination}")
        if not report.reconciles:
            print("\n  ACCOUNTS DO NOT RECONCILE — do not read the figures above as a result")
            return 1
        if not report.evidence_is_complete:
            print("\n  EVIDENCE INCOMPLETE — these figures do not cover everything that happened")
            if report.invalidated:
                print(f"    {len(report.invalidated)} attempt(s) storage never accepted")
            if report.unfinished_runs:
                print(
                    f"    {len(report.unfinished_runs)} earlier run(s) ended without a clean "
                    "shutdown; review, then `event-arb acknowledge-run <id>`"
                )
            if not report.integrity.is_sound:
                print(
                    f"    {len(report.integrity.findings)} trade(s) whose stored cash does "
                    "not match what the trade implies"
                )
            return 1
        if not restored.is_complete:
            print("\n  RESTORE INCOMPLETE — some stored trades could not be rebuilt")
            return 1
        return 0
    finally:
        await scanner.close()


async def _cmd_manifest(settings: Settings, args: argparse.Namespace) -> int:
    """Hash the code and configuration this run would use, by content.

    Replaces ``git status --porcelain | shasum -a 256``, which hashed a list of filenames
    and status letters — identical for two trees whose uncommitted files differ in every
    line. Most of this project's implementation is uncommitted, so that is the one case
    it could not survive, and the case the evidence most needs it to.
    """
    from event_arb.manifest import build_manifest

    _banner(settings)
    manifest = await asyncio.to_thread(build_manifest, settings)
    print()
    print(manifest.render())
    if args.json:
        destination = Path(args.json)
        await asyncio.to_thread(destination.write_text, manifest.to_json())
        print(f"\n  written: {destination}")
    return 0


async def _cmd_acknowledge_run(settings: Settings, args: argparse.Namespace) -> int:
    """Record an operator's review of a run that ended without a clean shutdown.

    This is the only way the evidence gate stops blocking on a crashed run, and it works
    by *appending* — the crash record stays in the journal forever, with this
    acknowledgement beside it. Nothing is deleted, so the gate going green always leaves
    behind a named person and a stated reason rather than a gap where a failure was.

    Both ``--by`` and ``--note`` are required for that reason: an unattributed
    acknowledgement would be indistinguishable from someone making the warning go away.
    """
    from event_arb.persistence.database import Database

    _banner(settings)
    if not args.by or not args.note:
        print("\n  REFUSED: --by and --note are both required")
        print("    acknowledging a crashed run is a claim that someone looked at it;")
        print("    it is recorded permanently and must say who, and what they found")
        return 1

    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    try:
        history = RunJournal.read(scanner.journal.path if scanner.journal else None)
        unfinished = {run.run_id for run in history.unfinished}
        if args.run_id not in unfinished:
            print(f"\n  REFUSED: {args.run_id} is not an unfinished run in this journal")
            if unfinished:
                print(f"    unfinished runs: {', '.join(sorted(unfinished))}")
            else:
                print("    no run in this journal is unfinished")
            return 1
        if not scanner.acknowledge_run(args.run_id, by=args.by, note=args.note):
            print("\n  FAILED: the acknowledgement could not be written to the journal")
            return 1
        print(f"\n  acknowledged run {args.run_id}")
        print(f"    by   : {args.by}")
        print(f"    note : {args.note}")
        print(f"    the original failure record remains in {scanner.journal.path}")  # type: ignore[union-attr]
        await scanner.audit(
            args.by,
            "run.acknowledged",
            args.run_id,
            {"note": args.note},
        )
        return 0
    finally:
        await scanner.close()


async def _cmd_smoke_test(settings: Settings, args: argparse.Namespace) -> int:
    """Run the separately approved, public-REST-only schema smoke test."""
    from event_arb.smoke_test import SmokeTestError, run_supervised_smoke_test

    database_path = Path(args.database)
    resolved_database = await asyncio.to_thread(database_path.resolve)
    configured = settings.model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{resolved_database}"}
    )
    doctor_result = await _cmd_doctor(configured, args)
    if doctor_result != 0:
        return doctor_result
    try:
        return await run_supervised_smoke_test(
            configured,
            database_path,
            authorized=args.yes_i_authorize_live_requests,
        )
    except SmokeTestError as exc:
        print(f"\n  SMOKE TEST BLOCKED: {exc}")
        print("  No request was made.")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="event-arb",
        description="Cross-venue prediction-market arbitrage scanner (read-only + paper trading).",
    )
    parser.add_argument("--log-level", default=None)
    parser.add_argument(
        "--log-format",
        default=None,
        choices=["json", "console"],
        help="console is easier to read when running interactively",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="detect opportunities once and print them")
    paper = subparsers.add_parser("paper", help="scan, then simulate two-leg execution")
    pairs = subparsers.add_parser("pairs", help="list candidate pairs and rule differences")
    subparsers.add_parser("doctor", help="print configuration and verify the safety posture")
    smoke = subparsers.add_parser(
        "smoke-test",
        help="run the approved bounded public-REST production schema smoke test",
    )
    smoke.add_argument(
        "--database",
        required=True,
        help="new dedicated SQLite file; data/event_arb.db and existing files are refused",
    )
    smoke.add_argument(
        "--yes-i-authorize-live-requests",
        action="store_true",
        help="authorize the exact five-row request plan printed after doctor",
    )
    settle_cmd = subparsers.add_parser(
        "settle",
        help="record a paper trade's real outcome and credit its payouts (no real orders)",
    )
    settle_cmd.add_argument("--trade-id", default="", help="the paper trade's id")
    settle_cmd.add_argument(
        "--list", action="store_true", help="list unsettled paper trades and exit"
    )
    settle_cmd.add_argument(
        "--kalshi-outcome",
        default="YES",
        choices=["YES", "NO", "yes", "no"],
        help="the outcome that won on Kalshi",
    )
    settle_cmd.add_argument(
        "--polymarket-outcome",
        default="",
        choices=["YES", "NO", "yes", "no", ""],
        help=(
            "the outcome that won on Polymarket. Omit to derive it from the pair's "
            "reviewer-recorded inversion; pass it to record a genuine cross-venue "
            "settlement discrepancy."
        ),
    )
    settle_cmd.add_argument(
        "--outcomes-inverted",
        action="store_true",
        help="treat Polymarket YES as Kalshi NO when the pair is not in local state",
    )
    settle_cmd.add_argument(
        "--recorded-by", default="", help="the person recording this outcome (required)"
    )
    settle_cmd.add_argument(
        "--evidence",
        default="",
        help="settlement authority and document consulted (required)",
    )
    settle_cmd.add_argument(
        "--dry-run", action="store_true", help="print the resolution without writing"
    )
    reconcile_cmd = subparsers.add_parser(
        "reconcile",
        help="re-derive every paper account from the stored ledger and check it balances",
    )
    reconcile_cmd.add_argument("--json", default="", help="also write the full report to this path")
    manifest_cmd = subparsers.add_parser(
        "manifest",
        help="hash the code and configuration by content, including untracked files",
    )
    manifest_cmd.add_argument(
        "--json", default="", help="also write the full manifest to this path"
    )
    ack_cmd = subparsers.add_parser(
        "acknowledge-run",
        help="record that an operator reviewed an earlier run that died without "
        "shutting down cleanly",
    )
    ack_cmd.add_argument("run_id", help="the unfinished run id, as `reconcile` prints it")
    ack_cmd.add_argument("--by", default="", help="the person who reviewed it (required)")
    ack_cmd.add_argument("--note", default="", help="what the review found (required)")
    trial = subparsers.add_parser(
        "trial", help="run the autonomous paper trial (ingest, scan, paper-execute)"
    )
    trial.add_argument(
        "--reconciliation-json",
        default="",
        help="write the end-of-run account reconciliation to this path",
    )
    trial.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after this many seconds (default: run until interrupted)",
    )
    trial.add_argument(
        "--rest-only",
        action="store_true",
        help="poll REST only; do not open WebSocket connections",
    )
    trial.add_argument(
        "--observation-only",
        action="store_true",
        help=(
            "ingest and scan but execute no paper trade. An explicit mode: without it a "
            "run with no approved pair is refused rather than quietly observing."
        ),
    )
    candidates = subparsers.add_parser(
        "candidates", help="bounded candidate research (never approves or trades)"
    )
    candidate_actions = candidates.add_subparsers(dest="candidates_command", required=True)
    discover = candidate_actions.add_parser(
        "discover", help="generate a capped shortlist of cross-venue candidates"
    )
    discover.add_argument("--kalshi-limit", type=int, default=250)
    discover.add_argument("--polymarket-limit", type=int, default=250)
    discover.add_argument("--min-score", default="0.55")
    discover.add_argument("--max-per-market", type=int, default=3)
    discover.add_argument("--max-results", type=int, default=100)
    discover.add_argument(
        "--recheck-top",
        type=int,
        default=25,
        help=(
            "ask the venue whether the top N shortlisted ids still resolve. The response "
            "body is not read, so this is an existence check, not verification."
        ),
    )
    discover.add_argument(
        "--authenticated-mode",
        action="store_true",
        help=(
            "authorize sending configured venue credentials. Without it the command "
            "refuses to run when credentials are present, because the request plan it "
            "shows is the credential-free one."
        ),
    )
    discover.add_argument(
        "--yes-i-authorize-live-requests",
        action="store_true",
        help=(
            "skip the interactive confirmation for live requests. Named explicitly so it "
            "cannot be typed by accident or copied without noticing."
        ),
    )

    serve = subparsers.add_parser("serve", help="run the dashboard")

    for sub in (scan, paper):
        sub.add_argument(
            "--replay-recorded-reviews",
            action="store_true",
            help=(
                "fixture data only: replay the human reviews recorded in "
                "tests/fixtures/recorded_reviews.json. Refuses to run against live data."
            ),
        )
    paper.add_argument(
        "--include-candidates",
        action="store_true",
        help="also paper-trade unapproved research candidates",
    )
    paper.add_argument("--export", default=None, help="write JSON results to this path")
    pairs.set_defaults(replay_recorded_reviews=False)

    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(
        args.log_level or settings.log_level,
        args.log_format or ("console" if sys.stdout.isatty() else settings.log_format),
    )

    if args.command == "serve":
        import uvicorn

        from event_arb.api.app import create_app

        uvicorn.run(
            create_app(settings),
            host=args.host or settings.api_host,
            port=args.port or settings.api_port,
        )
        return 0

    handlers = {
        "scan": _cmd_scan,
        "paper": _cmd_paper,
        "pairs": _cmd_pairs,
        "doctor": _cmd_doctor,
        "settle": _cmd_settle,
        "reconcile": _cmd_reconcile,
        "acknowledge-run": _cmd_acknowledge_run,
        "manifest": _cmd_manifest,
        "smoke-test": _cmd_smoke_test,
        "trial": _cmd_trial,
        "candidates": _cmd_candidates,
    }
    return asyncio.run(handlers[args.command](settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
