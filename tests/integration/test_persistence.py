"""Persistence: exact Decimal round-tripping and schema creation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from event_arb.persistence.database import Database
from event_arb.persistence.models import (
    AuditLogRow,
    MarketPairRow,
    OpportunityRow,
    VenueMarketRow,
)


async def test_schema_creates_cleanly(database: Database) -> None:
    async with database.session() as session:
        assert (await session.execute(select(VenueMarketRow))).all() == []


async def test_decimals_round_trip_exactly(database: Database) -> None:
    """SQLite has no exact decimal type; storing money as text is what keeps it exact."""
    tricky = [Decimal("0.0001"), Decimal("0.07"), Decimal("1.7325"), Decimal("93.0000")]
    async with database.session() as session:
        session.add(
            VenueMarketRow(
                venue="KALSHI",
                venue_market_id="K1",
                title="t",
                rules_hash="h",
                tick_size=tricky[0],
                fee_taker_rate=tricky[1],
                fee_maker_rate=tricky[2],
                fee_multiplier=tricky[3],
            )
        )
    async with database.session() as session:
        row = (await session.execute(select(VenueMarketRow))).scalar_one()
        assert [row.tick_size, row.fee_taker_rate, row.fee_maker_rate, row.fee_multiplier] == tricky
        assert all(isinstance(v, Decimal) for v in (row.tick_size, row.fee_taker_rate))


async def test_float_is_rejected_at_the_persistence_boundary(database: Database) -> None:
    """A float reaching the DB would silently lose exactness; fail loudly instead."""
    import pytest

    with pytest.raises(Exception):  # noqa: B017 - driver wraps the TypeError
        async with database.session() as session:
            session.add(
                VenueMarketRow(
                    venue="KALSHI",
                    venue_market_id="K2",
                    title="t",
                    rules_hash="h",
                    tick_size=0.01,  # type: ignore[arg-type]
                )
            )


async def test_pair_and_opportunity_relationship(database: Database) -> None:
    async with database.session() as session:
        session.add(
            MarketPairRow(
                id="pair1",
                kalshi_market_id="K1",
                polymarket_market_id="P1",
                status="approved",
                manually_approved=True,
                semantic_score=Decimal("0.78"),
                kalshi_rules_hash_at_approval="abc",
                polymarket_rules_hash_at_approval="def",
            )
        )
    async with database.session() as session:
        session.add(
            OpportunityRow(
                pair_id="pair1",
                direction="A_KALSHI_YES_POLY_NO",
                label="confirmed",
                executable_quantity=Decimal("100"),
                gross_acquisition_cost=Decimal("93.0000"),
                kalshi_fees=Decimal("1.74"),
                polymarket_fees=Decimal("0.9424"),
                slippage_allowance=Decimal("0.4650"),
                operational_cost_allowance=Decimal("0.1000"),
                usdc_conversion_allowance=Decimal("0.1000"),
                expected_payout=Decimal("100.0000"),
                net_profit=Decimal("3.6526"),
                return_on_capital=Decimal("3.9275"),
                risk_flags={"flags": ["non_atomic_execution"]},
            )
        )
    async with database.session() as session:
        row = (await session.execute(select(OpportunityRow))).scalar_one()
        assert row.net_profit == Decimal("3.6526")
        assert row.label == "confirmed"


async def test_audit_rows_are_append_only_in_practice(database: Database) -> None:
    async with database.session() as session:
        session.add(
            AuditLogRow(
                actor="reviewer@example.com",
                action="pair.approved",
                subject="pair1",
                detail={"notes": "verified"},
                created_at=datetime.now(UTC),
            )
        )
    async with database.session() as session:
        rows = (await session.execute(select(AuditLogRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].action == "pair.approved"
