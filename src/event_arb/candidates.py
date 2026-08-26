"""Bounded candidate research.

Generates cross-venue pair *candidates* from public market metadata. It never approves,
never paper-trades, never touches an order book, and never opens a WebSocket.

The design constraint is that a broad pull is the largest volume of venue traffic this
project produces, so every bound is explicit and every request is declared before it is
made:

- **one bounded page per venue.** Pagination is not followed silently; if a venue returns a
  cursor it is reported and ignored, because "we fetched more than you authorised" is not
  a decision code should take on its own.
- **no order books.** Candidate generation compares metadata. Depth is only meaningful for
  a pair someone intends to trade, so it is fetched later, for a capped top N.
- **full source lists stay in memory.** Only the capped shortlist is persisted, because
  retaining an entire venue catalogue is neither necessary for review nor obviously within
  either venue's storage terms.
- **existing human dispositions are preserved.** A pair a human already approved, rejected
  or suspended is never overwritten by a research run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.domain.enums import MatchStatus, VenueProduct
from event_arb.domain.models import MarketPair, VenueMarket
from event_arb.matching.candidates import generate_candidates
from event_arb.matching.readiness import ReadinessReport, assess_readiness
from event_arb.matching.rules_diff import RulesComparison
from event_arb.observability.logging import get_logger

__all__ = [
    "CandidateRecord",
    "CredentialsWouldBeSentError",
    "DiscoveryLimits",
    "DiscoveryReport",
    "PlannedRequest",
    "RequestPlan",
    "build_request_plan",
    "research_candidates",
]

log = get_logger(__name__)

#: Dispositions a human has made. A research run must never overwrite one.
_HUMAN_DISPOSITIONS = frozenset(
    {
        MatchStatus.APPROVED,
        MatchStatus.REJECTED,
        MatchStatus.SUSPENDED,
        MatchStatus.EXPERT_REVIEW,
        MatchStatus.UNDER_REVIEW,
    }
)


@dataclass(frozen=True)
class DiscoveryLimits:
    """Every bound on a research run, in one place."""

    kalshi_limit: int = 250
    polymarket_limit: int = 250
    min_score: Decimal = Decimal("0.55")
    max_per_market: int = 3
    max_results: int = 100
    #: How many shortlisted Polymarket ids to ask the venue about a second time. Named for
    #: what it does: it rechecks that the id still resolves, and reads nothing from the
    #: response. It is not verification of the candidate.
    recheck_top: int = 25

    @property
    def max_local_comparisons(self) -> int:
        """Comparisons are local and cost no requests, but the bound is still stated."""
        return self.kalshi_limit * self.polymarket_limit


@dataclass(frozen=True)
class PlannedRequest:
    """One request the run will actually make."""

    method: str
    url: str
    params: dict[str, object] | str
    sends_credentials: bool
    purpose: str
    count: str


@dataclass(frozen=True)
class RequestPlan:
    """Exactly what will be requested, derived from the constructed adapters.

    Built *from* the adapters rather than written alongside them. A plan composed
    independently drifts from the code, and an operator authorising a request set that does
    not match what is sent is the specific failure the confirmation gate exists to prevent.
    """

    entries: tuple[PlannedRequest, ...]
    minimum_requests: int
    worst_case_requests: int
    limits: DiscoveryLimits
    pagination: str
    polymarket_product: VenueProduct
    sends_any_credentials: bool
    orderbook_pairs: int = 0

    def render(self) -> str:
        lines = ["Request plan", "=" * 62]
        for entry in self.entries:
            auth = (
                "SENDS CREDENTIALS (signed request headers)"
                if entry.sends_credentials
                else "(none - public endpoint, no credentials sent)"
            )
            lines.append(f"  {entry.method} {entry.url}")
            lines.append(f"      params : {entry.params}")
            lines.append(f"      headers: {auth}")
            lines.append(f"      purpose: {entry.purpose}")
            lines.append(f"      count  : {entry.count}")
        persistence = (
            f"at most {self.orderbook_pairs * 2} market legs and "
            f"{self.orderbook_pairs * 2} snapshots"
            if self.orderbook_pairs
            else f"at most {self.limits.max_results} candidates"
        )
        books = (
            f"0..{self.orderbook_pairs * 2} (one snapshot per selected market)"
            if self.orderbook_pairs
            else "0 (candidate research never reads depth)"
        )
        lines += [
            "-" * 62,
            f"  polymarket product  : {self.polymarket_product.value}",
            f"  minimum requests    : {self.minimum_requests}",
            f"  worst-case requests : {self.worst_case_requests}",
            f"  pagination          : {self.pagination}",
            f"  local comparisons   : up to {self.limits.max_local_comparisons:,}",
            f"  persisted           : {persistence}",
            f"  order books fetched : {books}",
            "  websockets opened   : 0",
            f"  credentials         : {'SENT' if self.sends_any_credentials else 'none'}",
        ]
        return "\n".join(lines)


class CredentialsWouldBeSentError(RuntimeError):
    """Raised when a credential-free run would in fact transmit credentials.

    Refusing is the point. The operator authorised a plan that said no credentials would be
    sent; silently sending them would make that authorisation meaningless.
    """


def build_request_plan(
    limits: DiscoveryLimits,
    kalshi_adapter: object,
    polymarket_adapter: object,
    polymarket_product: VenueProduct,
    *,
    orderbook_pairs: int = 0,
) -> RequestPlan:
    """Describe every request, reading endpoints and auth state off the adapters."""
    kalshi_base = str(getattr(kalshi_adapter, "discovery_base_url", "(fixture)"))
    kalshi_auth = bool(getattr(kalshi_adapter, "sends_credentials", False))
    polymarket_base = str(getattr(polymarket_adapter, "discovery_base_url", "(fixture)"))
    polymarket_auth = bool(getattr(polymarket_adapter, "sends_credentials", False))

    is_us = polymarket_product is VenueProduct.POLYMARKET_US
    polymarket_list_path = "/v1/markets" if is_us else "/markets"
    polymarket_one_path = "/v1/market/slug/{id}" if is_us else "/markets/{condition_id}"

    if orderbook_pairs < 0 or orderbook_pairs > 2:
        raise ValueError("orderbook_pairs must be between 0 and 2")

    entries = [
        PlannedRequest(
            method="GET",
            url=f"{kalshi_base}/markets",
            params={"status": "open", "limit": limits.kalshi_limit},
            sends_credentials=kalshi_auth,
            purpose="one bounded page of Kalshi market metadata",
            count="1",
        ),
        PlannedRequest(
            method="GET",
            url=f"{polymarket_base}{polymarket_list_path}",
            params={
                "active": "true",
                "closed": "false",
                "limit": limits.polymarket_limit,
            },
            sends_credentials=polymarket_auth,
            purpose=(f"one bounded page of Polymarket {polymarket_product.value} market metadata"),
            count="1",
        ),
        PlannedRequest(
            method="GET",
            url=f"{polymarket_base}{polymarket_one_path}",
            params="(one request per shortlisted market)",
            sends_credentials=polymarket_auth,
            purpose=f"existence recheck of the top {limits.recheck_top} candidate ids "
            f"(response body not read)",
            count=f"0..{limits.recheck_top}",
        ),
    ]
    if orderbook_pairs:
        kalshi_book_path = "/markets/{ticker}/orderbook"
        polymarket_book_path = "/v1/markets/{id}/book" if is_us else "/book?token_id={token_id}"
        entries.extend(
            (
                PlannedRequest(
                    method="GET",
                    url=f"{kalshi_base}{kalshi_book_path}",
                    params="(none)",
                    sends_credentials=kalshi_auth,
                    purpose="one order-book snapshot per selected Kalshi market",
                    count=f"0..{orderbook_pairs}",
                ),
                PlannedRequest(
                    method="GET",
                    url=f"{polymarket_base}{polymarket_book_path}",
                    params="(none)",
                    sends_credentials=polymarket_auth,
                    purpose=(
                        "one order-book snapshot per selected Polymarket "
                        f"{polymarket_product.value} market"
                    ),
                    count=f"0..{orderbook_pairs}",
                ),
            )
        )
    return RequestPlan(
        entries=tuple(entries),
        minimum_requests=2,
        worst_case_requests=2 + limits.recheck_top + (orderbook_pairs * 2),
        limits=limits,
        pagination="one page per venue; a returned cursor is reported and NOT followed",
        polymarket_product=polymarket_product,
        sends_any_credentials=kalshi_auth or polymarket_auth,
        orderbook_pairs=orderbook_pairs,
    )


@dataclass(frozen=True)
class CandidateRecord:
    """One shortlisted candidate, with everything a reviewer needs and nothing else."""

    pair: MarketPair
    score: Decimal
    kalshi_title: str
    polymarket_title: str
    comparison: RulesComparison
    readiness: ReadinessReport
    #: Whether a second, targeted request confirmed the Polymarket id still resolves.
    #:
    #: This is **not** verification of the candidate. The re-fetched record's metadata is
    #: not read: nothing is re-normalized, no score is recomputed, no rule comparison is
    #: re-run, no fingerprint is recalculated and no readiness check is repeated. All it
    #: establishes is that the id was still returned by the venue at the time below.
    #: Calling that "verified" invited a reviewer to skip exactly the work it did not do.
    existence_rechecked: bool = False
    existence_rechecked_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair.id,
            "score": str(self.score),
            "kalshi_market_id": self.pair.kalshi_market_id,
            "kalshi_title": self.kalshi_title,
            "polymarket_market_id": self.pair.polymarket_market_id,
            "polymarket_title": self.polymarket_title,
            "polymarket_product": self.pair.polymarket_product.value,
            "material_differences": [d.dimension for d in self.comparison.material],
            "ready_for_approval": self.readiness.is_ready,
            "blockers": list(self.readiness.blocker_codes),
            "existence_rechecked": self.existence_rechecked,
            "existence_rechecked_at": (
                self.existence_rechecked_at.isoformat() if self.existence_rechecked_at else None
            ),
            "existence_recheck_scope": (
                "the venue still returns this market id; no metadata was re-read"
                if self.existence_rechecked
                else "not rechecked"
            ),
            # Never "candidate arbitrage": nothing here is approved.
            "label": "research_candidate",
        }


@dataclass
class DiscoveryReport:
    """What a research run saw, kept, and refused to do."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    kalshi_markets_seen: int = 0
    polymarket_markets_seen: int = 0
    comparisons: int = 0
    candidates_generated: int = 0
    candidates_retained: int = 0
    ready_for_approval: int = 0
    #: How many shortlisted candidates had their id re-confirmed. See
    #: :attr:`CandidateRecord.existence_rechecked` for what that does and does not mean.
    existence_rechecked: int = 0
    parse_failures: int = 0
    rate_limited: int = 0
    pagination_cursors_ignored: int = 0
    preserved_dispositions: int = 0
    records: list[CandidateRecord] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at.isoformat(),
            "kalshi_markets_seen": self.kalshi_markets_seen,
            "polymarket_markets_seen": self.polymarket_markets_seen,
            "comparisons": self.comparisons,
            "candidates_generated": self.candidates_generated,
            "candidates_retained": self.candidates_retained,
            "ready_for_approval": self.ready_for_approval,
            "existence_rechecked": self.existence_rechecked,
            "parse_failures": self.parse_failures,
            "rate_limited": self.rate_limited,
            "pagination_cursors_ignored": self.pagination_cursors_ignored,
            "preserved_dispositions": self.preserved_dispositions,
            # Constant zeros, reported rather than assumed: research reads no depth, can
            # approve nothing, and simulates nothing. Their absence from the output would
            # be indistinguishable from a run that forgot to count them.
            "order_books_fetched": 0,
            "approvals_created": 0,
            "paper_trades": 0,
        }


