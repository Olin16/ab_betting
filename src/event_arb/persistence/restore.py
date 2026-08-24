"""Safe restoration of persisted state on startup.

The dangerous operation at startup is reinstating an approval. An approval is a human
statement that two contracts were settlement-equivalent **as their rules read at that
moment**, and the pair carries the rules hashes to prove which text was reviewed.

If a venue has edited its rules while the process was down, the stored approval is
evidence about a contract that no longer exists. Restoring it verbatim would silently
reinstate a claim nobody has actually made about the current text. So restoration
re-checks every approval against current rules and suspends anything that does not match,
exactly as the running scanner would have done had it seen the change live.

An approval whose market metadata is missing entirely is also suspended: we cannot verify
what we cannot read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from event_arb.domain.enums import MatchStatus
from event_arb.domain.keys import MarketKey, pair_market_keys
from event_arb.domain.models import MarketPair, VenueMarket
from event_arb.matching.approval import check_for_rules_change, suspend_pair
from event_arb.matching.candidates import pair_id_for
from event_arb.observability.logging import get_logger

__all__ = ["RestoreReport", "restore_pairs"]

log = get_logger(__name__)


@dataclass
class RestoreReport:
    """What restoration did, for the audit log and the operator."""

    restored: list[MarketPair] = field(default_factory=list)
    suspended: list[tuple[MarketPair, str]] = field(default_factory=list)

    @property
    def approvals_kept(self) -> int:
        return sum(1 for pair in self.restored if pair.is_tradable_pair)

    def as_dict(self) -> dict[str, object]:
        return {
            "restored": len(self.restored),
            "approvals_kept": self.approvals_kept,
            "suspended": [
                {"pair_id": pair.id, "reason": reason} for pair, reason in self.suspended
            ],
        }


def restore_pairs(
    stored: dict[str, MarketPair],
    markets: dict[MarketKey, VenueMarket],
    observed: set[MarketKey] | None = None,
) -> tuple[dict[str, MarketPair], RestoreReport]:
    """Return pairs safe to use, suspending approvals that no longer hold.

    ``observed`` names the markets whose metadata was actually retrieved from the venue
    during this run. An approval may only be restored when **both** legs were observed:
    a persisted copy will always match the hash it was stored with, so validating against
    cached metadata proves nothing about the venue's current rules. Passing ``None``
    disables the check and is intended only for tests of the hash logic itself.

    An approval whose stored id does not match the id its own legs now hash to is also
    suspended: the identity scheme changed underneath it, and the recorded review was
    made against a pair identity that no longer means what it meant then.

    Unapproved pairs pass through untouched — there is no claim to invalidate.
    """
    report = RestoreReport()
    result: dict[str, MarketPair] = {}

    for pair_id, pair in stored.items():
        if not pair.manually_approved:
            result[pair_id] = pair
            report.restored.append(pair)
            continue

        expected_id = pair_id_for(
            pair.kalshi_market_id, pair.polymarket_market_id, pair.polymarket_product
        )
        if pair_id != expected_id:
            # Pair ids gained a product component. An id minted before that change encoded
            # an ambiguous identity, so the approval recorded against it cannot be carried
            # forward — the pair is re-proposed under its new id and must be re-reviewed.
            reason = (
                f"pair id {pair_id!r} predates the current identity scheme (expected "
                f"{expected_id!r}); approval must be re-reviewed under the new identity"
            )
            suspended = suspend_pair(pair, reason)
            result[pair_id] = suspended
            report.suspended.append((suspended, reason))
            continue

        kalshi_k, polymarket_k = pair_market_keys(pair)
        kalshi = markets.get(kalshi_k)
        polymarket = markets.get(polymarket_k)

        if kalshi is None or polymarket is None:
            missing = "Kalshi" if kalshi is None else "Polymarket"
            reason = (
                f"{missing} market metadata unavailable at startup; approval cannot be "
                "verified against current rules"
            )
            suspended = suspend_pair(pair, reason)
            result[pair_id] = suspended
            report.suspended.append((suspended, reason))
            continue

        if observed is not None:
            unobserved = [
                name
                for name, key in (("Kalshi", kalshi_k), ("Polymarket", polymarket_k))
                if key not in observed
            ]
            if unobserved:
                # Cached metadata matches the hash it was stored with by construction, so
                # it can never falsify an approval. Only fresh venue data can.
                reason = (
                    f"{' and '.join(unobserved)} metadata was not retrieved from the venue "
                    "during this run; approval cannot be verified against current rules"
                )
                suspended = suspend_pair(pair, reason)
                result[pair_id] = suspended
                report.suspended.append((suspended, reason))
                continue

        # Product identity is checked before the fingerprint. The fingerprint would also
        # catch a mismatch, but only incidentally via settlement asset; an explicit check
        # states the rule and survives a future product that happens to share an asset.
        if pair.polymarket_product is not polymarket.product:
            reason = (
                f"pair expects Polymarket product {pair.polymarket_product.value!r} but the "
                f"stored market is {polymarket.product.value!r}; a Global approval can "
                "never satisfy a US pair"
            )
            suspended = suspend_pair(pair, reason)
            result[pair_id] = suspended
            report.suspended.append((suspended, reason))
            continue

        check = check_for_rules_change(pair, kalshi, polymarket)
        if check.changed:
            suspended = suspend_pair(pair, f"{check.detail} (detected during restore)")
            result[pair_id] = suspended
            report.suspended.append((suspended, check.detail))
            continue

        result[pair_id] = pair
        report.restored.append(pair)

    if report.suspended:
        log.warning(
            "restore.approvals_suspended",
            count=len(report.suspended),
            pair_ids=[pair.id for pair, _ in report.suspended],
        )
    log.info(
        "restore.completed",
        restored=len(report.restored),
        approvals_kept=report.approvals_kept,
        suspended=len(report.suspended),
    )
    return result, report


def is_restorable(pair: MarketPair) -> bool:
    """True when a stored pair is in a state worth loading at all."""
    return pair.status is not MatchStatus.REJECTED
