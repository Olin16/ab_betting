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
from event_arb.domain.models import MarketPair, VenueMarket
from event_arb.observability.logging import get_logger

__all__ = [
    "ApprovalError",
    "RulesChangeCheck",
    "approve_pair",
    "check_for_rules_change",
    "enforce_rules_integrity",
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
    acknowledged_material_differences: bool = False,
) -> MarketPair:
    """Approve a pair as settlement-equivalent.

    Raises :class:`ApprovalError` when material rule differences exist and the reviewer
    has not explicitly acknowledged them. This is the guardrail against approving a pair
    by reflex: the system will not let a reviewer skip past a difference that can change
    which side settles in the money.
    """
    if not approved_by.strip():
        raise ApprovalError("approval requires an identified reviewer")
    if pair.status is MatchStatus.REJECTED:
        raise ApprovalError("a rejected pair must be re-opened for review before approval")

    material = [d for d in pair.rule_differences if d.is_material]
    if material and not acknowledged_material_differences:
        dimensions = ", ".join(sorted({d.dimension for d in material}))
        raise ApprovalError(
            f"pair has material rule differences ({dimensions}); the reviewer must "
            "explicitly acknowledge them before approval"
        )

    approved = pair.model_copy(
        update={
            "status": MatchStatus.APPROVED,
            "manually_approved": True,
            "approved_by": approved_by,
            "approved_at": datetime.now(UTC),
            "reviewer_notes": reviewer_notes or pair.reviewer_notes,
            "kalshi_rules_hash_at_approval": kalshi_market.rules_hash,
            "polymarket_rules_hash_at_approval": polymarket_market.rules_hash,
        }
    )
    log.info(
        "pair.approved",
        pair_id=pair.id,
        approved_by=approved_by,
        material_differences=len(material),
    )
    return approved


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

    kalshi_changed = (
        pair.kalshi_rules_hash_at_approval is not None
        and pair.kalshi_rules_hash_at_approval != kalshi_market.rules_hash
    )
    polymarket_changed = (
        pair.polymarket_rules_hash_at_approval is not None
        and pair.polymarket_rules_hash_at_approval != polymarket_market.rules_hash
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