def research_candidates(
    kalshi_markets: list[VenueMarket],
    polymarket_markets: list[VenueMarket],
    limits: DiscoveryLimits,
    existing_pairs: dict[str, MarketPair] | None = None,
) -> DiscoveryReport:
    """Generate, score, assess and cap candidates. Pure computation, no requests."""
    report = DiscoveryReport()
    report.kalshi_markets_seen = len(kalshi_markets)
    report.polymarket_markets_seen = len(polymarket_markets)
    report.comparisons = len(kalshi_markets) * len(polymarket_markets)

    generated = generate_candidates(
        kalshi_markets,
        polymarket_markets,
        min_score=limits.min_score,
        max_per_market=limits.max_per_market,
    )
    report.candidates_generated = len(generated)

    kalshi_by_id = {m.venue_market_id: m for m in kalshi_markets}
    polymarket_by_id = {m.venue_market_id: m for m in polymarket_markets}
    existing = existing_pairs or {}

    for pair, score, comparison in generated:
        if len(report.records) >= limits.max_results:
            break

        prior = existing.get(pair.id)
        if prior is not None and prior.status in _HUMAN_DISPOSITIONS:
            # A human already decided about this pair. Research does not revisit it.
            report.preserved_dispositions += 1
            continue

        kalshi = kalshi_by_id.get(pair.kalshi_market_id)
        polymarket = polymarket_by_id.get(pair.polymarket_market_id)
        if kalshi is None or polymarket is None:
            report.parse_failures += 1
            continue

        readiness = assess_readiness(pair, kalshi, polymarket, differences=comparison.differences)
        report.records.append(
            CandidateRecord(
                pair=pair,
                score=score.score,
                kalshi_title=kalshi.title,
                polymarket_title=polymarket.title,
                comparison=comparison,
                readiness=readiness,
            )
        )
        if readiness.is_ready:
            report.ready_for_approval += 1

    report.candidates_retained = len(report.records)
    log.info("candidates.research_complete", **report.as_dict())
    return report


def top_slugs_for_recheck(report: DiscoveryReport, limits: DiscoveryLimits) -> list[str]:
    """Polymarket ids of the highest-scoring candidates, capped at ``recheck_top``."""
    ordered = sorted(report.records, key=lambda record: record.score, reverse=True)
    seen: list[str] = []
    for record in ordered[: limits.recheck_top]:
        if record.pair.polymarket_market_id not in seen:
            seen.append(record.pair.polymarket_market_id)
    return seen
