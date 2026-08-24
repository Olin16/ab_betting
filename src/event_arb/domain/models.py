"""Pydantic value objects for the domain.

These are transport/compute models. Their persisted counterparts live in
``event_arb.persistence.models``; keeping them separate stops SQLAlchemy identity-map
concerns from leaking into the pure calculation path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from event_arb.domain.enums import (
    ArbDirection,
    ComparisonOperator,
    MatchStatus,
    OpportunityLabel,
    Outcome,
    RejectionReason,
    RiskFlag,
    SettlementAsset,
    Venue,
)
from event_arb.domain.money import ZERO, quantize_price, quantize_qty

__all__ = [
    "SETTLEMENT_FINGERPRINT_VERSION",
    "BookLevel",
    "CanonicalMarket",
    "ExecutionPlan",
    "FeeConfig",
    "LegPlan",
    "MarketPair",
    "Opportunity",
    "OrderBook",
    "OrderBookSide",
    "RuleDifference",
    "VenueMarket",
    "rules_hash",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Bump when the set of fields below changes. A bump invalidates every stored approval,
#: forcing re-review — which is the point: an old approval was granted against a weaker
#: definition of "unchanged".
SETTLEMENT_FINGERPRINT_VERSION = "v2"


def rules_hash(*parts: str | None) -> str:
    """Stable content hash of rules text, used to detect post-approval rule changes.

    Whitespace is normalized so that cosmetic reformatting by a venue does not trigger a
    spurious suspension, but any substantive text change does.
    """
    digest = hashlib.sha256()
    for part in parts:
        normalized = " ".join((part or "").split()).lower()
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


# --------------------------------------------------------------------------- markets


class CanonicalMarket(_Frozen):
    """A venue-independent description of the real-world question being traded."""

    id: str
    canonical_question: str
    normalized_event_type: str
    affirmative_outcome_definition: str
    threshold: Decimal | None = None
    comparison_operator: ComparisonOperator | None = None
    event_start: datetime | None = None
    event_end: datetime | None = None
    timezone: str = "UTC"
    settlement_deadline: datetime | None = None
    resolution_source: str
    cancellation_behavior: str
    invalid_resolution_behavior: str


class FeeConfig(_Frozen):
    """Per-market fee parameters.

    Fee rates are *queried per market*, never hard-coded globally: Kalshi applies a
    per-series multiplier and Polymarket's taker rate varies by category.
    """

    venue: Venue
    #: Kalshi: the 0.07 coefficient. Polymarket: the category taker rate.
    taker_rate: Decimal
    maker_rate: Decimal = ZERO
    #: Rebate **paid to** a maker, as a positive amount. Polymarket US publishes this as a
    #: negative theta; it is held positive so that "rate" means "amount charged"
    #: everywhere and a rebate can never be read as a fee by a caller that drops the sign.
    maker_rebate_rate: Decimal = ZERO
    #: Kalshi per-series multiplier ``M`` in ``ceil(M * 0.07 * C * P * (1-P))``.
    multiplier: Decimal = Decimal("1")
    #: Source of these numbers, for auditing. Never trust an undocumented default.
    source: str = "default"

    @field_validator("taker_rate", "maker_rate", "maker_rebate_rate", "multiplier")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < ZERO:
            raise ValueError("fee parameters must be non-negative")
        return v


class VenueMarket(_Frozen):
    """A tradable contract as it exists on one venue."""

    venue: Venue
    venue_market_id: str
    title: str
    rules_text: str
    outcomes: tuple[str, ...] = ("YES", "NO")
    close_time: datetime | None = None
    settlement_source: str = ""
    tick_size: Decimal = Decimal("0.01")
    #: Smallest tradable quantity step. Executable size is rounded *down* to a multiple
    #: of this, because rounding up would claim a fill the venue would not accept.
    min_quantity_increment: Decimal = Decimal("0.01")
    fee_config: FeeConfig
    is_active: bool = True
    is_tradable: bool = True
    settlement_asset: SettlementAsset = SettlementAsset.USD
    raw_metadata: dict[str, object] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=_utcnow)

    @property
    def settlement_fingerprint(self) -> str:
        """Versioned hash of every field a human weighs when judging equivalence.

        An approval is a statement that two contracts settle identically. Hashing only
        the prose was too narrow: a venue could move the close time, add an outcome, or
        change the settlement currency without touching a word of the rules text, and the
        approval would survive unchanged. Everything that can decide *who gets paid* is
        therefore in the fingerprint.

        The :data:`SETTLEMENT_FINGERPRINT_VERSION` prefix is deliberate. Bumping it
        invalidates every stored approval, which is the correct outcome: those approvals
        were granted against a weaker definition of "unchanged" and deserve re-review.
        """
        close = self.close_time.isoformat() if self.close_time else None
        return rules_hash(
            SETTLEMENT_FINGERPRINT_VERSION,
            self.title,
            self.rules_text,
            self.settlement_source,
            # Cutoff, expressed as an absolute instant so a timezone change is visible.
            close,
            # Outcome set: an added or renamed outcome changes what "YES" means.
            "|".join(self.outcomes),
            # USD and USDC do not settle alike.
            self.settlement_asset.value,
        )

    @property
    def rules_hash(self) -> str:
        """Backwards-compatible alias for :attr:`settlement_fingerprint`."""
        return self.settlement_fingerprint


class RuleDifference(_Frozen):
    """One structured difference between two venues' contract rules."""

    dimension: str
    kalshi_value: str
    polymarket_value: str
    #: True when this difference can change which side settles in the money.
    is_material: bool
    explanation: str


