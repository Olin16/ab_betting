"""Command-line entrypoint.

Commands:

``scan``     discover markets, generate pairs, detect opportunities, print the result
``paper``    the above, plus a simulated two-leg execution for each opportunity
``serve``    run the dashboard
``pairs``    list candidate pairs with their rule differences
``doctor``   print configuration and confirm the safety posture

All commands default to fixture data and require no credentials or network access.
"""

from __future__ import annotations

import argparse
import asyncio
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
    from event_arb.runtime import TrialRunner

    database = Database(settings.database_url)
    await database.create_all()
    scanner = Scanner(settings, database=database)

    await scanner.discover()
    report = await scanner.restore()
    if report.suspended:
        print(f"\n  {len(report.suspended)} approval(s) suspended on restore:")
        for pair, reason in report.suspended:
            print(f"    {pair.id[:12]}: {reason}")
    scanner.seed_watched_pairs()
    if not settings.trial.watched_pairs:
        scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.persist_markets()
    await scanner.persist_pairs()

    approved = [p for p in scanner.state.pairs.values() if p.is_tradable_pair]
    print(f"\n  watched pairs : {len(scanner.state.pairs)}")
    print(f"  approved      : {len(approved)}")
    if not approved:
        print("  NOTE: no approved pairs. Only research candidates will be detected,")
        print("        and none will be labeled arbitrage.")

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

    try:
        if args.duration:
            await asyncio.sleep(args.duration)
        else:
            await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.stop()
        print("\n--- Trial summary ---")
        for key, value in runner.stats.as_dict().items():
            print(f"  {key}: {value}")
        report_daily = scanner.portfolio.daily_report()
        print("\n--- Daily paper report ---")
        for key, value in report_daily.as_dict().items():
            print(f"  {key}: {value}")
        await scanner.close()
    return 0


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
    trial = subparsers.add_parser(
        "trial", help="run the autonomous paper trial (ingest, scan, paper-execute)"
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
        "smoke-test": _cmd_smoke_test,
        "trial": _cmd_trial,
        "candidates": _cmd_candidates,
    }
    return asyncio.run(handlers[args.command](settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
