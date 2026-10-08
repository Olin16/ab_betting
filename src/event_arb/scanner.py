"""The scanner runtime: wires adapters, matching, detection, and paper trading together.

This is the object the CLI and the dashboard both drive. It holds the current view of the
world (markets, pairs, books) and exposes one-shot operations rather than owning a loop,
so a caller can scan once, scan on a timer, or scan on demand from an HTTP request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from event_arb.config import DataSource, IngestionMode, PolymarketProduct, Settings
from event_arb.domain.enums import MatchStatus, RejectionReason, Venue, VenueProduct
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
    RejectedOpportunity,
    ReviewedSource,
    VenueMarket,
)
from event_arb.matching.approval import approve_pair
from event_arb.matching.candidates import generate_candidates, pair_id_for
from event_arb.observability import metrics
from event_arb.observability.health import HealthReport, build_health_report
from event_arb.observability.logging import get_logger
from event_arb.opportunities.service import OpportunityService, ScanResult
from event_arb.orderbooks.book import BookStore
from event_arb.orderbooks.coverage import RefreshOutcome, has_executable_depth
from event_arb.orderbooks.freshness import FreshnessPolicy, RefreshTiming
from event_arb.paper.durability import (
    DurabilityStage,
    InvalidatedAttempt,
    TradeNotDurableError,
)
from event_arb.paper.evidence import ProvidedBook
from event_arb.paper.executor import (
    ExecutionInterruptedError,
    PaperExecutor,
    PaperTrade,
    SettlementOutcomes,
)
from event_arb.paper.journal import JournalHistory, RunJournal, journal_path_for
from event_arb.paper.ledger import LedgerEntry
from event_arb.paper.portfolio import (
    CapitalCheck as PaperCapitalCheck,
)
from event_arb.paper.portfolio import (
    PaperPortfolio,
    expected_accounts,
    opening_balances_from_settings,
)
from event_arb.paper.restore import (
    PortfolioRestoreReport,
    inspect_portfolio,
    restore_portfolio,
)
from event_arb.paper.settlement import SettlementError, SettlementRecord, apply_settlement
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import (
    AuditLogRepository,
    MarketPairRepository,
    OpportunityRepository,
    OrderBookSnapshotRepository,
    PaperLedgerRepository,
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

#: How many times a durable accounting write is attempted before the attempt it belongs
#: to is invalidated. Both writes are keyed by their own ids, so a retry converges on
#: a partial first attempt instead of duplicating it, and nothing is re-executed.
_DURABLE_WRITE_ATTEMPTS = 2


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


def _books_differ(before: OrderBook, after: OrderBook) -> bool:
    """Did the tradable content of a market actually move?

    Compares the price levels only. Receipt time and sequence numbers change on every poll
    even when the book is identical, so keying "changed" on those would grade a completely
    static market as a measured observation.
    """
    return (before.yes, before.no) != (after.yes, after.no)


def _reservation_id(opportunity: Opportunity) -> str:
    """A reservation id that is stable for one attempt at one opportunity.

    Keyed on the detection instant as well as the pair and direction, so consecutive
    attempts at the same recurring opportunity each get their own hold rather than the
    second silently reusing the first's.
    """
    return f"hold:{opportunity.dedup_key}:{opportunity.detected_at.isoformat()}"


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
    #: Attempts storage refused, which are therefore in neither ``paper_trades`` nor the
    #: portfolio. A run that ends with any of these has not produced complete evidence,
    #: however clean the attempts that did land look.
    invalidated_attempts: list[InvalidatedAttempt] = field(default_factory=list)


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
        #: Durable failure history, kept beside the database rather than inside it —
        #: an attempt is invalidated *because* the database refused it, so recording
        #: that there would depend on the subsystem that just failed. See
        #: :mod:`event_arb.paper.journal`.
        journal_path = journal_path_for(settings.database_url) if database is not None else None
        self.journal = RunJournal(journal_path) if journal_path is not None else None
        #: What earlier runs left behind, read once at restore. Empty until then.
        self.journal_history = JournalHistory()
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
        paper = settings.paper
        self.portfolio = PaperPortfolio(
            paper.starting_usd_balance,
            paper.starting_usdc_balance,
            opening_balances=opening_balances_from_settings(
                expected_accounts(self.polymarket_product), paper
            ),
            max_trade_notional=paper.max_trade_notional,
            max_open_notional=paper.max_open_notional,
            max_open_unhedged_quantity=paper.max_open_unhedged_quantity,
            min_account_balance=paper.min_account_balance,
        )
        self.last_restore: RestoreReport | None = None
        #: What the last portfolio restore recovered. ``None`` until one has run;
        #: the trial preflight refuses to trade while it is incomplete.
        self.last_portfolio_restore: PortfolioRestoreReport | None = None
        #: True while a continuous feed is running; enables latency-aware paper fills.
        self.streaming: bool = False
        #: Serializes database writes. The trial's refresh, scan, execute and maintenance
        #: loops all persist concurrently; without this their sessions interleave on the
        #: same rows. Every acquisition wraps exactly one session block and never nests,
        #: so it cannot deadlock.
        self._db_lock = asyncio.Lock()
        #: Serializes the capital check and the reservation that follows it. Both are
        #: synchronous, but the ``await`` between checking and executing is exactly where
        #: a second coroutine used to slip in and pass the same check against the same
        #: cash. Holding this across check-and-reserve makes the pair atomic; execution
        #: itself runs outside it, so trades still overlap.
        self._capital_lock = asyncio.Lock()

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
    ) -> RefreshOutcome:
        """Fetch fresh snapshots for the watched markets, reporting what was covered.

        Pass ``market_ids`` to override the selection; omit it to refresh whatever
        :meth:`watched_markets` currently resolves to.

        Individual failures are still contained here — one unreachable market must not
        abandon the rest of the batch — but containment is no longer the same as silence.
        The returned :class:`RefreshOutcome` names every required market that did *not*
        end up with a usable book, so the runtime's failure streak, the preflight gate and
        the CLI exit code can all tell a dead feed from a quiet market. Before this, they
        could not: every one of them inferred health from this method returning normally,
        which it does even when every single request raised.
        """
        targets = market_ids if market_ids is not None else self.watched_markets()
        outcome = RefreshOutcome()
        required: list[MarketKey] = []
        started = time.monotonic()

        for venue, ids in targets.items():
            adapter = self.adapters.get(venue)
            for market_id in ids:
                key = self._key(venue, market_id)
                required.append(key)
                if adapter is None:
                    outcome.skipped[key] = "no adapter configured for this venue"
                    continue
                market = self.state.markets.get(key)
                if market is not None and not market.is_tradable:
                    outcome.skipped[key] = "market is not tradable"
                    continue
                outcome.requests += 1
                try:
                    book = await adapter.fetch_orderbook(market_id)
                except Exception as exc:
                    log.warning(
                        "scanner.book_fetch_failed",
                        venue=venue.value,
                        market_id=market_id,
                        error=str(exc),
                    )
                    self.store.mark_stale(key)
                    outcome.failed[key] = str(exc)
                    continue
                if not has_executable_depth(book):
                    # Stored anyway: the empty book is the current truth about this
                    # market, and hiding it would leave the previous, better-looking
                    # snapshot in place to be priced as if it were current. It is marked
                    # stale so nothing can trade on it, and excluded from ``books`` so it
                    # is not counted as coverage.
                    self.store.put(book)
                    self.store.mark_stale(key)
                    log.warning(
                        "scanner.book_unusable",
                        venue=venue.value,
                        market_id=market_id,
                        note="no asks on either outcome; nothing can be priced from it",
                    )
                    outcome.unusable[key] = "no executable depth on either outcome"
                    continue
                self.store.put(book)
                outcome.books.append(book)

        outcome.required = tuple(required)
        self.refresh_timing.record(time.monotonic() - started, outcome.requests)
        return outcome

    async def refresh_all_books(self) -> RefreshOutcome:
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
            # Suppressed detections are still detections. Persisting only the announced
            # ones let the alert cooldown decide what the trial had observed.
            for opportunity in result.alert_suppressed:
                await opportunities.record(opportunity, alert_suppressed=True)

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
        """Simulate execution of one opportunity against the book at each leg's arrival.

        The provider waits the configured latency and then goes back for the book, so leg 2
        prices against whatever is there *after* leg 1 — not against the detection-time
        snapshot, which would make every fill look clean and produce trial evidence that
        understates hedge failure.

        How it goes back depends on the ingestion mode:

        - **Streaming**: the store is being updated continuously, so re-reading it is a
          genuine re-observation and costs no extra request.
        - **REST polling**: nothing updates the store between legs, so the provider issues
          a fresh ``fetch_orderbook`` through the venue's rate limiter. That is **two extra
          requests per simulated trade** beyond scan traffic; they are counted in
          ``event_arb_paper_refetches_total`` and budgeted in docs/operations.md.

        Every trade records how much its leg-2 book is worth as evidence. A re-fetch that
        returns an unchanged book is graded ``unchanged_reread``, not ``measured``: it
        shows the book did not move in that sample, which is not the same claim as hedging
        surviving adverse movement. Fixture replay is always ``unchanged_reread`` — the
        file cannot move — unless an adverse-movement model is explicitly configured, in
        which case it is graded ``modelled``.
        """
        wait = self.streaming if simulate_latency is None else simulate_latency
        # In REST mode the store is static between legs, so re-reading it is not a
        # re-observation. Ask the venue again instead, through the same rate limiter.
        refetch = (
            self.settings.ingestion_mode is IngestionMode.REST_POLLING
            and self.settings.data_source is not DataSource.FIXTURE
        )

        # The books as they stood when the opportunity was detected. "Changed" means
        # changed relative to what was priced, not relative to the previous leg.
        baseline = self.store.snapshot()

        async def provider(venue: Venue, market_id: str, latency_ms: int) -> ProvidedBook:
            if wait and latency_ms > 0:
                await asyncio.sleep(latency_ms / 1000.0)
            key = self._key(venue, market_id)
            held = self.store.get(key)

            if not refetch:
                # Streaming: the store moved under us if the feed delivered anything, so a
                # re-read is a real observation. Fixtures: it never moves, and saying so is
                # the point — the grade must not claim a measurement that did not happen.
                prior = baseline.get(key)
                return ProvidedBook(
                    book=held,
                    refetched=self.streaming,
                    changed=held is not None and prior is not None and _books_differ(prior, held),
                )

            adapter = self.adapters.get(venue)
            if adapter is None:
                return ProvidedBook(book=held, refetched=False, changed=False)
            prior = baseline.get(key)
            try:
                fresh = await adapter.fetch_orderbook(market_id)
            except Exception as exc:
                # A failed re-fetch is a real leg-2 failure, not a reason to fall back to
                # a stale snapshot and record a clean fill.
                log.warning(
                    "paper.refetch_failed",
                    venue=venue.value,
                    market_id=market_id,
                    error=str(exc),
                )
                return ProvidedBook(book=None, refetched=True, changed=False)
            metrics.paper_refetches.labels(venue=venue.value).inc()
            self.store.put(fresh)
            return ProvidedBook(
                book=fresh,
                refetched=True,
                changed=prior is not None and _books_differ(prior, fresh),
            )

        executor = PaperExecutor(
            self.settings.paper,
            provider,
            kalshi_direct_member=self.settings.kalshi.direct_member,
            freshness=self.store.policy,
            checkpoint=self._checkpoint_leg1,
        )
        kalshi = self.state.markets[kalshi_key(opportunity.kalshi_leg.market_id)]
        polymarket = self.state.markets[
            polymarket_key(self.polymarket_product, opportunity.polymarket_leg.market_id)
        ]
        try:
            trade = await executor.execute(opportunity, kalshi, polymarket)
        except ExecutionInterruptedError as interrupted:
            # Leg 1 filled. Make the exposure durable before letting the interruption
            # continue, so a restart rebuilds a position that genuinely exists rather
            # than one the crash quietly deleted. The status says the attempt is
            # incomplete, so it is carried as exposure without counting as evidence
            # about hedging.
            await self._publish_interrupted(interrupted)
            if isinstance(interrupted.cause, TradeNotDurableError):
                # Storage refused the attempt before leg 2; `execute` wrapped that refusal
                # on its way out because it looks like any other interruption from there.
                # The caller needs the refusal itself: nothing was published, and the
                # attempt is already recorded as invalidated.
                raise interrupted.cause from None
            # Cancellation is not an execution failure. `execute` wraps it so the partial
            # exposure reaches a caller that can store it; now that it has been stored,
            # re-raising the wrapper instead of the cancellation would turn an orderly
            # operator shutdown into a trial error, complete with a consecutive-failure
            # count and a non-zero exit the operator did not cause.
            if isinstance(interrupted.cause, asyncio.CancelledError):
                raise interrupted.cause from None
            raise

        entries = self.portfolio.plan_record(trade)
        if self.database is not None:
            # Stored first, published second. A trade whose cash landed but whose row did
            # not — or the reverse, or neither, while memory holds both — is a portfolio
            # that cannot be reconstructed, and reconstruction is the only thing standing
            # between a restart and an invented balance. One transaction covers the two
            # writes; publishing only after it commits covers memory.
            await self._store_trade(trade, entries, DurabilityStage.TRADE)

        self._publish(trade, entries)

        if self.database is not None:
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
                    "accounts": {e.account.key: str(e.amount) for e in entries},
                },
            )
        return trade

    def _publish(self, trade: PaperTrade, entries: list[LedgerEntry]) -> None:
        """Make a stored attempt visible to the running process. Call only after a commit.

        Replaces rather than duplicates, because a leg-1 checkpoint has already published
        a snapshot under the same id and the finished attempt supersedes it.
        """
        self.portfolio.commit_record(trade, entries)
        superseded = [t for t in self.state.paper_trades if t.id != trade.id]
        superseded.append(trade)
        self.state.paper_trades = superseded

    async def _store_trade(
        self,
        trade: PaperTrade,
        entries: list[LedgerEntry],
        stage: DurabilityStage,
    ) -> None:
        """Write a trade row and its cash entries in one transaction, or refuse the trade.

        Retried once, because the failures this guards against are overwhelmingly
        transient — a locked SQLite file, a dropped connection — and both writes are
        keyed by their own ids, so a retry that lands after a partial first attempt
        converges rather than duplicating. Nothing is re-executed: the trade is already
        decided and this is purely its storage.

        Raises :class:`TradeNotDurableError` when it cannot be stored. The caller must not
        publish the attempt to memory, which is what keeps a restart's view equal to the
        running process's.
        """
        assert self.database is not None
        last: BaseException | None = None
        for attempt in range(_DURABLE_WRITE_ATTEMPTS):
            try:
                async with self._db_lock, self.database.session() as session:
                    await PaperTradeRepository(session).record(trade)
                    await PaperLedgerRepository(session).record_many(entries)
                return
            except asyncio.CancelledError:
                # A cancelled write is the operator stopping the process, not storage
                # failing. Retrying would ignore the stop; it propagates as itself.
                raise
            except Exception as exc:
                last = exc
                log.warning(
                    "paper.trade.write_failed",
                    trade_id=trade.id,
                    stage=stage.value,
                    attempt=attempt + 1,
                    of=_DURABLE_WRITE_ATTEMPTS,
                    error=str(exc),
                )
        assert last is not None
        raise TradeNotDurableError(self._invalidate(trade, stage, last), last)

    def _invalidate(
        self, trade: PaperTrade, stage: DurabilityStage, cause: BaseException
    ) -> InvalidatedAttempt:
        """Record an attempt that was not stored, and therefore was never published."""
        record = InvalidatedAttempt(
            trade_id=trade.id,
            pair_id=trade.pair_id,
            stage=stage,
            execution_status=trade.execution_status.value,
            unhedged_quantity=trade.unhedged_quantity,
            error=str(cause),
        )
        self.state.invalidated_attempts.append(record)
        self.portfolio.record_invalidation(record)
        metrics.paper_trades_invalidated.labels(stage=stage.value).inc()
        if self.journal is not None:
            # The database is the thing that just refused this attempt, so the record of
            # the refusal goes somewhere else. Without this the invalidation lived only in
            # process memory: a restart read zero invalidations off the same database and
            # upgraded the run's evidence from incomplete to complete.
            self.journal.attempt_invalidated(record)
        return record

    async def _checkpoint_leg1(self, trade: PaperTrade) -> None:
        """Make leg 1's exposure durable before leg 2 is attempted.

        Without this, an *abrupt* death between the legs — a signal the process never
        handles, a host going away — leaves nothing at all: the previous design only
        recorded the exposure if the process survived long enough to catch an exception,
        which is a recovery path that assumes the failure it is recovering from did not
        happen. Writing here means the exposure is on disk before the window opens.

        In a paper trial no venue holds the other side, so an attempt lost before this
        point genuinely has no consequence — memory dies with the process and storage
        never heard of it. That symmetry is the point: after this point the two agree
        because the write came first, and before it they agree because neither has
        anything. Refusing to proceed when the write fails is what preserves it.
        """
        if self.database is None:
            return
        entries = self.portfolio.plan_record(trade)
        await self._store_trade(trade, entries, DurabilityStage.CHECKPOINT)
        # A snapshot, not the live trade. `execute` keeps mutating that object through
        # leg 2, so publishing it here would let a later, *unstored* leg-2 fill appear in
        # the portfolio by aliasing — the exact divergence this ordering exists to stop.
        # Round-tripping through the stored record makes the published trade equal, field
        # for field, to what a restart would read.
        self._publish(PaperTrade.from_record(trade.as_dict(), trade.opportunity), entries)
        log.info(
            "paper.trade.leg1_checkpointed",
            trade_id=trade.id,
            filled=str(trade.leg1.filled_quantity) if trade.leg1 else "0",
            note="leg-1 exposure is durable; an abrupt stop before leg 2 restores it",
        )

    async def _publish_interrupted(self, interrupted: ExecutionInterruptedError) -> None:
        """Carry an interrupted attempt's leg-1 exposure, durably or not at all.

        Three cases, and they are deliberately different:

        - The checkpoint already stored this attempt: nothing to do but note the status
          change, which is the ordinary path now that the checkpoint runs before leg 2.
        - The checkpoint is the thing that failed: the attempt is already invalidated and
          was never published, so re-attempting the same write here would only repeat it.
        - No database at all: memory is the only store, so publishing to it is durable by
          definition.

        Never raises. This runs while an interruption — usually a cancellation during
        shutdown — is already in flight, and replacing it with a storage error would
        rewrite an orderly stop as a crash.
        """
        trade = interrupted.trade
        if isinstance(interrupted.cause, TradeNotDurableError):
            # The checkpoint refused it. It was not published and must not be now.
            return

        if self.database is None:
            self._publish(trade, self.portfolio.plan_record(trade))
            return

        entries = self.portfolio.plan_record(trade)
        try:
            await self._store_trade(trade, entries, DurabilityStage.INTERRUPTED)
        except TradeNotDurableError:
            # Already recorded as invalidated by `_store_trade`. Deliberately not
            # published: memory claiming exposure that no restart can read is the
            # divergence this whole path exists to prevent.
            return
        except asyncio.CancelledError:
            self._invalidate(
                trade,
                DurabilityStage.INTERRUPTED,
                asyncio.CancelledError("cancelled while storing interrupted attempt"),
            )
            return

        self._publish(trade, entries)
        log.warning(
            "paper.trade.interrupted_recorded",
            trade_id=trade.id,
            unhedged=str(trade.unhedged_quantity),
            note="incomplete attempt; carried as open exposure and excluded from "
            "hedge-quality statistics",
        )

    async def paper_trade_if_funded(
        self, opportunity: Opportunity, simulate_latency: bool | None = None
    ) -> PaperTrade | None:
        """Paper-trade an opportunity, but only if the simulated accounts can fund it.

        This is the entry point automated trading uses. :meth:`paper_trade` deliberately
        does not check capital itself, so a one-shot ``event-arb paper`` demonstration and
        the unit tests can still exercise fill behaviour in isolation — but nothing that
        runs unattended may reach it directly, because an unbounded simulator accumulates
        exposure no operator would have taken and reports a strategy nobody ran.

        Returns ``None`` when the trade was refused, having recorded the reason.
        """
        funding = self.portfolio.funding_for(opportunity)
        async with self._capital_lock:
            check = self.portfolio.check_capital(opportunity)
            if check.allowed:
                reservation = self.portfolio.reserve(_reservation_id(opportunity), funding)
            else:
                reservation = []
        if not check.allowed:
            await self._record_capital_refusal(opportunity, check)
            return None

        try:
            # Inside the try, so the cleanup below covers it. Persisting the hold used to
            # sit above this line: a failure there skipped the `finally` entirely and the
            # in-memory hold was never released, reserving that cash against a trade that
            # had not even been attempted for as long as the process lived.
            if self.database is not None and reservation:
                async with self._db_lock, self.database.session() as session:
                    await PaperLedgerRepository(session).record_many(reservation)
            return await self.paper_trade(opportunity, simulate_latency=simulate_latency)
        finally:
            # A crash mid-execution must not leave the cash held forever. The hold is
            # keyed to the opportunity, not to the trade id ``record`` knows about, so
            # releasing it is this function's job on both the success and the failure path.
            released = self.portfolio.release(_reservation_id(opportunity))
            if released and self.database is not None:
                try:
                    async with self._db_lock, self.database.session() as session:
                        await PaperLedgerRepository(session).record_many(released)
                except Exception as exc:
                    # Cleanup must never replace what it is unwinding from. This block
                    # runs while a cancellation is in flight during shutdown, and letting
                    # its failure escape rewrote an orderly stop as a trial error —
                    # complete with a spurious consecutive-failure count in the final
                    # report. The cash is already released in memory; a restart replays
                    # the ledger, and any hold this failed to release is reaped there.
                    log.error(
                        "paper.reservation_release_unrecorded",
                        reservation=_reservation_id(opportunity),
                        error=str(exc),
                        note="hold released in memory but not on disk; "
                        "restore releases it on the next start",
                    )

    async def _record_capital_refusal(
        self, opportunity: Opportunity, check: PaperCapitalCheck
    ) -> None:
        """Record a refused trade so the gap in the evidence has a stated cause.

        A trial that simply stops trading is indistinguishable from a market with no
        opportunities. The rejection lands in the daily report's ``missed_opportunities``
        and, when a database is attached, in the append-only audit log — the durable copy,
        since no trade row exists to hang it on.
        """
        reason = (
            RejectionReason.INSUFFICIENT_PAPER_FUNDS
            if check.shortfalls
            else RejectionReason.PAPER_LIMIT_EXCEEDED
        )
        rejection = RejectedOpportunity(
            pair_id=opportunity.pair_id,
            direction=opportunity.direction,
            reason=reason,
            detail=check.reason,
        )
        self.portfolio.record_rejections([rejection])
        metrics.paper_trades_refused.labels(reason=reason.value).inc()
        log.warning(
            "paper.trade_refused",
            pair_id=opportunity.pair_id,
            reason=reason.value,
            detail=check.reason,
            shortfalls=check.shortfalls,
        )
        await self.audit(
            "system",
            "paper_trade.refused",
            opportunity.pair_id,
            {
                "reason": reason.value,
                "detail": check.reason,
                "shortfalls": check.shortfalls,
                "direction": opportunity.direction.value,
                "quantity": str(opportunity.executable_quantity),
            },
        )

    # ------------------------------------------------------------------- settlement

    async def settle_paper_trade(
        self,
        trade_id: str,
        outcomes: SettlementOutcomes,
        settled_at: datetime | None = None,
    ) -> SettlementRecord:
        """Record one trade's settlement, credit its payouts, and persist both.

        The trade must already be in the portfolio, which means a restart has to restore
        before settling — that is deliberate. Settling against a portfolio that was not
        restored would credit a payout into freshly-invented starting balances.

        The credit and the updated trade row are written in one transaction. Idempotent at
        every level: an already-settled trade is returned unchanged, and the ledger's
        deterministic entry ids make a repeated write a no-op rather than a second credit.
        """
        trade = next((t for t in self.portfolio.trades if t.id == trade_id), None)
        if trade is None:
            raise SettlementError(
                f"no paper trade {trade_id!r} in the portfolio. Restore first, or check "
                "the id against 'event-arb settle --list'."
            )
        record = apply_settlement(self.portfolio, trade, outcomes, settled_at)
        if self.database is not None:
            try:
                async with self._db_lock, self.database.session() as session:
                    await PaperTradeRepository(session).record(trade)
                    await PaperLedgerRepository(session).record_many(record.entries)
            except Exception as exc:
                # Settlement is the one place memory legitimately runs ahead of storage:
                # the credit is idempotent and `credit_settlement` hands back the full
                # entry set precisely so a repeat converges. But until that repeat lands,
                # the run is reporting cash a restart would not find, and the caller — an
                # operator at a command line — needs the failure itself. Recording the
                # divergence makes it something the daily report states rather than
                # something only this traceback knew.
                self._invalidate(trade, DurabilityStage.SETTLEMENT, exc)
                raise
            if self.portfolio.resolve_invalidation(trade.id) and self.journal is not None:
                self.journal.attempt_resolved(
                    trade.id, note="settlement written after an earlier attempt was refused"
                )
            self.state.invalidated_attempts = [
                a for a in self.state.invalidated_attempts if a.trade_id != trade.id
            ]
            await self.audit(
                outcomes.recorded_by or "operator",
                "paper_trade.settled",
                trade.id,
                {
                    "pair_id": trade.pair_id,
                    "kalshi_winning_outcome": outcomes.kalshi.value,
                    "polymarket_winning_outcome": outcomes.polymarket.value,
                    "evidence": outcomes.evidence,
                    "credited": str(record.credited),
                    "final_pnl": str(record.final_pnl),
                    "hedge_held": record.hedge_held,
                    "already_settled": record.already_settled,
                },
            )
        return record

    def unsettled_trades(self) -> list[PaperTrade]:
        """Trades with no recorded settlement, oldest first."""
        return [t for t in self.portfolio.trades if not t.is_settled]

    # ------------------------------------------------------------------ persistence

    async def persist_markets(self) -> None:
        """Write current market metadata, including rules hashes."""
        if self.database is None:
            return
        async with self._db_lock, self.database.session() as session:
            await VenueMarketRepository(session).upsert_many(self.state.markets.values())

    async def persist_markets_subset(self, markets: Iterable[VenueMarket]) -> int:
        """Write metadata for a named subset of markets. Returns how many were written.

        Candidate research holds a whole catalogue page in memory but must persist only the
        legs of the shortlist it kept. Storing the full page would exceed what §3.1 of the
        Kalshi Developer Agreement permits (data stored to facilitate our own trading), and
        storing nothing left the dashboard unable to render a persisted candidate at all.
        """
        if self.database is None:
            return 0
        selected = list(markets)
        if not selected:
            return 0
        async with self._db_lock, self.database.session() as session:
            await VenueMarketRepository(session).upsert_many(selected)
        return len(selected)

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

    async def load_dispositions(self) -> dict[str, MarketPair]:
        """Read stored pairs **verbatim**, validating and mutating nothing.

        :meth:`restore` is the wrong tool for candidate research. It re-verifies every
        approval against current rules, and an approval can only survive that check when
        both legs were fetched from the venue *this run*. Candidate research fetches a
        bounded catalogue page, not the specific markets an old approval points at, so
        restore saw them as unobserved and suspended every approved pair — turning a
        read-only research command into one that silently revoked human decisions.

        This returns a detached copy. Nothing here is written back, so a research run can
        consult what a human already decided without being able to change it.
        """
        if self.database is None:
            return {}
        async with self._db_lock, self.database.session() as session:
            return await MarketPairRepository(session).load_all()

    async def persist_new_pairs(self, pairs: Iterable[MarketPair]) -> int:
        """Write only pairs that do not already exist. Returns how many were written.

        Deliberately not :meth:`persist_pairs`, which rewrites everything in state. A
        research run must be incapable of overwriting a stored disposition, so existence is
        checked against storage inside the same transaction rather than against memory.
        """
        if self.database is None:
            return 0
        written = 0
        async with self._db_lock, self.database.session() as session:
            repository = MarketPairRepository(session)
            stored = set(await repository.load_ids())
            for pair in pairs:
                if pair.id in stored:
                    continue
                await repository.save(pair)
                written += 1
        return written

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

        await self.restore_portfolio()
        self.last_restore = report
        return report

    async def stored_pair(self, pair_id: str) -> MarketPair | None:
        """Read one stored pair as it was recorded, or ``None`` if it is not there.

        Deliberately not :meth:`restore`, which re-verifies every approval against rules
        this process has not fetched and would therefore suspend them all. Settlement does
        not need a pair to be currently approved; it needs the reviewer's recorded
        ``outcomes_inverted`` statement, which is a fact about what a human decided and
        does not expire.
        """
        if self.database is None:
            return None
        async with self._db_lock, self.database.session() as session:
            stored = await MarketPairRepository(session).load_all()
        return stored.get(pair_id)

    async def inspect_portfolio(self) -> PortfolioRestoreReport:
        """Read the stored portfolio without changing a single row.

        The counterpart to :meth:`restore_portfolio`, for commands that report rather than
        recover. Nothing here writes: no hold is reaped, no ledger entry is persisted, and
        the journal is read but not appended to. That matters most when another process
        owns the database — a report must never release the reservations a running trial
        is holding.
        """
        report = PortfolioRestoreReport()
        if self.database is None:
            return report
        self._replay_journal()
        async with self._db_lock, self.database.session() as session:
            entries = await PaperLedgerRepository(session).load_all()
            rows = list(await PaperTradeRepository(session).load_all())
        report = inspect_portfolio(self.portfolio, entries, rows)
        self.state.paper_trades = list(self.portfolio.trades)
        self.last_portfolio_restore = report
        return report

    async def restore_portfolio(self) -> PortfolioRestoreReport:
        """Rebuild simulated cash, positions and history from storage.

        Restoring approvals but not the portfolio meant every restart handed the trial its
        configured starting balances back while the trades that had spent them stayed on
        disk. Cash is replayed from the ledger and history from the trade rows; both are
        idempotent, so calling this twice changes nothing.
        """
        report = PortfolioRestoreReport()
        if self.database is None:
            return report
        self._replay_journal()
        async with self._db_lock, self.database.session() as session:
            entries = await PaperLedgerRepository(session).load_all()
            rows = list(await PaperTradeRepository(session).load_all())
        report = restore_portfolio(self.portfolio, entries, rows)
        if report.stale_holds_released:
            # Persist the reaping, or every subsequent start re-reads the same dead hold
            # and the stored ledger never stops disagreeing with the running one.
            async with self._db_lock, self.database.session() as session:
                await PaperLedgerRepository(session).record_many(report.stale_holds_released)
        self.state.paper_trades = list(self.portfolio.trades)
        self.last_portfolio_restore = report
        if not report.is_complete:
            log.error(
                "paper.restore.incomplete",
                unreadable=report.trades_unreadable,
                note="simulated exposure may be understated; do not trade against this",
            )
            await self.audit(
                "system",
                "paper_portfolio.restore_incomplete",
                "",
                {"unreadable": report.trades_unreadable},
            )
        return report

    async def prune_snapshots(self) -> int:
        """Enforce bounded retention across every table that grows with time.

        Only order-book snapshots used to be pruned. Detections and paper trades grew
        without limit, which a multi-week trial does not survive: at a 5s cycle with two
        detections per cycle the opportunities table projects to ~970,000 rows per 28 days.

        **Retention never changes a balance and never drops a live position.** Trade rows
        are prunable only once settled with no unhedged remainder, so an old leg-2 failure
        that is still open stays in the exposure report however long it has been open. The
        ledger is *compacted* rather than deleted: aged entries are replaced by one
        carry-forward per account, which bounds the table while leaving every derived
        balance identical. Entries belonging to still-open trades are left alone.

        Returns the total number of rows removed.
        """
        if self.database is None:
            return 0
        trial = self.settings.trial
        keep = self.portfolio.unresolved_trade_ids
        async with self._db_lock, self.database.session() as session:
            snapshots = await OrderBookSnapshotRepository(session).prune(
                timedelta(hours=trial.snapshot_retention_hours)
            )
            opportunities = await OpportunityRepository(session).prune(
                timedelta(days=trial.opportunity_retention_days)
            )
            trades = await PaperTradeRepository(session).prune(
                timedelta(days=trial.paper_trade_retention_days)
            )
            ledger = await PaperLedgerRepository(session).compact(
                timedelta(days=trial.paper_trade_retention_days), keep
            )
        if ledger:
            # Mirror the compaction in memory so the running process and storage agree on
            # what the ledger contains without a restart.
            self.portfolio.ledger.compact_before(
                datetime.now(UTC) - timedelta(days=trial.paper_trade_retention_days), keep
            )
        removed = snapshots + opportunities + trades + ledger
        if removed:
            log.info(
                "scanner.retention_pruned",
                snapshots=snapshots,
                opportunities=opportunities,
                paper_trades=trades,
                ledger_entries_compacted=ledger,
                balances=self.portfolio.balances,
            )
        return removed

    # ----------------------------------------------------------------------- misc

    # --------------------------------------------------------------- run journal

    def _replay_journal(self) -> None:
        """Carry an earlier run's failures into this one's reporting.

        Idempotent: restoring twice re-reads the same file and the portfolio de-duplicates
        by trade id, so a second restore cannot double-count an invalidation.
        """
        if self.journal is None:
            return
        history = RunJournal.read(self.journal.path)
        self.journal_history = history
        known = {a.trade_id for a in self.state.invalidated_attempts}
        for attempt in history.invalidated:
            if attempt.trade_id in known:
                continue
            self.state.invalidated_attempts.append(attempt)
            self.portfolio.record_invalidation(attempt)
        self.portfolio.record_unfinished_runs([run.run_id for run in history.unfinished])
        if not history.is_clean:
            log.error(
                "paper.journal.prior_run_incomplete",
                note="this database carries evidence gaps from an earlier run; they are "
                "counted against this run's completeness until acknowledged",
                **history.as_dict(),
            )

    def begin_run(self, **detail: object) -> str | None:
        """Open a run in the journal, returning its id.

        The open record is what makes an *abrupt* death visible. Storage looks identical
        whether a process shut down cleanly or was killed between two writes, so without a
        marker that only a clean shutdown closes, a crash reads as a completed run.
        """
        if self.journal is None:
            return None
        self.journal.run_started(**detail)
        return self.journal.run_id

    def finish_run(self, **detail: object) -> None:
        """Close this run in the journal. Only reached by an orderly shutdown."""
        if self.journal is not None:
            self.journal.run_finished(**detail)

    def acknowledge_run(self, run_id: str, *, by: str, note: str) -> bool:
        """Record that an operator reviewed an earlier unfinished run.

        Appends; the crash record itself is never removed. This is the only way the
        evidence gate stops blocking on it, and it leaves a named person and a reason in
        the file rather than a gap where the failure used to be.
        """
        if self.journal is None:
            return False
        return self.journal.acknowledge(run_id, by=by, note=note)

    def health(self, now: datetime | None = None) -> HealthReport:
        return build_health_report(self.adapters, len(self.store), now=now or datetime.now(UTC))

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()
        if self.database is not None:
            await self.database.dispose()