class MarketPair(_Frozen):
    """A proposed or approved Kalshi<->Polymarket equivalence."""

    id: str
    kalshi_market_id: str
    polymarket_market_id: str
    status: MatchStatus = MatchStatus.SUGGESTED
    semantic_score: Decimal = ZERO
    manually_approved: bool = False
    reviewer_notes: str = ""
    rule_differences: tuple[RuleDifference, ...] = ()
    kalshi_rules_hash_at_approval: str | None = None
    polymarket_rules_hash_at_approval: str | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    #: True when Polymarket YES corresponds to Kalshi NO (inverted outcome mapping).
    outcomes_inverted: bool = False

    @property
    def is_tradable_pair(self) -> bool:
        """Only an explicitly approved pair may produce a ``confirmed`` opportunity."""
        return self.status is MatchStatus.APPROVED and self.manually_approved

    @property
    def label(self) -> OpportunityLabel:
        return (
            OpportunityLabel.CONFIRMED
            if self.is_tradable_pair
            else OpportunityLabel.RESEARCH_CANDIDATE
        )

    @property
    def has_material_differences(self) -> bool:
        return any(d.is_material for d in self.rule_differences)


# ------------------------------------------------------------------------ orderbooks


class BookLevel(_Frozen):
    """One aggregated price level."""

    price: Decimal
    quantity: Decimal

    @field_validator("price")
    @classmethod
    def _price_grid(cls, v: Decimal) -> Decimal:
        return quantize_price(v)

    @field_validator("quantity")
    @classmethod
    def _qty_grid(cls, v: Decimal) -> Decimal:
        return quantize_qty(v)


class OrderBookSide(_Frozen):
    """Bids and asks for a single outcome (YES or NO) of one venue market."""

    outcome: Outcome
    #: Descending by price: best bid first.
    bids: tuple[BookLevel, ...] = ()
    #: Ascending by price: best ask first.
    asks: tuple[BookLevel, ...] = ()

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None


class OrderBook(_Frozen):
    """Normalized two-sided book for one venue market.

    Kalshi publishes bids only; the adapter synthesizes the asks (a NO bid at $0.40 is a
    YES ask at $0.60) so that downstream code never has to know which venue it came from.
    """

    venue: Venue
    market_id: str
    yes: OrderBookSide
    no: OrderBookSide
    exchange_timestamp: datetime | None = None
    received_at: datetime = Field(default_factory=_utcnow)
    sequence: int | None = None
    is_stale: bool = False
    is_complete: bool = True

    def side(self, outcome: Outcome) -> OrderBookSide:
        return self.yes if outcome is Outcome.YES else self.no

    def age_ms(self, now: datetime | None = None) -> int:
        """Age measured from local receipt time.

        Exchange timestamps are not trusted for staleness because venue clock skew would
        otherwise make a stale book look fresh.
        """
        reference = now or _utcnow()
        return max(0, int((reference - self.received_at).total_seconds() * 1000))


# ----------------------------------------------------------------------- opportunity


class LegPlan(_Frozen):
    """The executable plan for one leg, derived by walking real book depth."""

    venue: Venue
    market_id: str
    outcome: Outcome
    quantity: Decimal
    #: Volume-weighted average *executable* price. Never a midpoint.
    vwap: Decimal
    worst_price: Decimal
    levels_consumed: int
    gross_cost: Decimal
    fees: Decimal
    settlement_asset: SettlementAsset
    book_age_ms: int


class ExecutionPlan(_Frozen):
    direction: ArbDirection
    kalshi_leg: LegPlan
    polymarket_leg: LegPlan


class Opportunity(_Frozen):
    """A detected opportunity, with every cost component broken out."""

    pair_id: str
    direction: ArbDirection
    label: OpportunityLabel
    requested_quantity: Decimal
    executable_quantity: Decimal
    kalshi_leg: LegPlan
    polymarket_leg: LegPlan
    gross_acquisition_cost: Decimal
    kalshi_fees: Decimal
    polymarket_fees: Decimal
    slippage_allowance: Decimal
    operational_cost_allowance: Decimal
    usdc_conversion_allowance: Decimal
    expected_payout: Decimal
    net_profit: Decimal
    return_on_capital: Decimal
    gross_spread: Decimal
    risk_flags: tuple[RiskFlag, ...] = ()
    detected_at: datetime = Field(default_factory=_utcnow)

    @property
    def is_confirmed(self) -> bool:
        return self.label is OpportunityLabel.CONFIRMED

    @property
    def dedup_key(self) -> str:
        return f"{self.pair_id}:{self.direction.value}"


class RejectedOpportunity(_Frozen):
    """A candidate that did not qualify, retained so the dashboard can explain why."""

    pair_id: str
    direction: ArbDirection
    reason: RejectionReason
    detail: str = ""
    detected_at: datetime = Field(default_factory=_utcnow)
