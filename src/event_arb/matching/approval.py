"""Approval and suspension workflow for market pairs.

Approval is the only mechanism that turns a machine-suggested pair into something this
system will call an arbitrage. It is deliberately a manual, recorded, reversible act:

- A human must supply their identity and confirm they reviewed the rule differences.
- The **rules hashes of both legs at approval time** are stored on the pair.
- Any later change to either rules text invalidates the approval, and the pair is
  automatically **suspended** until a human reviews it again.

The hash is computed over whitespace-normalized, lowercased rules text plus title and
settlement source, so cosmetic reformatting by a venue does not cause churn while any
substantive edit does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from event_arb.domain.enums import MatchStatus
from event_arb.domain.models import MarketPair, ReviewedSource, VenueMarket
from event_arb.matching.readiness import assess_readiness, effective_sources
from event_arb.observability.logging import get_logger

__all__ = [
    "ApprovalError",
    "RulesChangeCheck",
    "approve_pair",
    "check_for_rules_change",
    "enforce_rules_integrity",
    "record_expert_review",
    "reject_pair",
    "suspend_pair",
]

log = get_logger(__name__)


class ApprovalError(Exception):
    """Raised when an approval request is not valid."""


@dataclass(frozen=True)
class RulesChangeCheck:
    changed: bool
    kalshi_changed: bool
    polymarket_changed: bool
    detail: str


def approve_pair(
    pair: MarketPair,
    kalshi_market: VenueMarket,
    polymarket_market: VenueMarket,
    approved_by: str,
    reviewer_notes: str = "",
    kalshi_reviewed_source: ReviewedSource | None = None,
    polymarket_reviewed_source: ReviewedSource | None = None,
    settlement_deadline_required: bool = False,
) -> MarketPair:
    """Approve a pair as settlement-equivalent.

    Raises :class:`ApprovalError` unless the pair is *ready*: we hold authoritative rules
    and a named settlement source for both legs, product and outcome identity resolve,
    close times are known, and no detected difference can settle the legs to opposite
    outcomes.

    There is deliberately **no generic acknowledgement override**. A reviewer ticking a box
    cannot make a contradictory pair equivalent — that is not a risk being accepted, it is
    a false statement. Genuine exceptions go through :func:`record_expert_review`, which is
    separately named, separately audited, and never produces a confirmed arbitrage label.

    ``*_reviewed_source`` records the resolution authority a reviewer verified for a leg
    whose venue publishes none. Both legs must resolve to the **same** canonical authority;
    two unknowns are not agreement, and neither are two different known authorities. The
    reviewed source is stored on the pair and folded into the settlement fingerprint, so a
    later change to it suspends the approval exactly as a rules change would.
    """
    if not approved_by.strip():
        raise ApprovalError("approval requires an identified reviewer")
    if pair.status is MatchStatus.REJECTED:
        raise ApprovalError("a rejected pair must be re-opened for review before approval")

    # The reviewed sources go onto the pair *before* assessment, so readiness judges the
    # pair as it will be stored rather than as it arrived.
    candidate = pair.model_copy(
        update={
            "kalshi_reviewed_source": kalshi_reviewed_source or pair.kalshi_reviewed_source,
            "polymarket_reviewed_source": (
                polymarket_reviewed_source or pair.polymarket_reviewed_source
            ),
        }
    )
    readiness = assess_readiness(
        candidate,
        kalshi_market,
        polymarket_market,
        settlement_deadline_required=settlement_deadline_required,
    )
    if not readiness.is_ready:
        detail = "; ".join(f"{code.value}: {reason}" for code, reason in readiness.blockers)
        raise ApprovalError(f"pair is not ready for approval — {detail}")

    kalshi_source, polymarket_source = effective_sources(
        candidate, kalshi_market, polymarket_market
    )
    approved = candidate.model_copy(
        update={
            "status": MatchStatus.APPROVED,
            "manually_approved": True,
            "approved_by": approved_by,
            "approved_at": datetime.now(UTC),
            "reviewer_notes": reviewer_notes or pair.reviewer_notes,
            # The hash covers the authority the approval actually rests on.
            "kalshi_rules_hash_at_approval": kalshi_market.fingerprint_with_source(kalshi_source),
            "polymarket_rules_hash_at_approval": polymarket_market.fingerprint_with_source(
                polymarket_source
            ),
        }
    )
    log.info("pair.approved", pair_id=pair.id, approved_by=approved_by)
    return approved


def record_expert_review(
    pair: MarketPair,
    kalshi_market: VenueMarket,
    polymarket_market: VenueMarket,
    reviewed_by: str,
    justification: str,
) -> MarketPair:
    """Record a documented exception for a pair that is not normally approvable.

    This is **not** approval. The pair moves to ``EXPERT_REVIEW`` and
    ``manually_approved`` stays false, so :attr:`MarketPair.is_tradable_pair` remains
    false and no opportunity on it can ever be labeled confirmed. It exists so an expert
    judgement can be recorded and audited rather than smuggled in as an approval.
    """
    if not reviewed_by.strip():
        raise ApprovalError("expert review requires an identified reviewer")
    if len(justification.strip()) < 40:
        raise ApprovalError(
            "expert review requires a substantive written justification (at least 40 characters)"
        )

    readiness = assess_readiness(pair, kalshi_market, polymarket_market)
    log.warning(
        "pair.expert_review_recorded",
        pair_id=pair.id,
        reviewed_by=reviewed_by,
        blockers=list(readiness.blocker_codes),
    )
    return pair.model_copy(
        update={
            "status": MatchStatus.EXPERT_REVIEW,
            "manually_approved": False,
            "reviewer_notes": (
                f"{pair.reviewer_notes}\n[expert review by {reviewed_by}] {justification}"
            ).strip(),
        }
    )


def reject_pair(pair: MarketPair, reviewer_notes: str, rejected_by: str) -> MarketPair:
    """Mark a pair as not settlement-equivalent."""
    log.info("pair.rejected", pair_id=pair.id, rejected_by=rejected_by)
    return pair.model_copy(
        update={
            "status": MatchStatus.REJECTED,
            "manually_approved": False,
            "reviewer_notes": reviewer_notes,
            "approved_by": None,
            "approved_at": None,
        }
    )


def suspend_pair(pair: MarketPair, reason: str) -> MarketPair:
    """Suspend an approved pair pending re-review.

    ``manually_approved`` is cleared so that a suspended pair can never satisfy
    :attr:`MarketPair.is_tradable_pair`, whatever else changes around it.
    """
    log.warning("pair.suspended", pair_id=pair.id, reason=reason)
    return pair.model_copy(
        update={
            "status": MatchStatus.SUSPENDED,
            "manually_approved": False,
            "reviewer_notes": f"{pair.reviewer_notes}\n[auto-suspended] {reason}".strip(),
        }
    )


def check_for_rules_change(
    pair: MarketPair, kalshi_market: VenueMarket, polymarket_market: VenueMarket
) -> RulesChangeCheck:
    """Compare current rules hashes against the ones recorded at approval."""
    if not pair.manually_approved:
        return RulesChangeCheck(False, False, False, "pair is not approved")

    kalshi_source, polymarket_source = effective_sources(pair, kalshi_market, polymarket_market)
    kalshi_changed = (
        pair.kalshi_rules_hash_at_approval is not None
        and pair.kalshi_rules_hash_at_approval
        != kalshi_market.fingerprint_with_source(kalshi_source)
    )
    polymarket_changed = (
        pair.polymarket_rules_hash_at_approval is not None
        and pair.polymarket_rules_hash_at_approval
        != polymarket_market.fingerprint_with_source(polymarket_source)
    )

    changed_venues = [
        name
        for name, flag in (("Kalshi", kalshi_changed), ("Polymarket", polymarket_changed))
        if flag
    ]
    detail = (
        f"rules text changed on {' and '.join(changed_venues)} since approval"
        if changed_venues
        else "rules unchanged since approval"
    )
    return RulesChangeCheck(bool(changed_venues), kalshi_changed, polymarket_changed, detail)


def enforce_rules_integrity(
    pair: MarketPair, kalshi_market: VenueMarket, polymarket_market: VenueMarket
) -> MarketPair:
    """Return the pair, auto-suspended if either venue's rules have changed.

    This is called on every scan cycle so that a mid-flight rules edit cannot leave a
    stale approval in force.
    """
    check = check_for_rules_change(pair, kalshi_market, polymarket_market)
    if check.changed:
        return suspend_pair(pair, check.detail)
    return pair
