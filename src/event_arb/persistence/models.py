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
        UniqueConstraint("venue", "venue_market_id", name="uq_venue_market"),
        Index("ix_venue_markets_close_time", "close_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
    venue_market_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    rules_text: Mapped[str] = mapped_column(Text, default="")
    rules_hash: Mapped[str] = mapped_column(String(64))
    outcomes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_source: Mapped[str] = mapped_column(Text, default="")
    settlement_asset: Mapped[str] = mapped_column(String(8), default="USD")
    tick_size: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0.01"))
    fee_taker_rate: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    fee_maker_rate: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    fee_multiplier: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("1"))
    fee_source: Mapped[str] = mapped_column(String(128), default="default")
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
    status: Mapped[str] = mapped_column(String(32), default="suggested")
    semantic_score: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    manually_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcomes_inverted: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_notes: Mapped[str] = mapped_column(Text, default="")
    rule_differences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kalshi_rules_hash_at_approval: Mapped[str | None] = mapped_column(String(64))
    polymarket_rules_hash_at_approval: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    opportunities: Mapped[list[OpportunityRow]] = relationship(back_populates="pair")


class OrderBookSnapshotRow(Base):
    """Retained for replay and audit. See docs/operations.md for retention policy."""

    __tablename__ = "orderbook_snapshots"
    __table_args__ = (Index("ix_books_venue_market_time", "venue", "market_id", "received_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
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
