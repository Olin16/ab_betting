"""Supervised, bounded, public-REST-only production schema smoke test.

This module is deliberately separate from candidate research and paper execution. It can
read at most two candidate pairs and one book per leg, but cannot approve, paper-trade,
sign, submit, cancel, or inspect an order.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from event_arb.candidates import (
    CandidateRecord,
    DiscoveryLimits,
    DiscoveryReport,
    RequestPlan,
    build_request_plan,
    research_candidates,
    top_slugs_for_recheck,
)
from event_arb.config import (
    DataSource,
    IngestionMode,
    KalshiEnvironment,
    PolymarketProduct,
    Settings,
)
from event_arb.domain.enums import Venue, VenueProduct
from event_arb.domain.models import OrderBook, VenueMarket
from event_arb.matching.normalizer import NormalizedMarket
from event_arb.persistence.database import Database
from event_arb.scanner import Scanner
from event_arb.venues.base import MarketDataError, VenueAdapter
from event_arb.venues.kalshi.client import KalshiClient
from event_arb.venues.polymarket_us.client import PolymarketUSClient

__all__ = [
    "RequestRecord",
    "SmokeTestError",
    "SmokeTestResult",
    "assert_documented_request_contract",
    "run_supervised_smoke_test",
    "select_smoke_candidates",
]

_KALSHI_PROD = "https://external-api.kalshi.com/trade-api/v2"
_POLYMARKET_US_PUBLIC = "https://gateway.polymarket.us"
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "x-api-key",
        "kalshi-access-key",
        "kalshi-access-signature",
        "kalshi-access-timestamp",
    }
)
_DEFAULT_DATABASE = Path("data/event_arb.db").resolve()


class SmokeTestError(RuntimeError):
    """A fail-closed smoke-test condition."""


@dataclass
class RequestRecord:
    method: str
    url: str
    purpose: str
    started_at: datetime
    status: int | None = None
    elapsed_seconds: float | None = None
    retry_after: str | None = None
    cursor_returned: bool = False


@dataclass
class SmokeTestResult:
    passed: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    elapsed_seconds: float = 0.0
    requests: list[RequestRecord] = field(default_factory=list)
    selected: list[CandidateRecord] = field(default_factory=list)
    schema_differences: list[str] = field(default_factory=list)
    mapper_failures: list[str] = field(default_factory=list)
    failure: str = ""


@dataclass(frozen=True)
class _AllowedRequest:
    method: str
    pattern: re.Pattern[str]
    params: dict[str, str] | None
    maximum: int
    purpose: str


class SmokeRequestGuard:
    """Pre-send endpoint/credential/budget gate plus response audit trail."""

    def __init__(self, maximum_requests: int = 31) -> None:
        self.maximum_requests = maximum_requests
        self.records: list[RequestRecord] = []
        self._allowed: list[_AllowedRequest] = []
        self._counts: dict[int, int] = {}

    def configure(self, plan: RequestPlan) -> None:
        allowed: list[_AllowedRequest] = []
        for entry in plan.entries:
            escaped = re.escape(entry.url)
            escaped = re.sub(r"\\\{(?:id|ticker|condition_id|token_id)\\\}", r"[^/?]+", escaped)
            maximum = int(entry.count.split("..")[-1])
            params = (
                {str(key): str(value) for key, value in entry.params.items()}
                if isinstance(entry.params, dict)
                else None
            )
            allowed.append(
                _AllowedRequest(
                    method=entry.method,
                    pattern=re.compile(f"^{escaped}$"),
                    params=params,
                    maximum=maximum,
                    purpose=entry.purpose,
                )
            )
        self._allowed = allowed
        self._counts.clear()

    async def on_request(self, request: httpx.Request) -> None:
        if not self._allowed:
            raise SmokeTestError("request guard was not configured")
        if len(self.records) >= self.maximum_requests:
            raise SmokeTestError(f"request budget exceeded ({self.maximum_requests})")

        sensitive = sorted(name for name in request.headers if name.lower() in _SENSITIVE_HEADERS)
        if sensitive:
            raise SmokeTestError(
                "credential or cookie header detected before send: " + ", ".join(sensitive)
            )

        url_without_query = str(request.url.copy_with(query=None))
        match_index: int | None = None
        for index, allowed in enumerate(self._allowed):
            if request.method != allowed.method or not allowed.pattern.fullmatch(url_without_query):
                continue
            actual_params = dict(request.url.params.multi_items())
            if allowed.params is not None and actual_params != allowed.params:
                continue
            if allowed.params is None and actual_params:
                continue
            match_index = index
            break
        if match_index is None:
            raise SmokeTestError(
                f"unexpected endpoint or params before send: {request.method} {request.url}"
            )

        allowed = self._allowed[match_index]
        count = self._counts.get(match_index, 0) + 1
        if count > allowed.maximum:
            raise SmokeTestError(f"request-row budget exceeded for {allowed.purpose}")
        self._counts[match_index] = count
        record = RequestRecord(
            method=request.method,
            url=str(request.url),
            purpose=allowed.purpose,
            started_at=datetime.now(UTC),
        )
        self.records.append(record)
        request.extensions["event_arb_smoke_record"] = len(self.records) - 1
        request.extensions["event_arb_smoke_started"] = time.monotonic()

    async def on_response(self, response: httpx.Response) -> None:
        index = response.request.extensions.get("event_arb_smoke_record")
        started = response.request.extensions.get("event_arb_smoke_started")
        if not isinstance(index, int) or index >= len(self.records):
            raise SmokeTestError("response arrived without a request audit record")
        record = self.records[index]
        record.status = response.status_code
        record.retry_after = response.headers.get("Retry-After")
        if isinstance(started, float):
            record.elapsed_seconds = time.monotonic() - started

    def mark_last_cursor(self, returned: bool) -> None:
        if self.records:
            self.records[-1].cursor_returned = returned


def assert_documented_request_contract(plan: RequestPlan) -> None:
    """Refuse if the derived plan differs from smoke-test-plan.md §1."""
    expected = (
        ("GET", f"{_KALSHI_PROD}/markets", "1"),
        ("GET", f"{_POLYMARKET_US_PUBLIC}/v1/markets", "1"),
        ("GET", f"{_POLYMARKET_US_PUBLIC}/v1/market/slug/{{id}}", "0..25"),
        ("GET", f"{_KALSHI_PROD}/markets/{{ticker}}/orderbook", "0..2"),
        ("GET", f"{_POLYMARKET_US_PUBLIC}/v1/markets/{{id}}/book", "0..2"),
    )
    actual = tuple((entry.method, entry.url, entry.count) for entry in plan.entries)
    differences: list[str] = []
    if actual != expected:
        differences.append(f"endpoint rows differ: expected={expected!r}, actual={actual!r}")
    if plan.worst_case_requests != 31:
        differences.append(f"worst-case requests is {plan.worst_case_requests}, expected 31")
    if plan.sends_any_credentials:
        differences.append("derived plan would send credentials")
    if plan.polymarket_product is not VenueProduct.POLYMARKET_US:
        differences.append(f"product is {plan.polymarket_product.value}, expected polymarket_us")
    if plan.orderbook_pairs != 2:
        differences.append(f"orderbook pair scope is {plan.orderbook_pairs}, expected 2")
    if differences:
        raise SmokeTestError("derived request plan does not match §1: " + "; ".join(differences))


def _validate_preflight(settings: Settings, database_path: Path) -> None:
    if settings.data_source is not DataSource.LIVE:
        raise SmokeTestError("smoke-test requires EVENT_ARB_DATA_SOURCE=live")
    if settings.kalshi.environment is not KalshiEnvironment.PROD:
        raise SmokeTestError("smoke-test requires Kalshi production")
    if settings.polymarket.product is not PolymarketProduct.US:
        raise SmokeTestError("smoke-test requires Polymarket US")
    if settings.ingestion_mode is not IngestionMode.REST_POLLING:
        raise SmokeTestError("smoke-test requires credential-free REST polling")
    if settings.kalshi.has_credentials:
        raise SmokeTestError("Kalshi credentials are configured; unset them before smoke-test")
    resolved = database_path.resolve()
    if resolved == _DEFAULT_DATABASE:
        raise SmokeTestError("refusing to use data/event_arb.db for the smoke test")
    temporary_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),  # noqa: S108 - canonical shared temporary root on POSIX
    }
    if not any(resolved.is_relative_to(root) for root in temporary_roots):
        raise SmokeTestError(
            "smoke-test database must be under a temporary directory: "
            + ", ".join(str(root) for root in sorted(temporary_roots))
        )
    if database_path.exists():
        raise SmokeTestError(f"dedicated smoke-test database already exists: {database_path}")


def _validate_polymarket_schema(markets: list[VenueMarket]) -> None:
    for market in markets:
        raw = market.raw_metadata
        if "feeCoefficient" not in raw:
            raise SmokeTestError(f"{market.venue_market_id}: feeCoefficient is missing")
        if not isinstance(raw.get("outcomes"), str):
            raise SmokeTestError(f"{market.venue_market_id}: outcomes is not a JSON string")
        tick = raw.get("orderPriceMinTickSize")
        try:
            if Decimal(str(tick)) <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            raise SmokeTestError(
                f"{market.venue_market_id}: orderPriceMinTickSize is missing or non-positive"
            ) from None
        if "resolutionSource" in raw:
            raise SmokeTestError(
                f"{market.venue_market_id}: resolutionSource unexpectedly appeared"
            )


def _validate_kalshi_schema(markets: list[VenueMarket]) -> None:
    required = {
        "ticker",
        "title",
        "event_ticker",
        "yes_bid_dollars",
        "volume_fp",
        "status",
        "close_time",
        "price_ranges",
    }
    for market in markets:
        raw = market.raw_metadata
        missing = sorted(required - raw.keys())
        if missing:
            raise SmokeTestError(
                f"{market.venue_market_id}: documented Kalshi fields missing: " + ", ".join(missing)
            )
        if not isinstance(raw.get("yes_bid_dollars"), str):
            raise SmokeTestError(f"{market.venue_market_id}: yes_bid_dollars is not a string")
        if not isinstance(raw.get("volume_fp"), str):
            raise SmokeTestError(f"{market.venue_market_id}: volume_fp is not a string")
        if "tick_size_dollars" in raw:
            raise SmokeTestError(
                f"{market.venue_market_id}: tick_size_dollars unexpectedly reappeared"
            )
        if "settlement_source" in raw:
            raise SmokeTestError(
                f"{market.venue_market_id}: settlement_source unexpectedly appeared"
            )


def _has_nonempty_ask(book: OrderBook) -> bool:
    return bool(book.yes.asks or book.no.asks)


def _style(record: CandidateRecord) -> str:
    text = f"{record.kalshi_title} {record.polymarket_title}".lower()
    if any(token in text for token in ("federal reserve", "fomc", "fed decision", "fed rate")):
        return "fed"
    if any(token in text for token in ("weather", "temperature", "degrees", "rain", "snow")):
        return "weather"
    if any(token in text for token in ("cpi", "consumer price", "inflation")):
        return "cpi"
    return "other"


def select_smoke_candidates(report: DiscoveryReport, maximum: int = 2) -> list[CandidateRecord]:
    """Pick the strongest distinct legs, preferring Fed plus weather/CPI."""
    ordered = sorted(report.records, key=lambda record: record.score, reverse=True)
    selected: list[CandidateRecord] = []
    used_kalshi: set[str] = set()
    used_polymarket: set[str] = set()

    def add_first(styles: set[str]) -> None:
        for record in ordered:
            if len(selected) >= maximum or _style(record) not in styles:
                continue
            if record.pair.kalshi_market_id in used_kalshi:
                continue
            if record.pair.polymarket_market_id in used_polymarket:
                continue
            selected.append(record)
            used_kalshi.add(record.pair.kalshi_market_id)
            used_polymarket.add(record.pair.polymarket_market_id)
            return

    add_first({"fed"})
    add_first({"weather", "cpi"})
    for record in ordered:
        if len(selected) >= maximum:
            break
        if record.pair.kalshi_market_id in used_kalshi:
            continue
        if record.pair.polymarket_market_id in used_polymarket:
            continue
        selected.append(record)
        used_kalshi.add(record.pair.kalshi_market_id)
        used_polymarket.add(record.pair.polymarket_market_id)
    return selected


def _policy_excerpt(rules: str, words: tuple[str, ...]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", rules.strip())
    matches = [
        sentence for sentence in sentences if any(word in sentence.lower() for word in words)
    ]
    return " ".join(matches) if matches else "Not separately stated in the retrieved rules."


def _print_rule_comparisons(
    selected: list[CandidateRecord], markets: dict[tuple[Venue, str], VenueMarket]
) -> None:
    print("\n=== MANUAL RULE REVIEW (selection is not equivalence approval) ===")
    for index, record in enumerate(selected, 1):
        kalshi = markets[(Venue.KALSHI, record.pair.kalshi_market_id)]
        polymarket = markets[(Venue.POLYMARKET, record.pair.polymarket_market_id)]
        print(f"\nPair {index}: {record.pair.id}  score={record.score} style={_style(record)}")
        for label, market in (("Kalshi", kalshi), ("Polymarket US", polymarket)):
            normalized = NormalizedMarket.from_market(
                market.venue_market_id, market.title, market.rules_text, market.close_time
            )
            print(f"\n  {label}")
            print(f"    market id          : {market.venue_market_id}")
            print(f"    title              : {market.title}")
            print(f"    fingerprint        : {market.settlement_fingerprint}")
            print(f"    cutoff time        : {market.close_time}")
            print(f"    settlement source  : {market.settlement_source or '(unnamed)'}")
            print(
                "    threshold semantics: "
                f"value={normalized.threshold} operator={normalized.operator} "
                f"unit={normalized.unit}"
            )
            print(
                "    postponement/cancel: "
                + _policy_excerpt(market.rules_text, ("postpon", "cancel", "delay"))
            )
            print(
                "    invalid/void        : "
                + _policy_excerpt(market.rules_text, ("invalid", "void", "abandon"))
            )
            print("    complete rules:")
            print(market.rules_text or "      (missing)")
        print("\n  Identified differences:")
        if not record.comparison.differences:
            print("    (none identified automatically; this is not an equivalence conclusion)")
        for difference in record.comparison.differences:
            print(
                f"    - {difference.dimension}: Kalshi={difference.kalshi_value!r}; "
                f"Polymarket={difference.polymarket_value!r}; material={difference.is_material}; "
                f"{difference.explanation}"
            )


def _print_result(result: SmokeTestResult) -> None:
    print("\n=== SMOKE TEST REQUEST AUDIT ===")
    for index, request in enumerate(result.requests, 1):
        elapsed = "n/a" if request.elapsed_seconds is None else f"{request.elapsed_seconds:.3f}s"
        print(
            f"  {index:02d}. {request.method} {request.url} status={request.status} "
            f"elapsed={elapsed} cursor={request.cursor_returned} "
            f"retry_after={request.retry_after or 'none'}"
        )
    print(f"  request count       : {len(result.requests)}")
    print(f"  elapsed time        : {result.elapsed_seconds:.3f}s")
    print(f"  schema differences  : {result.schema_differences or 'none'}")
    print(f"  mapper failures     : {result.mapper_failures or 'none'}")
    print("  credential endpoints: none")
    print("  authenticated/order endpoints: none")
    print(f"  result              : {'PASS' if result.passed else 'FAIL'}")
    if result.failure:
        print(f"  failure             : {result.failure}")


async def run_supervised_smoke_test(
    settings: Settings,
    database_path: Path,
    *,
    authorized: bool,
) -> int:
    """Run the exact bounded test after doctor has passed and approval is confirmed."""
    _validate_preflight(settings, database_path)
    guard = SmokeRequestGuard()
    result = SmokeTestResult()
    started = time.monotonic()
    resolved_database = await asyncio.to_thread(database_path.resolve)
    database_url = f"sqlite+aiosqlite:///{resolved_database}"
    configured = settings.model_copy(update={"database_url": database_url})

    async with (
        httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            event_hooks={"request": [guard.on_request], "response": [guard.on_response]},
        ) as kalshi_http,
        httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            event_hooks={"request": [guard.on_request], "response": [guard.on_response]},
        ) as polymarket_http,
    ):
        adapters: dict[Venue, VenueAdapter] = {
            Venue.KALSHI: KalshiClient(configured.kalshi, client=kalshi_http),
            Venue.POLYMARKET: PolymarketUSClient(configured.polymarket, client=polymarket_http),
        }
        limits = DiscoveryLimits(recheck_top=25)
        plan = build_request_plan(
            limits,
            adapters[Venue.KALSHI],
            adapters[Venue.POLYMARKET],
            VenueProduct.POLYMARKET_US,
            orderbook_pairs=2,
        )
        print("\n" + plan.render() + "\n")
        assert_documented_request_contract(plan)
        guard.configure(plan)
        if not authorized:
            raise SmokeTestError(
                "live requests were not authorized; pass --yes-i-authorize-live-requests"
            )

        database = Database(database_url)
        await database.create_all()
        scanner = Scanner(configured, adapters=adapters, database=database)
        try:
            kalshi_adapter = adapters[Venue.KALSHI]
            polymarket_adapter = adapters[Venue.POLYMARKET]
            if not isinstance(polymarket_adapter, PolymarketUSClient):
                raise SmokeTestError("smoke-test did not construct the Polymarket US client")

            kalshi_markets = await kalshi_adapter.discover_markets(limit=limits.kalshi_limit)
            kalshi_cursor = getattr(kalshi_adapter, "last_pagination_cursor", None)
            guard.mark_last_cursor(bool(kalshi_cursor))
            failures = int(getattr(kalshi_adapter, "last_parse_failures", 0))
            if failures:
                result.mapper_failures.append(f"Kalshi catalogue had {failures} mapper failure(s)")
                raise SmokeTestError(f"Kalshi catalogue had {failures} mapper failure(s)")
            if not kalshi_markets:
                raise SmokeTestError("Kalshi catalogue was empty")
            _validate_kalshi_schema(kalshi_markets)

            polymarket_markets = await polymarket_adapter.discover_markets(
                limit=limits.polymarket_limit
            )
            polymarket_cursor = getattr(polymarket_adapter, "last_pagination_cursor", None)
            guard.mark_last_cursor(bool(polymarket_cursor))
            failures = int(getattr(polymarket_adapter, "last_parse_failures", 0))
            if failures:
                result.mapper_failures.append(
                    f"Polymarket US catalogue had {failures} mapper failure(s)"
                )
                raise SmokeTestError(f"Polymarket US catalogue had {failures} mapper failure(s)")
            if not polymarket_markets:
                raise SmokeTestError("Polymarket US catalogue was empty")
            _validate_polymarket_schema(polymarket_markets)

            report = research_candidates(kalshi_markets, polymarket_markets, limits)
            for slug in top_slugs_for_recheck(report, limits):
                before = len(guard.records)
                exists = await polymarket_adapter.market_exists(slug)
                request = guard.records[-1] if len(guard.records) > before else None
                if request is None:
                    raise SmokeTestError(f"existence recheck sent no request for {slug}")
                if request.status == 404:
                    continue
                if request.status != 200:
                    raise SmokeTestError(f"existence recheck returned {request.status} for {slug}")
                if not exists:
                    raise SmokeTestError(f"existence recheck returned no market for {slug}")

            selected = select_smoke_candidates(report)
            result.selected = selected
            if len(selected) != 2:
                raise SmokeTestError(
                    f"bounded catalogues produced {len(selected)} distinct plausible pair(s); "
                    "the approved test requires two"
                )

            by_key = {
                (market.venue, market.venue_market_id): market
                for market in kalshi_markets + polymarket_markets
            }
            legs = [
                by_key[(venue, market_id)]
                for record in selected
                for venue, market_id in (
                    (Venue.KALSHI, record.pair.kalshi_market_id),
                    (Venue.POLYMARKET, record.pair.polymarket_market_id),
                )
            ]
            await scanner.persist_markets_subset(legs)

            books: list[OrderBook] = []
            refresh_started = time.monotonic()
            for record in selected:
                kalshi_book = await kalshi_adapter.fetch_orderbook(record.pair.kalshi_market_id)
                if getattr(kalshi_adapter, "last_orderbook_schema_valid", None) is not True:
                    raise SmokeTestError(
                        f"{record.pair.kalshi_market_id}: Kalshi orderbook_fp schema changed"
                    )
                if not _has_nonempty_ask(kalshi_book):
                    raise SmokeTestError(
                        f"{record.pair.kalshi_market_id}: normalized Kalshi book has no asks"
                    )
                books.append(kalshi_book)

                polymarket_book = await polymarket_adapter.fetch_orderbook(
                    record.pair.polymarket_market_id
                )
                if not _has_nonempty_ask(polymarket_book):
                    raise SmokeTestError(
                        f"{record.pair.polymarket_market_id}: normalized Polymarket "
                        "book has no asks"
                    )
                books.append(polymarket_book)

            refresh_elapsed = time.monotonic() - refresh_started
            maximum_refresh = (
                configured.opportunity.max_book_age_ms / 1000
                - configured.trial.clock_tolerance_seconds
            )
            if refresh_elapsed > maximum_refresh:
                raise SmokeTestError(
                    f"refresh batch {refresh_elapsed:.3f}s exceeds freshness budget "
                    f"{maximum_refresh:.3f}s"
                )
            await scanner.record_books(books)
            await scanner.audit(
                "smoke-test",
                "smoke_test.passed",
                detail={
                    "request_count": len(guard.records),
                    "selected_pair_ids": [record.pair.id for record in selected],
                    "cursor_returned": bool(kalshi_cursor or polymarket_cursor),
                    "refresh_elapsed_seconds": refresh_elapsed,
                },
            )
            result.passed = True
            _print_rule_comparisons(selected, by_key)
        except Exception as exc:
            result.failure = str(exc)
            if isinstance(exc, MarketDataError):
                result.mapper_failures.append(str(exc))
            elif isinstance(exc, SmokeTestError) and any(
                word in str(exc).lower() for word in ("schema", "missing", "outcomes")
            ):
                result.schema_differences.append(str(exc))
            with suppress(Exception):
                await scanner.audit("smoke-test", "smoke_test.failed", detail={"failure": str(exc)})
        finally:
            result.requests = list(guard.records)
            result.elapsed_seconds = time.monotonic() - started
            _print_result(result)
            await scanner.close()
    return 0 if result.passed else 1
