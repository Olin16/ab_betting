"""The scanner runtime: wires adapters, matching, detection, and paper trading together.

This is the object the CLI and the dashboard both drive. It holds the current view of the
world (markets, pairs, books) and exposes one-shot operations rather than owning a loop,
so a caller can scan once, scan on a timer, or scan on demand from an HTTP request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.config import DataSource, Settings
from event_arb.domain.enums import MatchStatus, Venue
from event_arb.domain.models import MarketPair, Opportunity, OrderBook, VenueMarket
from event_arb.matching.approval import approve_pair
from event_arb.matching.candidates import generate_candidates, pair_id_for
from event_arb.observability.health import HealthReport, build_health_report
from event_arb.observability.logging import get_logger
from event_arb.opportunities.service import OpportunityService, ScanResult
from event_arb.orderbooks.book import BookStore
from event_arb.orderbooks.freshness import FreshnessPolicy
from event_arb.paper.executor import PaperExecutor, PaperTrade
from event_arb.paper.portfolio import PaperPortfolio
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
        return {
            Venue.KALSHI: FixtureAdapter(Venue.KALSHI, settings.fixture_dir),
            Venue.POLYMARKET: FixtureAdapter(Venue.POLYMARKET, settings.fixture_dir),
        }

    from event_arb.venues.kalshi.client import KalshiClient
    from event_arb.venues.polymarket.client import PolymarketClient

    log.warning(
        "scanner.live_market_data",
        note="read-only live market data; order submission remains impossible",
    )
    return {
        Venue.KALSHI: KalshiClient(settings.kalshi),
        Venue.POLYMARKET: PolymarketClient(settings.polymarket),
    }


@dataclass
class ScannerState:
    markets: dict[tuple[Venue, str], VenueMarket] = field(default_factory=dict)
    pairs: dict[str, MarketPair] = field(default_factory=dict)
    last_scan: ScanResult | None = None
    paper_trades: list[PaperTrade] = field(default_factory=list)


class Scanner:
    """Coordinates discovery, matching, detection, and paper execution."""

    def __init__(
        self, settings: Settings, adapters: dict[Venue, VenueAdapter] | None = None
    ) -> None:
        self.settings = settings
        self.adapters = adapters or build_adapters(settings)
        self.store = BookStore(FreshnessPolicy(max_age_ms=settings.opportunity.max_book_age_ms))
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

    # ------------------------------------------------------------------ discovery

    async def discover(self) -> list[VenueMarket]:
        """Load markets from every adapter into scanner state."""
        discovered: list[VenueMarket] = []
        for venue, adapter in self.adapters.items():
            try:
                markets = await adapter.discover_markets()
            except Exception as exc:
                log.error("scanner.discovery_failed", venue=venue.value, error=str(exc))
                continue
            for market in markets:
                self.state.markets[(venue, market.venue_market_id)] = market
            discovered.extend(markets)
        log.info("scanner.discovered", count=len(discovered))
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

        for venue, ids in targets.items():
            adapter = self.adapters.get(venue)
            if adapter is None:
                continue
            for market_id in ids:
                market = self.state.markets.get((venue, market_id))
                if market is not None and not market.is_tradable:
                    continue
                try:
                    book = await adapter.fetch_orderbook(market_id)
                except Exception as exc:
                    log.warning(
                        "scanner.book_fetch_failed",
                        venue=venue.value,
                        market_id=market_id,
                        error=str(exc),
                    )
                    self.store.mark_stale(venue, market_id)
                    continue
                self.store.put(book)
                books.append(book)
        return books

    async def refresh_all_books(self) -> list[OrderBook]:
        """Refresh every discovered tradable market.

        Only safe for fixture data or a deliberately tiny universe. At live venue scale
        this is serial and rate-limited; use :meth:`refresh_books` instead.
        """
        targets: dict[Venue, list[str]] = {}
        for (venue, market_id), market in self.state.markets.items():
            if market.is_tradable:
                targets.setdefault(venue, []).append(market_id)
        return await self.refresh_books(targets)

    # ------------------------------------------------------------------- matching

    def generate_pairs(self, min_score: Decimal = Decimal("0.5")) -> list[MarketPair]:
        """Generate candidate pairs. Nothing produced here is approved."""
        kalshi = [m for (v, _), m in self.state.markets.items() if v is Venue.KALSHI]
        polymarket = [m for (v, _), m in self.state.markets.items() if v is Venue.POLYMARKET]
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
            pair_id = pair_id_for(watched.kalshi_market_id, watched.polymarket_market_id)
            if pair_id in self.state.pairs:
                continue
            pair = MarketPair(
                id=pair_id,
                kalshi_market_id=watched.kalshi_market_id,
                polymarket_market_id=watched.polymarket_market_id,
                status=MatchStatus.UNDER_REVIEW,
                manually_approved=False,
                outcomes_inverted=watched.outcomes_inverted,
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
        acknowledge_material_differences: bool = False,
    ) -> MarketPair:
        """Approve a pair. This is the only route to a 'confirmed' opportunity."""
        pair = self.state.pairs[pair_id]
        kalshi = self.state.markets[(Venue.KALSHI, pair.kalshi_market_id)]
        polymarket = self.state.markets[(Venue.POLYMARKET, pair.polymarket_market_id)]
        approved = approve_pair(
            pair,
            kalshi,
            polymarket,
            approved_by,
            reviewer_notes,
            acknowledged_material_differences=acknowledge_material_differences,
        )
        self.state.pairs[pair_id] = approved
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

    # --------------------------------------------------------------- paper trading

    async def paper_trade(self, opportunity: Opportunity) -> PaperTrade:
        """Simulate execution of one opportunity against current books."""

        async def provider(venue: Venue, market_id: str, _latency_ms: int) -> OrderBook | None:
            # A real implementation would advance simulated time by the latency; with
            # snapshot fixtures the latest stored book is the best available proxy.
            return self.store.get(venue, market_id)

        executor = PaperExecutor(
            self.settings.paper, provider, kalshi_direct_member=self.settings.kalshi.direct_member
        )
        kalshi = self.state.markets[(Venue.KALSHI, opportunity.kalshi_leg.market_id)]
        polymarket = self.state.markets[(Venue.POLYMARKET, opportunity.polymarket_leg.market_id)]
        trade = await executor.execute(opportunity, kalshi, polymarket)
        self.portfolio.record(trade)
        self.state.paper_trades.append(trade)
        return trade

    # ----------------------------------------------------------------------- misc

    def health(self, now: datetime | None = None) -> HealthReport:
        return build_health_report(self.adapters, len(self.store), now=now or datetime.now(UTC))

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()
