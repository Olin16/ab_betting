"""The scanner runtime: wires adapters, matching, detection, and paper trading together.

This is the object the CLI and the dashboard both drive. It holds the current view of the
world (markets, pairs, books) and exposes one-shot operations rather than owning a loop,
so a caller can scan once, scan on a timer, or scan on demand from an HTTP request.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from event_arb.config import DataSource, PolymarketProduct, Settings
from event_arb.domain.enums import MatchStatus, Venue, VenueProduct
from event_arb.domain.keys import (
    MarketKey,
    kalshi_key,
    market_key,
    pair_market_keys,
    polymarket_key,
)
from event_arb.domain.models import (
    MarketPair,
    Opportunity,
    OrderBook,
    ReviewedSource,
    VenueMarket,
)
from event_arb.matching.approval import approve_pair
from event_arb.matching.candidates import generate_candidates, pair_id_for
from event_arb.observability.health import HealthReport, build_health_report
from event_arb.observability.logging import get_logger
from event_arb.opportunities.service import OpportunityService, ScanResult
from event_arb.orderbooks.book import BookStore
from event_arb.orderbooks.freshness import FreshnessPolicy, RefreshTiming
from event_arb.paper.executor import PaperExecutor, PaperTrade
from event_arb.paper.portfolio import PaperPortfolio
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import (
    AuditLogRepository,
    MarketPairRepository,
    OpportunityRepository,
    OrderBookSnapshotRepository,
    PaperTradeRepository,
    VenueMarketRepository,
)
from event_arb.persistence.restore import RestoreReport, restore_pairs
from event_arb.venues.base import VenueAdapter
from event_arb.venues.fixture import FixtureAdapter

__all__ = ["Scanner", "build_adapters"]

#: Pair states whose books are worth keeping fresh. Rejected and suspended pairs are
#: deliberately excluded: spending rate-limit budget on them buys nothing.
_WATCHED_STATUSES = frozenset(
    {MatchStatus.SUGGESTED, MatchStatus.UNDER_REVIEW, MatchStatus.APPROVED}
)

log = get_logger(__name__)


def build_adapters(settings: Settings) -> dict[Venue, VenueAdapter]:
    """Construct venue adapters for the configured data source.

    Fixture mode is the default and requires no credentials or network access.
    """
    if settings.data_source is DataSource.FIXTURE:
        product = (
            VenueProduct.POLYMARKET_US
            if settings.polymarket.product is PolymarketProduct.US
            else VenueProduct.POLYMARKET_GLOBAL
        )
        return {
            Venue.KALSHI: FixtureAdapter(Venue.KALSHI, settings.fixture_dir, product=product),
            Venue.POLYMARKET: FixtureAdapter(
                Venue.POLYMARKET, settings.fixture_dir, product=product
            ),
        }

    settings.validate_live_market_data()

    from event_arb.venues.kalshi.client import KalshiClient

    log.warning(
        "scanner.live_market_data",
        note="read-only live market data; order submission remains impossible",
        polymarket_product=settings.polymarket.product.value,
        ingestion_mode=settings.ingestion_mode.value,
    )

    polymarket: VenueAdapter
    if settings.polymarket.product is PolymarketProduct.US:
        from event_arb.venues.polymarket_us.client import PolymarketUSClient

        polymarket = PolymarketUSClient(settings.polymarket)
    else:
        from event_arb.venues.polymarket.client import PolymarketClient

        polymarket = PolymarketClient(settings.polymarket)

    return {
        Venue.KALSHI: KalshiClient(settings.kalshi),
        Venue.POLYMARKET: polymarket,
    }


@dataclass
class ScannerState:
    markets: dict[MarketKey, VenueMarket] = field(default_factory=dict)
    #: Markets whose metadata was retrieved from the venue during *this* process run.
    #: Persisted metadata does not count: a cached copy trivially matches the hash it was
    #: stored with, so trusting it would let an approval survive without the current rules
    #: ever being fetched.
    observed_markets: set[MarketKey] = field(default_factory=set)
    pairs: dict[str, MarketPair] = field(default_factory=dict)
    last_scan: ScanResult | None = None
    paper_trades: list[PaperTrade] = field(default_factory=list)


class Scanner:
    """Coordinates discovery, matching, detection, and paper execution."""

    def __init__(
        self,
        settings: Settings,
        adapters: dict[Venue, VenueAdapter] | None = None,
        database: Database | None = None,
    ) -> None:
        self.settings = settings
        #: Optional: when absent the scanner runs entirely in memory (tests, demos).
        self.database = database
        self.adapters = adapters or build_adapters(settings)
        self.store = BookStore(FreshnessPolicy(max_age_ms=settings.opportunity.max_book_age_ms))
        #: Observed refresh cost, used to justify the freshness bound rather than guess it.
        self.refresh_timing = RefreshTiming()
        self.state = ScannerState()
        self.opportunities = OpportunityService(
            self.store,
            settings.opportunity,
            settings.polymarket,
            kalshi_direct_member=settings.kalshi.direct_member,
        )
        self.portfolio = PaperPortfolio(
            settings.paper.starting_usd_balance, settings.paper.starting_usdc_balance
        )
        self.last_restore: RestoreReport | None = None
        #: True while a continuous feed is running; enables latency-aware paper fills.
        self.streaming = False
        #: Serializes database writes. The trial's refresh, scan, execute and maintenance
        #: loops all persist concurrently; without this their sessions interleave on the
        #: same rows. Every acquisition wraps exactly one session block and never nests,
        #: so it cannot deadlock.
        self._db_lock = asyncio.Lock()

    # ------------------------------------------------------------------ discovery

    @property
    def polymarket_product(self) -> VenueProduct:
        """The Polymarket product this scanner is configured for."""
        return (
            VenueProduct.POLYMARKET_US
            if self.settings.polymarket.product is PolymarketProduct.US
            else VenueProduct.POLYMARKET_GLOBAL
        )

    def _key(self, venue: Venue, market_id: str) -> MarketKey:
        """Key a bare ``(venue, id)`` pair using this scanner's configured product."""
        if venue is Venue.POLYMARKET:
            return polymarket_key(self.polymarket_product, market_id)
        return (venue, VenueProduct.NONE, market_id)

    def _product_matches(self, market: VenueMarket) -> bool:
        """Runtime guard against mixing products in one process.

        Keys carry the product, so a leftover Global row can no longer be mistaken for a US
        market. It would still be *loaded*, though, and a US-configured scanner has no
        adapter that can refresh a Global book — so drop it rather than carry dead state.
        """
        if market.venue is not Venue.POLYMARKET:
            return True
        return market.product is self.polymarket_product

    async def discover(self, market_ids: dict[Venue, list[str]] | None = None) -> list[VenueMarket]:
        """Load market metadata into scanner state.

        When trial pairs are configured, only those markets are retrieved. Listing an
        entire venue at trial scale is slow, is subject to pagination we have not
        implemented, and widens API access beyond the markets under trial — which §3 of
        the Kalshi Developer Agreement ties to your own trading.
        """
        targets = market_ids
        if targets is None and self.settings.trial.watched_pairs:
            targets = self.watched_markets()

        discovered: list[VenueMarket] = []
        for venue, adapter in self.adapters.items():
            wanted = targets.get(venue) if targets else None
            if targets is not None and not wanted:
                continue
            try:
                markets = await adapter.discover_markets(market_ids=wanted)
            except Exception as exc:
                log.error("scanner.discovery_failed", venue=venue.value, error=str(exc))
                continue
            for market in markets:
                if not self._product_matches(market):
                    log.warning(
                        "scanner.product_mismatch_discarded",
                        market_id=market.venue_market_id,
                        market_product=market.product.value,
                        configured_product=self.polymarket_product.value,
                    )
                    continue
                key = market_key(market)
                self.state.markets[key] = market
                self.state.observed_markets.add(key)
            discovered.extend(markets)
        log.info(
            "scanner.discovered",
            count=len(discovered),
            targeted=targets is not None,
        )
        return discovered

    def watched_markets(self) -> dict[Venue, list[str]]:
        """The markets this scanner should keep fresh, by venue.

        Refreshing every discovered market does not work at trial scale: it is serial and
        rate-limited, Polymarket costs two requests per market, and a few thousand markets
        means hours per cycle — so every book is stale long before it is scanned. Scoping
        to the markets that belong to a pair we actually care about turns that into a
        handful of requests. It also keeps API usage narrowly tied to markets we intend to
        trade, which is what the Kalshi Developer Agreement §3 requires.

        Precedence: explicitly configured watched pairs, else the pairs currently held in
        state that a human has approved or is reviewing.
        """
        watched: dict[Venue, set[str]] = {Venue.KALSHI: set(), Venue.POLYMARKET: set()}

        configured = self.settings.trial.watched_pairs
        if configured:
            for entry in configured:
                watched[Venue.KALSHI].add(entry.kalshi_market_id)
                watched[Venue.POLYMARKET].add(entry.polymarket_market_id)
        else:
            for pair in self.state.pairs.values():
                if pair.status in _WATCHED_STATUSES:
                    watched[Venue.KALSHI].add(pair.kalshi_market_id)
                    watched[Venue.POLYMARKET].add(pair.polymarket_market_id)

        return {venue: sorted(ids) for venue, ids in watched.items()}

    async def refresh_books(
        self, market_ids: dict[Venue, list[str]] | None = None
    ) -> list[OrderBook]:
        """Fetch fresh snapshots for the watched markets.

        Pass ``market_ids`` to override the selection; omit it to refresh whatever
        :meth:`watched_markets` currently resolves to.
        """
        targets = market_ids if market_ids is not None else self.watched_markets()
        books: list[OrderBook] = []
        started = time.monotonic()
        requests = 0

        for venue, ids in targets.items():
            adapter = self.adapters.get(venue)
            if adapter is None:
                continue
            for market_id in ids:
                market = self.state.markets.get(self._key(venue, market_id))
                if market is not None and not market.is_tradable:
                    continue
                requests += 1
                try:
                    book = await adapter.fetch_orderbook(market_id)
                except Exception as exc:
                    log.warning(
                        "scanner.book_fetch_failed",
                        venue=venue.value,
                        market_id=market_id,
                        error=str(exc),
                    )
                    self.store.mark_stale(self._key(venue, market_id))
                    continue
                self.store.put(book)
                books.append(book)

        self.refresh_timing.record(time.monotonic() - started, requests)
        return books

    async def refresh_all_books(self) -> list[OrderBook]:
        """Refresh every discovered tradable market.

        Only safe for fixture data or a deliberately tiny universe. At live venue scale
        this is serial and rate-limited; use :meth:`refresh_books` instead.
        """
        targets: dict[Venue, list[str]] = {}
        for (venue, _product, market_id), market in self.state.markets.items():
            if market.is_tradable:
                targets.setdefault(venue, []).append(market_id)
        return await self.refresh_books(targets)

    # ------------------------------------------------------------------- matching

    def generate_pairs(self, min_score: Decimal = Decimal("0.5")) -> list[MarketPair]:
        """Generate candidate pairs. Nothing produced here is approved."""
        kalshi = [m for m in self.state.markets.values() if m.venue is Venue.KALSHI]
        polymarket = [m for m in self.state.markets.values() if m.venue is Venue.POLYMARKET]
        candidates = generate_candidates(kalshi, polymarket, min_score=min_score)
        for pair, _score, _comparison in candidates:
            # Never overwrite a pair a human has already dispositioned.
            existing = self.state.pairs.get(pair.id)
            if existing is not None and existing.status is not MatchStatus.SUGGESTED:
                continue
            self.state.pairs[pair.id] = pair
        return list(self.state.pairs.values())

    def seed_watched_pairs(self) -> list[MarketPair]:
        """Register explicitly configured watched pairs as pairs under review.

        A trial names its pairs up front rather than relying on candidate generation, so
        the pairs exist in state before any discovery has matched anything. They enter as
        ``under_review``: configuration selects what to watch, but only a human approval
        can make a pair tradable, and that gate is never bypassed by config.
        """
        seeded: list[MarketPair] = []
        for watched in self.settings.trial.watched_pairs:
            pair_id = pair_id_for(
                watched.kalshi_market_id,
                watched.polymarket_market_id,
                self.polymarket_product,
            )
            if pair_id in self.state.pairs:
                continue
            pair = MarketPair(
                id=pair_id,
                kalshi_market_id=watched.kalshi_market_id,
                polymarket_market_id=watched.polymarket_market_id,
                status=MatchStatus.UNDER_REVIEW,
                manually_approved=False,
                outcomes_inverted=watched.outcomes_inverted,
                polymarket_product=self.polymarket_product,
                reviewer_notes=watched.note or "seeded from trial.watched_pairs configuration",
            )
            self.state.pairs[pair_id] = pair
            seeded.append(pair)
        if seeded:
            log.info("scanner.watched_pairs_seeded", count=len(seeded))
        return seeded

    def approve(
        self,
        pair_id: str,
        approved_by: str,
        reviewer_notes: str = "",
        kalshi_reviewed_source: ReviewedSource | None = None,
        polymarket_reviewed_source: ReviewedSource | None = None,
    ) -> MarketPair:
        """Approve a pair. This is the only route to a 'confirmed' opportunity.

        Refuses unless the pair is ready: authoritative rules and a named settlement
        source for both legs, resolved product and outcome identity, known close times,
        and no difference that could settle the legs to opposite outcomes.

        Synchronous so that approval logic stays testable without a database. Use
        :meth:`approve_and_persist` when durability is required.
        """
        pair = self.state.pairs[pair_id]
        kalshi_k, polymarket_k = pair_market_keys(pair)
        kalshi = self.state.markets[kalshi_k]
        polymarket = self.state.markets[polymarket_k]
        approved = approve_pair(
            pair,
            kalshi,
            polymarket,
            approved_by,
            reviewer_notes,
            kalshi_reviewed_source=kalshi_reviewed_source,
            polymarket_reviewed_source=polymarket_reviewed_source,
        )
        self.state.pairs[pair_id] = approved
        return approved

    async def approve_and_persist(
        self,
        pair_id: str,
        approved_by: str,
        reviewer_notes: str = "",
        kalshi_reviewed_source: ReviewedSource | None = None,
        polymarket_reviewed_source: ReviewedSource | None = None,
    ) -> MarketPair:
        """Approve a pair and durably record the approval and its rule hashes."""
        approved = self.approve(
            pair_id,
            approved_by,
            reviewer_notes,
            kalshi_reviewed_source=kalshi_reviewed_source,
            polymarket_reviewed_source=polymarket_reviewed_source,
        )
        await self.persist_pairs()
        await self.audit(
            approved_by,
            "pair.approved",
            approved.id,
            {
                "kalshi_market_id": approved.kalshi_market_id,
                "polymarket_market_id": approved.polymarket_market_id,
                "kalshi_rules_hash": approved.kalshi_rules_hash_at_approval,
                "polymarket_rules_hash": approved.polymarket_rules_hash_at_approval,
                "kalshi_reviewed_source": (
                    kalshi_reviewed_source.canonical_id if kalshi_reviewed_source else None
                ),
                "polymarket_reviewed_source": (
                    polymarket_reviewed_source.canonical_id if polymarket_reviewed_source else None
                ),
                "reviewer_notes": reviewer_notes,
            },
        )
        return approved

    # ------------------------------------------------------------------ detection

    def scan(self, now: datetime | None = None) -> ScanResult:
        result = self.opportunities.scan(
            list(self.state.pairs.values()), self.state.markets, now=now
        )
        for suspended in result.suspended_pairs:
            self.state.pairs[suspended.id] = suspended
        self.portfolio.record_rejections(result.rejections)
        self.state.last_scan = result
        return result

    async def scan_and_persist(self, now: datetime | None = None) -> ScanResult:
        """Scan, then durably record the opportunities and any auto-suspensions."""
        result = self.scan(now=now)
        if self.database is None:
            return result

        async with self._db_lock, self.database.session() as session:
            opportunities = OpportunityRepository(session)
            for opportunity in result.opportunities:
                await opportunities.record(opportunity)

        if result.suspended_pairs:
            await self.persist_pairs()
            for pair in result.suspended_pairs:
                await self.audit(
                    "system",
                    "pair.suspended",
                    pair.id,
                    {"reason": "rules changed since approval"},
                )
        return result

    async def record_books(self, books: list[OrderBook]) -> None:
        """Persist book snapshots for replay and audit, within the retention window."""
        if self.database is None or not books:
            return
        async with self._db_lock, self.database.session() as session:
            repository = OrderBookSnapshotRepository(session)
            for book in books:
                await repository.record(book)

    # --------------------------------------------------------------- paper trading

    async def paper_trade(
        self, opportunity: Opportunity, simulate_latency: bool | None = None
    ) -> PaperTrade:
        """Simulate execution of one opportunity against current books.

        When ``simulate_latency`` is enabled the provider actually waits the configured
        network latency before reading the book. That matters: with a live feed the store
        keeps updating while we wait, so leg 2 genuinely arrives into a book that has
        moved. Reading the detection-time snapshot instead would make every fill look
        clean and would produce trial evidence that understates fill failure — the exact
        thing the trial exists to measure.

        It defaults to on whenever a continuous feed is running, and off for one-shot
        fixture runs where waiting would only slow the demo down.
        """
        wait = self.streaming if simulate_latency is None else simulate_latency

        async def provider(venue: Venue, market_id: str, latency_ms: int) -> OrderBook | None:
            if wait and latency_ms > 0:
                await asyncio.sleep(latency_ms / 1000.0)
            return self.store.get(self._key(venue, market_id))

        executor = PaperExecutor(
            self.settings.paper,
            provider,
            kalshi_direct_member=self.settings.kalshi.direct_member,
            freshness=self.store.policy,
        )
        kalshi = self.state.markets[kalshi_key(opportunity.kalshi_leg.market_id)]
        polymarket = self.state.markets[
            polymarket_key(self.polymarket_product, opportunity.polymarket_leg.market_id)
        ]
        trade = await executor.execute(opportunity, kalshi, polymarket)
        self.portfolio.record(trade)
        self.state.paper_trades.append(trade)

        if self.database is not None:
            async with self._db_lock, self.database.session() as session:
                await PaperTradeRepository(session).record(trade)
            await self.audit(
                "system",
                "paper_trade.executed",
                trade.id,
                {
                    "pair_id": trade.pair_id,
                    "status": trade.status.value,
                    "hedged": str(trade.hedged_quantity),
                    "unhedged": str(trade.unhedged_quantity),
                    "pnl": str(trade.realized_pnl_if_hedged),
                },
            )
        return trade

    # ------------------------------------------------------------------ persistence

    async def persist_markets(self) -> None:
        """Write current market metadata, including rules hashes."""
        if self.database is None:
            return
        async with self._db_lock, self.database.session() as session:
            await VenueMarketRepository(session).upsert_many(self.state.markets.values())

    async def persist_pairs(self) -> None:
        """Write pairs with their approval metadata and rule hashes."""
        if self.database is None:
            return
        async with self._db_lock, self.database.session() as session:
            await MarketPairRepository(session).save_many(self.state.pairs.values())

    async def audit(
        self, actor: str, action: str, subject: str = "", detail: dict[str, object] | None = None
    ) -> None:
        if self.database is None:
            return
        async with self._db_lock, self.database.session() as session:
            await AuditLogRepository(session).append(actor, action, subject, detail)

    async def restore(self) -> RestoreReport:
        """Load persisted state, re-verifying every approval against current rules.

        Markets are loaded first because approvals can only be validated against the rules
        text they were granted for. An approval whose rules have since changed — or whose
        market cannot be read at all — is suspended rather than reinstated.
        """
        if self.database is None:
            return RestoreReport()

        async with self._db_lock, self.database.session() as session:
            stored_markets = await VenueMarketRepository(session).load_all()
            stored_pairs = await MarketPairRepository(session).load_all()

        # Stored rows may predate a product switch, so drop anything for the other
        # product before merging: restoring it would let a Global market answer a US key.
        stored_markets = {
            key: market for key, market in stored_markets.items() if self._product_matches(market)
        }
        # Live metadata wins over stored metadata: discovery may already have run.
        merged_markets = {**stored_markets, **self.state.markets}
        self.state.markets = merged_markets

        # Only markets fetched from the venue during this run can verify an approval.
        pairs, report = restore_pairs(
            stored_pairs, merged_markets, observed=self.state.observed_markets
        )
        for pair_id, pair in pairs.items():
            self.state.pairs.setdefault(pair_id, pair)

        if report.suspended:
            await self.persist_pairs()
            for pair, reason in report.suspended:
                await self.audit("system", "pair.suspended_on_restore", pair.id, {"reason": reason})
        self.last_restore = report
        return report

    async def prune_snapshots(self) -> int:
        """Enforce bounded retention. Returns the number of snapshots removed."""
        if self.database is None:
            return 0
        window = timedelta(hours=self.settings.trial.snapshot_retention_hours)
        async with self._db_lock, self.database.session() as session:
            removed = await OrderBookSnapshotRepository(session).prune(window)
        if removed:
            log.info("scanner.snapshots_pruned", removed=removed)
        return removed

    # ----------------------------------------------------------------------- misc

    def health(self, now: datetime | None = None) -> HealthReport:
        return build_health_report(self.adapters, len(self.store), now=now or datetime.now(UTC))

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()
        if self.database is not None:
            await self.database.dispose()
