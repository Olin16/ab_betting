"""Opportunity detection service.

Orchestrates one scan cycle:

1. re-verify each approved pair's rules hashes, auto-suspending on change;
2. pull both books and judge freshness (a stale book suppresses the pair entirely);
3. evaluate **both** directions;
4. attach risk flags;
5. deduplicate and apply an alert cooldown.

Ordering matters. Rules integrity is checked before any pricing, so a pair whose rules
changed mid-session cannot produce a "confirmed" opportunity even for one cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from event_arb.config import OpportunitySettings, PolymarketSettings
from event_arb.domain.enums import ArbDirection, RejectionReason, RiskFlag
from event_arb.domain.keys import MarketKey, pair_market_keys
from event_arb.domain.models import (
    MarketPair,
    Opportunity,
    RejectedOpportunity,
    VenueMarket,
)
from event_arb.matching.approval import enforce_rules_integrity
from event_arb.observability import metrics
from event_arb.observability.logging import get_logger
from event_arb.opportunities.calculator import CalculationInputs, calculate_direction
from event_arb.opportunities.risk import assess_risk_flags, single_level_flag
from event_arb.orderbooks.book import BookStore

__all__ = ["OpportunityService", "ScanResult"]

log = get_logger(__name__)


@dataclass
class ScanResult:
    """Everything one scan cycle produced, including why candidates were dropped."""

    opportunities: list[Opportunity] = field(default_factory=list)
    #: Detections that were real but not re-announced, because the same opportunity alerted
    #: within the cooldown window. Kept as whole opportunities rather than rejection stubs
    #: so the evidence record stays complete: over a multi-week trial the cooldown is an
    #: alerting knob, and it must not decide what was observed.
    alert_suppressed: list[Opportunity] = field(default_factory=list)
    rejections: list[RejectedOpportunity] = field(default_factory=list)
    suspended_pairs: list[MarketPair] = field(default_factory=list)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def all_detected(self) -> list[Opportunity]:
        """Everything this cycle actually saw, announced or not.

        Use this for evidence and persistence. Use :attr:`opportunities` for anything that
        should happen once per cooldown window — alerting, and paper execution.
        """
        return [*self.opportunities, *self.alert_suppressed]

    @property
    def confirmed(self) -> list[Opportunity]:
        return [o for o in self.opportunities if o.is_confirmed]

    @property
    def research_candidates(self) -> list[Opportunity]:
        return [o for o in self.opportunities if not o.is_confirmed]


class OpportunityService:
    """Detects opportunities across a set of market pairs."""

    def __init__(
        self,
        store: BookStore,
        settings: OpportunitySettings,
        polymarket_settings: PolymarketSettings | None = None,
        kalshi_direct_member: bool = False,
    ) -> None:
        self._store = store
        self._settings = settings
        self._polymarket_settings = polymarket_settings or PolymarketSettings()
        self._kalshi_direct_member = kalshi_direct_member
        #: dedup key -> last emission time, for alert cooldown
        self._last_emitted: dict[str, datetime] = {}

    def _cooldown_active(self, key: str, now: datetime) -> bool:
        last = self._last_emitted.get(key)
        if last is None:
            return False
        return now - last < timedelta(seconds=self._settings.alert_cooldown_seconds)

    def scan(
        self,
        pairs: list[MarketPair],
        markets: dict[MarketKey, VenueMarket],
        now: datetime | None = None,
    ) -> ScanResult:
        """Run one detection cycle over the supplied pairs."""
        reference = now or datetime.now(UTC)
        result = ScanResult(scanned_at=reference)

        for pair in pairs:
            kalshi_k, polymarket_k = pair_market_keys(pair)
            kalshi_market = markets.get(kalshi_k)
            polymarket_market = markets.get(polymarket_k)
            if kalshi_market is None or polymarket_market is None:
                result.rejections.append(
                    RejectedOpportunity(
                        pair_id=pair.id,
                        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
                        reason=RejectionReason.BOOK_MISSING,
                        detail="market metadata missing for one or both legs",
                    )
                )
                continue

            # (1) Rules integrity first: a changed rules text invalidates approval.
            checked = enforce_rules_integrity(pair, kalshi_market, polymarket_market)
            if checked is not pair and not checked.manually_approved:
                result.suspended_pairs.append(checked)
            pair = checked

            if pair.status.value == "suspended":
                result.rejections.append(
                    RejectedOpportunity(
                        pair_id=pair.id,
                        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
                        reason=RejectionReason.PAIR_SUSPENDED,
                        detail="pair suspended pending re-review",
                    )
                )
                continue

            if not pair.is_tradable_pair and not self._settings.include_research_candidates:
                result.rejections.append(
                    RejectedOpportunity(
                        pair_id=pair.id,
                        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
                        reason=RejectionReason.PAIR_NOT_APPROVED,
                        detail="pair is not manually approved",
                    )
                )
                continue

            # (2) Books and freshness.
            kalshi_book, kalshi_verdict = self._store.evaluate(kalshi_k, reference)
            polymarket_book, polymarket_verdict = self._store.evaluate(polymarket_k, reference)
            if kalshi_book is None or polymarket_book is None:
                result.rejections.append(
                    RejectedOpportunity(
                        pair_id=pair.id,
                        direction=ArbDirection.A_KALSHI_YES_POLY_NO,
                        reason=RejectionReason.BOOK_MISSING,
                        detail="no order book available for one or both legs",
                    )
                )
                continue

            assert kalshi_verdict is not None and polymarket_verdict is not None
            if not kalshi_verdict.is_fresh or not polymarket_verdict.is_fresh:
                stale_side = "kalshi" if not kalshi_verdict.is_fresh else "polymarket"
                detail = (
                    kalshi_verdict.reason
                    if not kalshi_verdict.is_fresh
                    else polymarket_verdict.reason
                )
                for direction in ArbDirection:
                    result.rejections.append(
                        RejectedOpportunity(
                            pair_id=pair.id,
                            direction=direction,
                            reason=RejectionReason.BOOK_STALE,
                            detail=f"{stale_side}: {detail}",
                        )
                    )
                continue

            # (3) + (4) Evaluate both directions with risk flags attached.
            flags = assess_risk_flags(
                pair,
                kalshi_market,
                polymarket_market,
                kalshi_book,
                polymarket_book,
                kalshi_verdict,
                polymarket_verdict,
                reference,
            )
            inputs = CalculationInputs(
                pair=pair,
                kalshi_market=kalshi_market,
                polymarket_market=polymarket_market,
                kalshi_book=kalshi_book,
                polymarket_book=polymarket_book,
                settings=self._settings,
                kalshi_book_age_ms=kalshi_verdict.age_ms,
                polymarket_book_age_ms=polymarket_verdict.age_ms,
                usdc_haircut_pct=self._polymarket_settings.usdc_haircut_pct,
                kalshi_direct_member=self._kalshi_direct_member,
                risk_flags=flags,
            )

            for direction in ArbDirection:
                outcome = calculate_direction(inputs, direction)
                if isinstance(outcome, RejectedOpportunity):
                    result.rejections.append(outcome)
                    metrics.opportunities_rejected.labels(reason=outcome.reason.value).inc()
                    continue

                extra = single_level_flag(
                    outcome.kalshi_leg.levels_consumed, outcome.polymarket_leg.levels_consumed
                )
                if extra:
                    merged = tuple(sorted({*outcome.risk_flags, *extra}, key=lambda f: f.value))
                    outcome = outcome.model_copy(update={"risk_flags": merged})

                # (5) Dedup + cooldown. The opportunity is recorded either way; the
                # cooldown only decides whether it is announced again. Recording it as a
                # bare rejection used to discard its size, prices and profit, so a
                # persistent opportunity read as one detection plus a run of suppressions —
                # and how long an opportunity persists is exactly what distinguishes a real
                # one from a stale-quote artifact.
                if self._cooldown_active(outcome.dedup_key, reference):
                    result.alert_suppressed.append(outcome)
                    metrics.opportunities_rejected.labels(
                        reason=RejectionReason.COOLDOWN_ACTIVE.value
                    ).inc()
                    continue

                self._last_emitted[outcome.dedup_key] = reference
                result.opportunities.append(outcome)
                metrics.opportunities_detected.labels(
                    label=outcome.label.value, direction=direction.value
                ).inc()
                log.info(
                    "opportunity.detected",
                    pair_id=pair.id,
                    direction=direction.value,
                    label=outcome.label.value,
                    net_profit=str(outcome.net_profit),
                    quantity=str(outcome.executable_quantity),
                    risk_flags=[flag.value for flag in outcome.risk_flags],
                )

        result.opportunities.sort(key=lambda o: o.net_profit, reverse=True)
        return result

    def reset_cooldowns(self) -> None:
        self._last_emitted.clear()


def flags_include_stale(flags: tuple[RiskFlag, ...]) -> bool:
    return bool({RiskFlag.STALE_BOOK_KALSHI, RiskFlag.STALE_BOOK_POLYMARKET} & set(flags))
