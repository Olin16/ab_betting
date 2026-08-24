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

from event_arb.domain.enums import MatchStatus
from event_arb.domain.models import MarketPair, VenueMarket
from event_arb.matching.candidates import generate_candidates
from event_arb.matching.readiness import ReadinessReport, assess_readiness
from event_arb.matching.rules_diff import RulesComparison
from event_arb.observability.logging import get_logger

__all__ = [
    "CandidateRecord",
    "DiscoveryLimits",
    "DiscoveryReport",
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
    verify_top: int = 25

    @property
    def max_local_comparisons(self) -> int:
        """Comparisons are local and cost no requests, but the bound is still stated."""
        return self.kalshi_limit * self.polymarket_limit


@dataclass(frozen=True)
class RequestPlan:
    """Exactly what will be requested, declared before anything is sent."""

    entries: tuple[dict[str, object], ...]
    minimum_requests: int
    worst_case_requests: int
    limits: DiscoveryLimits
    pagination: str

    def render(self) -> str:
        lines = ["Request plan", "=" * 60]
        for entry in self.entries:
            lines.append(f"  {entry['method']} {entry['url']}")
            lines.append(f"      params : {entry['params']}")
            lines.append(f"      headers: {entry['headers']}")
            lines.append(f"      purpose: {entry['purpose']}")
            lines.append(f"      count  : {entry['count']}")
        lines += [
            "-" * 60,
            f"  minimum requests    : {self.minimum_requests}",
            f"  worst-case requests : {self.worst_case_requests}",
            f"  pagination          : {self.pagination}",
            f"  local comparisons   : up to {self.limits.max_local_comparisons:,}",
            f"  persisted           : at most {self.limits.max_results} candidates",
            "  order books fetched : 0 (candidate research never reads depth)",
            "  websockets opened   : 0",
            "  credentials sent    : none (both venues serve public market data)",
        ]
        return "\n".join(lines)


def build_request_plan(
    limits: DiscoveryLimits, kalshi_url: str, polymarket_url: str
) -> RequestPlan:
    """Describe every request before any is issued.

    Headers are listed explicitly and are empty: public market data needs no credential,
    and printing the header set makes an accidental credential obvious rather than
    invisible. Any secret that did appear would be redacted by the logging pipeline.
    """
    entries: tuple[dict[str, object], ...] = (
        {
            "method": "GET",
            "url": f"{kalshi_url}/markets",
            "params": {"status": "open", "limit": limits.kalshi_limit},
            "headers": "(none — public endpoint, no credentials sent)",
            "purpose": "one bounded page of Kalshi market metadata",
            "count": 1,
        },
        {
            "method": "GET",
            "url": f"{polymarket_url}/v1/markets",
            "params": {
                "active": "true",
                "closed": "false",
                "limit": limits.polymarket_limit,
            },
            "headers": "(none — public gateway, no API key required)",
            "purpose": "one bounded page of Polymarket US market metadata",
            "count": 1,
        },
        {
            "method": "GET",
            "url": f"{polymarket_url}/v1/market/slug/{{slug}}",
            "params": "(one request per shortlisted market)",
            "headers": "(none)",
            "purpose": f"targeted re-fetch of the top {limits.verify_top} candidates",
            "count": f"0..{limits.verify_top}",
        },
    )
    return RequestPlan(
        entries=entries,
        minimum_requests=2,
        worst_case_requests=2 + limits.verify_top,
        limits=limits,
        pagination=("one page per venue; a returned cursor is reported and NOT followed"),
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
    verified: bool = False

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
            "targeted_reverify": self.verified,
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
    verified: int = 0
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
            "verified": self.verified,
            "parse_failures": self.parse_failures,
            "rate_limited": self.rate_limited,
            "pagination_cursors_ignored": self.pagination_cursors_ignored,
            "preserved_dispositions": self.preserved_dispositions,
            "order_books_fetched": 0,
            "auto_approved": 0,
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


def top_slugs_for_verification(report: DiscoveryReport, limits: DiscoveryLimits) -> list[str]:
    """Polymarket ids of the highest-scoring candidates, capped at ``verify_top``."""
    ordered = sorted(report.records, key=lambda record: record.score, reverse=True)
    seen: list[str] = []
    for record in ordered[: limits.verify_top]:
        if record.pair.polymarket_market_id not in seen:
            seen.append(record.pair.polymarket_market_id)
    return seen
