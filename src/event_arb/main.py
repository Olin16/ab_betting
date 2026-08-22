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
from decimal import Decimal
from pathlib import Path

from event_arb.config import DataSource, Settings, get_settings
from event_arb.domain.enums import MatchStatus, Venue
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


async def _prepare(settings: Settings, auto_approve: bool) -> Scanner:
    scanner = Scanner(settings)
    await scanner.discover()
    # Pairs first: the book refresh is scoped to the markets those pairs reference.
    scanner.seed_watched_pairs()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books()

    if auto_approve:
        # Demo convenience only: approves every candidate whose automated rule comparison
        # found no material differences. Real approval is a human reading both rule sets.
        for pair_id, pair in list(scanner.state.pairs.items()):
            if pair.has_material_differences or pair.status is not MatchStatus.SUGGESTED:
                continue
            scanner.approve(
                pair_id,
                approved_by="cli-demo (--auto-approve)",
                reviewer_notes="Auto-approved for demonstration: no material differences found.",
            )
    return scanner


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
    scanner = await _prepare(settings, args.auto_approve)
    _print_scan(scanner)
    await scanner.close()
    return 0


async def _cmd_paper(settings: Settings, args: argparse.Namespace) -> int:
    _banner(settings)
    scanner = await _prepare(settings, args.auto_approve)
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

    if args.export:
        # A one-shot CLI write; offloaded to a thread so the event loop is never blocked.
        await asyncio.to_thread(_write_export, args.export, scanner.portfolio.to_json())
        print(f"\nExported paper results to {args.export}")

    await scanner.close()
    return 0


async def _cmd_pairs(settings: Settings, args: argparse.Namespace) -> int:
    _banner(settings)
    scanner = await _prepare(settings, auto_approve=False)
    from event_arb.matching.rules_diff import compare_rules

    for pair in sorted(scanner.state.pairs.values(), key=lambda p: p.semantic_score, reverse=True):
        kalshi = scanner.state.markets[(Venue.KALSHI, pair.kalshi_market_id)]
        polymarket = scanner.state.markets[(Venue.POLYMARKET, pair.polymarket_market_id)]
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
    print(f"  USDC haircut        : {settings.polymarket.usdc_haircut_pct}%")

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
        print("\n  NOTE: live market data is enabled (read-only). Confirm you have")
        print("  reviewed each venue's developer terms and rate limits.")
    return 0


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
    serve = subparsers.add_parser("serve", help="run the dashboard")

    for sub in (scan, paper):
        sub.add_argument(
            "--auto-approve",
            action="store_true",
            help="demo only: approve candidate pairs that show no material rule differences",
        )
    paper.add_argument(
        "--include-candidates",
        action="store_true",
        help="also paper-trade unapproved research candidates",
    )
    paper.add_argument("--export", default=None, help="write JSON results to this path")
    pairs.set_defaults(auto_approve=False)

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
    }
    return asyncio.run(handlers[args.command](settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
