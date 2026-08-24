"""SQLAlchemy 2.x ORM models.

Money is stored via :class:`DecimalString`, which serializes ``Decimal`` as text. SQLite
has no exact decimal type and silently round-trips ``NUMERIC`` through binary float,
which would defeat the entire fixed-point discipline. Text storage is exact on both
SQLite and PostgreSQL, and the comparisons this application performs on stored money are
equality and ordering by id/time, not SQL-side arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "AuditLogRow",
    "Base",
    "DecimalString",
    "MarketPairRow",
    "OpportunityRow",
    "OrderBookSnapshotRow",
    "PaperTradeRow",
    "SportsbookMappingRow",
    "SportsbookQuoteRow",
    "VenueMarketRow",
]


class DecimalString(TypeDecorator[Decimal]):
    """Exact ``Decimal`` persistence as text."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"expected Decimal, got {type(value).__name__}")
        return format(value, "f")

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        Decimal: DecimalString,
        dict[str, Any]: JSON,
    }


class VenueMarketRow(Base):
    __tablename__ = "venue_markets"
    __table_args__ = (
        # Product is part of market identity: two Polymarket products share a venue
        # value, so (venue, market_id) alone stopped meaning "one row per market".
        UniqueConstraint("venue", "product", "venue_market_id", name="uq_venue_market"),
        Index("ix_venue_markets_close_time", "close_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
    #: VenueProduct value. "none" for single-product venues such as Kalshi.
    product: Mapped[str] = mapped_column(String(32), default="none")
    venue_market_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    rules_text: Mapped[str] = mapped_column(Text, default="")
    rules_hash: Mapped[str] = mapped_column(String(64))
    outcomes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_source: Mapped[str] = mapped_column(Text, default="")
    settlement_asset: Mapped[str] = mapped_column(String(8), default="USD")
    settlement_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tick_size: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0.01"))
    fee_taker_rate: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    fee_maker_rate: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    fee_multiplier: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("1"))
    fee_source: Mapped[str] = mapped_column(String(128), default="default")
    #: FeePolicy value. Rows written before this column existed are "unknown", which the
    #: dispatcher refuses to price — fresh venue metadata must be fetched first.
    fee_policy: Mapped[str] = mapped_column(String(48), default="unknown")
    fee_maker_rebate_rate: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_tradable: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MarketPairRow(Base):
    __tablename__ = "market_pairs"
    __table_args__ = (UniqueConstraint("kalshi_market_id", "polymarket_market_id", name="uq_pair"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kalshi_market_id: Mapped[str] = mapped_column(String(255))
    polymarket_market_id: Mapped[str] = mapped_column(String(255))
    #: Which Polymarket product the second leg refers to. Rows predating product identity
    #: backfill to Global, the conservative reading.
    polymarket_product: Mapped[str] = mapped_column(String(32), default="polymarket_global")
    status: Mapped[str] = mapped_column(String(32), default="suggested")
    semantic_score: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    manually_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcomes_inverted: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_notes: Mapped[str] = mapped_column(Text, default="")
    rule_differences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Reviewer-verified resolution authority per leg, stored so the approval can be
    #: re-verified after restart rather than resting on a claim nobody kept.
    kalshi_reviewed_source: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    polymarket_reviewed_source: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    kalshi_rules_hash_at_approval: Mapped[str | None] = mapped_column(String(64))
    polymarket_rules_hash_at_approval: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    opportunities: Mapped[list[OpportunityRow]] = relationship(back_populates="pair")


class OrderBookSnapshotRow(Base):
    """Retained for replay and audit. See docs/operations.md for retention policy."""

    __tablename__ = "orderbook_snapshots"
    __table_args__ = (
        # Product is part of the key: without it two products' histories interleave
        # under one market id and neither can be read back reliably.
        Index("ix_books_venue_market_time", "venue", "product", "market_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
    product: Mapped[str] = mapped_column(String(32), default="none")
    market_id: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sequence: Mapped[int | None] = mapped_column(Integer)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)


class OpportunityRow(Base):
    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opportunities_detected_at", "detected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[str] = mapped_column(ForeignKey("market_pairs.id"))
    direction: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(32))
    executable_quantity: Mapped[Decimal] = mapped_column(DecimalString)
    gross_acquisition_cost: Mapped[Decimal] = mapped_column(DecimalString)
    kalshi_fees: Mapped[Decimal] = mapped_column(DecimalString)
    polymarket_fees: Mapped[Decimal] = mapped_column(DecimalString)
    slippage_allowance: Mapped[Decimal] = mapped_column(DecimalString)
    operational_cost_allowance: Mapped[Decimal] = mapped_column(DecimalString)
    usdc_conversion_allowance: Mapped[Decimal] = mapped_column(DecimalString)
    expected_payout: Mapped[Decimal] = mapped_column(DecimalString)
    net_profit: Mapped[Decimal] = mapped_column(DecimalString)
    return_on_capital: Mapped[Decimal] = mapped_column(DecimalString)
    risk_flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    pair: Mapped[MarketPairRow] = relationship(back_populates="opportunities")


class PaperTradeRow(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    opportunity_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    orders: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    unhedged_quantity: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    settlement_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SportsbookQuoteRow(Base):
    """Stored sportsbook quotes.

    Retention is bounded like every other ingested dataset. Provider licences govern how
    long third-party odds may be retained, and that has not been verified for any provider
    here, so the conservative posture applies by default.
    """

    __tablename__ = "sportsbook_quotes"
    __table_args__ = (
        Index("ix_sb_quotes_lookup", "provider", "provider_event_id", "received_at"),
        Index("ix_sb_quotes_key", "quote_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    sportsbook: Mapped[str] = mapped_column(String(64))
    provider_event_id: Mapped[str] = mapped_column(String(128))
    sportsbook_event_id: Mapped[str | None] = mapped_column(String(128))
    provider_market_id: Mapped[str] = mapped_column(String(128))
    market_type: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(32), default="full_game")
    sport: Mapped[str] = mapped_column(String(64), default="")
    league: Mapped[str] = mapped_column(String(64), default="")
    selection: Mapped[str] = mapped_column(String(255))
    normalized_selection: Mapped[str] = mapped_column(String(255), default="")
    line: Mapped[Decimal | None] = mapped_column(DecimalString)
    odds: Mapped[Decimal] = mapped_column(DecimalString)
    odds_format: Mapped[str] = mapped_column(String(16), default="american")
    decimal_odds: Mapped[Decimal] = mapped_column(DecimalString)
    #: NULL means the provider published no limit. It does not mean "unlimited", and it
    #: must never be backfilled with an assumed number.
    max_stake: Mapped[Decimal | None] = mapped_column(DecimalString)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    quote_key: Mapped[str] = mapped_column(String(512))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SportsbookMappingRow(Base):
    """A proposed or approved sportsbook-quote to exchange-contract mapping.

    Separate from ``market_pairs`` on purpose. An exchange pair maps two order books; this
    maps a book's price to a contract, which has different approval dimensions (overtime,
    push, grading source) and must never be restorable as an exchange approval.
    """

    __tablename__ = "sportsbook_mappings"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_market_id",
            "selection",
            "exchange_venue",
            "exchange_market_id",
            "exchange_outcome",
            name="uq_sb_mapping",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    sportsbook: Mapped[str] = mapped_column(String(64))
    provider_event_id: Mapped[str] = mapped_column(String(128))
    provider_market_id: Mapped[str] = mapped_column(String(128))
    selection: Mapped[str] = mapped_column(String(255))
    exchange_venue: Mapped[str] = mapped_column(String(32))
    exchange_market_id: Mapped[str] = mapped_column(String(255))
    exchange_outcome: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), default="suggested")
    manually_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_notes: Mapped[str] = mapped_column(Text, default="")
    rule_differences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sportsbook_fingerprint_at_approval: Mapped[str | None] = mapped_column(String(64))
    exchange_fingerprint_at_approval: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLogRow(Base):
    """Append-only audit trail. Rows are never updated or deleted by application code."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
