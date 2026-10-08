"""Repositories mapping domain objects to and from the ORM.

Two rules shape this layer.

**Money stays exact.** Decimals are stored as text (see
:class:`~event_arb.persistence.models.DecimalString`) and are never round-tripped through
a float on the way in or out.

**Approval is never resurrected blindly.** A stored approval is only as good as the rules
it was granted against. :meth:`MarketPairRepository.load_all` returns pairs exactly as
stored; it is :func:`restore_pairs` that re-checks each approval against the *current*
rules text and suspends anything whose hash no longer matches. A restart must not quietly
reinstate an approval for a contract the venue has since rewritten.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from event_arb.domain.enums import FeePolicy, MatchStatus, Venue, VenueProduct
from event_arb.domain.models import (
    FeeConfig,
    MarketPair,
    Opportunity,
    OrderBook,
    ReviewedSource,
    RuleDifference,
    VenueMarket,
)
from event_arb.persistence.models import (
    AuditLogRow,
    MarketPairRow,
    OpportunityRow,
    OrderBookSnapshotRow,
    PaperLedgerEntryRow,
    PaperTradeRow,
    VenueMarketRow,
)

__all__ = [
    "AuditLogRepository",
    "MarketPairRepository",
    "OpportunityRepository",
    "OrderBookSnapshotRepository",
    "PaperLedgerRepository",
    "PaperTradeRepository",
    "VenueMarketRepository",
]


def _json_safe(value: Any) -> Any:
    """Coerce a value into something the JSON column can hold, without losing precision."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- markets


class VenueMarketRepository:
    """Stores normalized venue markets, including the rules hash used for approvals."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, market: VenueMarket) -> None:
        existing = await self._session.scalar(
            select(VenueMarketRow).where(
                VenueMarketRow.venue == market.venue.value,
                VenueMarketRow.product == market.product.value,
                VenueMarketRow.venue_market_id == market.venue_market_id,
            )
        )
        row = existing or VenueMarketRow(
            venue=market.venue.value,
            product=market.product.value,
            venue_market_id=market.venue_market_id,
        )
        row.title = market.title
        row.rules_text = market.rules_text
        row.rules_hash = market.rules_hash
        row.outcomes = {"outcomes": list(market.outcomes)}
        row.close_time = market.close_time
        row.settlement_source = market.settlement_source
        row.settlement_asset = market.settlement_asset.value
        row.settlement_deadline = market.settlement_deadline
        row.tick_size = market.tick_size
        row.fee_taker_rate = market.fee_config.taker_rate
        row.fee_maker_rate = market.fee_config.maker_rate
        row.fee_multiplier = market.fee_config.multiplier
        row.fee_source = market.fee_config.source
        row.fee_policy = market.fee_config.fee_policy.value
        row.fee_maker_rebate_rate = market.fee_config.maker_rebate_rate
        row.is_active = market.is_active
        row.is_tradable = market.is_tradable
        row.raw_metadata = _json_safe(market.raw_metadata)
        row.last_updated = market.last_updated
        if existing is None:
            self._session.add(row)

    async def upsert_many(self, markets: Iterable[VenueMarket]) -> None:
        for market in markets:
            await self.upsert(market)

    async def load_all(self) -> dict[tuple[Venue, VenueProduct, str], VenueMarket]:
        """Keyed by (venue, product, id).

        Keying on (venue, id) alone collapsed the two Polymarket products: both rows
        persisted correctly but one silently overwrote the other in memory, so a Global
        market could answer a US lookup.
        """
        rows = (await self._session.scalars(select(VenueMarketRow))).all()
        return {
            (Venue(r.venue), VenueProduct(r.product), r.venue_market_id): self._to_domain(r)
            for r in rows
        }

    @staticmethod
    def _to_domain(row: VenueMarketRow) -> VenueMarket:
        from event_arb.domain.enums import SettlementAsset

        outcomes = row.outcomes.get("outcomes") if isinstance(row.outcomes, dict) else None
        return VenueMarket(
            venue=Venue(row.venue),
            venue_market_id=row.venue_market_id,
            title=row.title,
            rules_text=row.rules_text,
            outcomes=tuple(outcomes) if outcomes else ("YES", "NO"),
            close_time=row.close_time,
            settlement_source=row.settlement_source,
            tick_size=row.tick_size,
            fee_config=FeeConfig(
                venue=Venue(row.venue),
                taker_rate=row.fee_taker_rate,
                maker_rate=row.fee_maker_rate,
                multiplier=row.fee_multiplier,
                maker_rebate_rate=row.fee_maker_rebate_rate,
                fee_policy=FeePolicy(row.fee_policy),
                source=row.fee_source,
            ),
            is_active=row.is_active,
            is_tradable=row.is_tradable,
            settlement_asset=SettlementAsset(row.settlement_asset),
            product=VenueProduct(row.product),
            settlement_deadline=row.settlement_deadline,
            raw_metadata=dict(row.raw_metadata or {}),
            last_updated=row.last_updated,
        )


# ----------------------------------------------------------------------------- pairs


class MarketPairRepository:
    """Stores pairs together with their approval metadata and rule hashes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, pair: MarketPair) -> None:
        existing = await self._session.get(MarketPairRow, pair.id)
        row = existing or MarketPairRow(id=pair.id)
        row.kalshi_market_id = pair.kalshi_market_id
        row.polymarket_market_id = pair.polymarket_market_id
        row.polymarket_product = pair.polymarket_product.value
        row.status = pair.status.value
        row.semantic_score = pair.semantic_score
        row.manually_approved = pair.manually_approved
        row.outcomes_inverted = pair.outcomes_inverted
        row.reviewer_notes = pair.reviewer_notes
        row.rule_differences = {
            "differences": [d.model_dump(mode="json") for d in pair.rule_differences]
        }
        row.kalshi_reviewed_source = (
            _json_safe(pair.kalshi_reviewed_source.model_dump(mode="json"))
            if pair.kalshi_reviewed_source
            else None
        )
        row.polymarket_reviewed_source = (
            _json_safe(pair.polymarket_reviewed_source.model_dump(mode="json"))
            if pair.polymarket_reviewed_source
            else None
        )
        row.kalshi_rules_hash_at_approval = pair.kalshi_rules_hash_at_approval
        row.polymarket_rules_hash_at_approval = pair.polymarket_rules_hash_at_approval
        row.approved_at = pair.approved_at
        row.approved_by = pair.approved_by
        if existing is None:
            self._session.add(row)

    async def save_many(self, pairs: Iterable[MarketPair]) -> None:
        for pair in pairs:
            await self.save(pair)

    async def load_all(self) -> dict[str, MarketPair]:
        rows = (await self._session.scalars(select(MarketPairRow))).all()
        return {r.id: self._to_domain(r) for r in rows}

    async def load_ids(self) -> set[str]:
        """Just the ids, for callers that only need to know what already exists."""
        return set((await self._session.scalars(select(MarketPairRow.id))).all())

    @staticmethod
    def _to_domain(row: MarketPairRow) -> MarketPair:
        raw = row.rule_differences or {}
        entries = raw.get("differences", []) if isinstance(raw, dict) else []
        return MarketPair(
            id=row.id,
            kalshi_market_id=row.kalshi_market_id,
            polymarket_market_id=row.polymarket_market_id,
            polymarket_product=VenueProduct(row.polymarket_product),
            status=MatchStatus(row.status),
            semantic_score=row.semantic_score,
            manually_approved=row.manually_approved,
            reviewer_notes=row.reviewer_notes,
            rule_differences=tuple(RuleDifference(**e) for e in entries),
            kalshi_reviewed_source=(
                ReviewedSource(**row.kalshi_reviewed_source) if row.kalshi_reviewed_source else None
            ),
            polymarket_reviewed_source=(
                ReviewedSource(**row.polymarket_reviewed_source)
                if row.polymarket_reviewed_source
                else None
            ),
            kalshi_rules_hash_at_approval=row.kalshi_rules_hash_at_approval,
            polymarket_rules_hash_at_approval=row.polymarket_rules_hash_at_approval,
            approved_at=row.approved_at,
            approved_by=row.approved_by,
            outcomes_inverted=row.outcomes_inverted,
        )


# --------------------------------------------------------------------- opportunities


class OpportunityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, opportunity: Opportunity, *, alert_suppressed: bool = False) -> None:
        """Store one detection.

        Suppressed detections keep every economic column but **not** the full ``snapshot``
        JSON. That blob is ~1.2 kB and is near-identical across consecutive sightings of the
        same opportunity; at a 5 s cycle it projects to over a gigabyte per month for two
        detections per cycle. What a suppressed row is for — how long an opportunity
        persisted, at what size and profit — lives in the columns. The per-cycle leg detail
        is deliberately not retained for repeats; the announced row for that dedup key has
        it.
        """
        self._session.add(
            OpportunityRow(
                pair_id=opportunity.pair_id,
                alert_suppressed=alert_suppressed,
                direction=opportunity.direction.value,
                label=opportunity.label.value,
                executable_quantity=opportunity.executable_quantity,
                gross_acquisition_cost=opportunity.gross_acquisition_cost,
                kalshi_fees=opportunity.kalshi_fees,
                polymarket_fees=opportunity.polymarket_fees,
                slippage_allowance=opportunity.slippage_allowance,
                operational_cost_allowance=opportunity.operational_cost_allowance,
                usdc_conversion_allowance=opportunity.usdc_conversion_allowance,
                expected_payout=opportunity.expected_payout,
                net_profit=opportunity.net_profit,
                return_on_capital=opportunity.return_on_capital,
                risk_flags={"flags": [f.value for f in opportunity.risk_flags]},
                snapshot=(
                    {} if alert_suppressed else _json_safe(opportunity.model_dump(mode="json"))
                ),
                detected_at=opportunity.detected_at,
            )
        )

    async def prune(self, older_than: timedelta, now: datetime | None = None) -> int:
        """Delete detections beyond the retention window. Returns rows removed."""
        cutoff = (now or datetime.now(UTC)) - older_than
        stale = (
            await self._session.scalars(
                select(OpportunityRow.id).where(OpportunityRow.detected_at < cutoff)
            )
        ).all()
        if not stale:
            return 0
        await self._session.execute(delete(OpportunityRow).where(OpportunityRow.id.in_(stale)))
        return len(stale)

    async def recent(self, limit: int = 100) -> Sequence[OpportunityRow]:
        return (
            await self._session.scalars(
                select(OpportunityRow).order_by(OpportunityRow.detected_at.desc()).limit(limit)
            )
        ).all()

    async def count(self) -> int:
        return len((await self._session.scalars(select(OpportunityRow.id))).all())


# ---------------------------------------------------------------------- paper trades


class PaperTradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, trade: Any) -> None:
        """Persist a :class:`~event_arb.paper.executor.PaperTrade`, keyed by its own id.

        An **upsert**, not an insert. The same trade reaches here more than once by
        design — settlement rewrites it, and a restarted loop may finish work it had
        already persisted — and a second row would double the exposure that restore
        reconstructs.
        """
        payload = trade.as_dict()
        existing = await self._session.scalar(
            select(PaperTradeRow).where(PaperTradeRow.trade_id == trade.id)
        )
        row = existing or PaperTradeRow(trade_id=trade.id)
        row.pair_id = trade.pair_id
        row.direction = trade.direction.value
        row.status = trade.status.value
        row.execution_status = trade.execution_status.value
        row.opportunity_snapshot = _json_safe(trade.opportunity.model_dump(mode="json"))
        row.orders = _json_safe(payload)
        row.realized_pnl = trade.realized_pnl_if_hedged
        row.unhedged_quantity = trade.unhedged_quantity
        row.settlement_result = (
            _json_safe(trade.settlement_result) if trade.settlement_result else None
        )
        row.created_at = trade.created_at
        row.settled_at = trade.settled_at
        if existing is None:
            self._session.add(row)

    async def prune(self, older_than: timedelta, now: datetime | None = None) -> int:
        """Delete aged trades whose positions are **resolved**. Returns rows removed.

        Retention used to delete purely by age. That removed the record of positions that
        were still open — a leg-2 failure sitting unhedged for longer than the window
        vanished from the exposure report while the money stayed spent. A trade is only
        prunable once it has settled and carries no unhedged remainder; anything else is
        kept regardless of age, because it is still a live position.

        Cash is unaffected either way: balances come from the ledger, which is compacted
        rather than deleted. See :meth:`PaperLedgerRepository.compact`.
        """
        cutoff = (now or datetime.now(UTC)) - older_than
        candidates = (
            await self._session.scalars(
                select(PaperTradeRow).where(
                    PaperTradeRow.created_at < cutoff,
                    PaperTradeRow.settled_at.is_not(None),
                )
            )
        ).all()
        # Compared as a Decimal, not as stored text: "0", "0.00" and "0.0000" are the
        # same number and a SQL string comparison would keep or drop rows by formatting.
        stale = [row.id for row in candidates if row.unhedged_quantity == Decimal("0")]
        if not stale:
            return 0
        await self._session.execute(delete(PaperTradeRow).where(PaperTradeRow.id.in_(stale)))
        return len(stale)

    async def recent(self, limit: int = 100) -> Sequence[PaperTradeRow]:
        return (
            await self._session.scalars(
                select(PaperTradeRow).order_by(PaperTradeRow.created_at.desc()).limit(limit)
            )
        ).all()

    async def load_all(self, limit: int | None = None) -> Sequence[PaperTradeRow]:
        """Every stored trade, oldest first, for portfolio reconstruction."""
        statement = select(PaperTradeRow).order_by(PaperTradeRow.created_at.asc())
        if limit is not None:
            statement = statement.limit(limit)
        return (await self._session.scalars(statement)).all()

    async def get(self, trade_id: str) -> PaperTradeRow | None:
        row: PaperTradeRow | None = await self._session.scalar(
            select(PaperTradeRow).where(PaperTradeRow.trade_id == trade_id)
        )
        return row

    async def open_trades(self) -> Sequence[PaperTradeRow]:
        """Trades with no recorded settlement — the ones an operator has to settle."""
        return (
            await self._session.scalars(
                select(PaperTradeRow)
                .where(PaperTradeRow.settled_at.is_(None))
                .order_by(PaperTradeRow.created_at.asc())
            )
        ).all()

    async def count(self) -> int:
        return len((await self._session.scalars(select(PaperTradeRow.id))).all())


# --------------------------------------------------------------------- paper ledger


class PaperLedgerRepository:
    """Durable store for the append-only cash ledger.

    Writes are keyed by the entry's own deterministic id, so persisting the same movement
    twice is a no-op rather than a double debit — the same property the in-memory ledger
    has, held at the storage boundary too.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_many(self, entries: Iterable[Any]) -> int:
        """Persist ledger entries, skipping ones already stored. Returns how many landed."""
        written = 0
        for entry in entries:
            existing = await self._session.get(PaperLedgerEntryRow, entry.entry_id)
            if existing is not None:
                continue
            self._session.add(
                PaperLedgerEntryRow(
                    entry_id=entry.entry_id,
                    account=entry.account.key,
                    kind=entry.kind.value,
                    amount=entry.amount,
                    trade_id=entry.trade_id,
                    detail=entry.detail,
                    created_at=entry.created_at,
                )
            )
            written += 1
        return written

    async def load_all(self) -> list[Any]:
        """Every stored entry, oldest first, as domain :class:`LedgerEntry` objects."""
        from event_arb.paper.accounts import AccountId
        from event_arb.paper.ledger import EntryKind, LedgerEntry

        rows = (
            await self._session.scalars(
                select(PaperLedgerEntryRow).order_by(PaperLedgerEntryRow.created_at.asc())
            )
        ).all()
        return [
            LedgerEntry(
                entry_id=row.entry_id,
                account=AccountId.parse(row.account),
                kind=EntryKind(row.kind),
                amount=row.amount,
                trade_id=row.trade_id,
                detail=row.detail,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def compact(
        self,
        older_than: timedelta,
        keep_trade_ids: frozenset[str],
        now: datetime | None = None,
    ) -> int:
        """Bound the ledger **without changing a balance**. Returns entries removed.

        Aged entries are summed per account and replaced by one carry-forward row, so the
        derived balance is identical before and after. Entries belonging to
        ``keep_trade_ids`` — trades whose position is still open — and reservation entries
        are never compacted: the first because their detail reconstructs live exposure,
        the second because a hold and its release are a matched pair.
        """
        from event_arb.paper.accounts import AccountId
        from event_arb.paper.ledger import EntryKind

        cutoff = (now or datetime.now(UTC)) - older_than
        reservation_kinds = {EntryKind.RESERVATION.value, EntryKind.RELEASE.value}
        rows = (
            await self._session.scalars(
                select(PaperLedgerEntryRow).where(PaperLedgerEntryRow.created_at < cutoff)
            )
        ).all()
        removable = [
            row
            for row in rows
            if row.kind not in reservation_kinds
            and row.kind != EntryKind.CARRY_FORWARD.value
            and (not row.trade_id or row.trade_id not in keep_trade_ids)
        ]
        if not removable:
            return 0

        carried: dict[str, Decimal] = {}
        for row in removable:
            carried[row.account] = carried.get(row.account, Decimal("0")) + row.amount
        await self._session.execute(
            delete(PaperLedgerEntryRow).where(
                PaperLedgerEntryRow.entry_id.in_([row.entry_id for row in removable])
            )
        )
        stamp = cutoff.isoformat()
        for account_key, amount in sorted(carried.items()):
            AccountId.parse(account_key)
            entry_id = f"carry:{stamp}:{account_key}"
            existing = await self._session.get(PaperLedgerEntryRow, entry_id)
            if existing is not None:
                existing.amount = existing.amount + amount
                continue
            self._session.add(
                PaperLedgerEntryRow(
                    entry_id=entry_id,
                    account=account_key,
                    kind=EntryKind.CARRY_FORWARD.value,
                    amount=amount,
                    trade_id="",
                    detail=f"net of {len(removable)} ledger entries pruned before {stamp}",
                    created_at=cutoff,
                )
            )
        return len(removable)

    async def count(self) -> int:
        return len((await self._session.scalars(select(PaperLedgerEntryRow.entry_id))).all())


# ------------------------------------------------------------------------ order books


class OrderBookSnapshotRepository:
    """Bounded storage of book snapshots.

    Retention is deliberately finite. The Kalshi Developer Agreement §3.1 permits storing
    API data only to facilitate our own trading; an unbounded archive is not that. See
    docs/compliance.md.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, book: OrderBook) -> None:
        """Product comes from the book itself.

        It used to be a caller-supplied parameter defaulting to ``"none"``, which meant
        every Polymarket snapshot was filed under the wrong product unless the caller
        remembered to pass it — and no caller did.
        """
        self._session.add(
            OrderBookSnapshotRow(
                venue=book.venue.value,
                product=book.product.value,
                market_id=book.market_id,
                payload=_json_safe(json.loads(book.model_dump_json())),
                exchange_timestamp=book.exchange_timestamp,
                received_at=book.received_at,
                sequence=book.sequence,
                is_stale=book.is_stale,
            )
        )

    async def prune(self, older_than: timedelta, now: datetime | None = None) -> int:
        """Delete snapshots beyond the retention window. Returns rows removed."""
        cutoff = (now or datetime.now(UTC)) - older_than
        stale = (
            await self._session.scalars(
                select(OrderBookSnapshotRow.id).where(OrderBookSnapshotRow.received_at < cutoff)
            )
        ).all()
        if not stale:
            return 0
        await self._session.execute(
            delete(OrderBookSnapshotRow).where(OrderBookSnapshotRow.id.in_(stale))
        )
        return len(stale)

    async def count(self) -> int:
        return len((await self._session.scalars(select(OrderBookSnapshotRow.id))).all())


# -------------------------------------------------------------------------- audit log


class AuditLogRepository:
    """Append-only audit trail. There is deliberately no update or delete method."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self, actor: str, action: str, subject: str = "", detail: dict[str, Any] | None = None
    ) -> None:
        from event_arb.observability.logging import redact_value

        self._session.add(
            AuditLogRow(
                actor=actor,
                action=action,
                subject=subject,
                # Audit rows are read by humans and exported; redact before they land.
                detail=_json_safe(redact_value(detail or {})),
                created_at=datetime.now(UTC),
            )
        )

    async def recent(self, limit: int = 200) -> Sequence[AuditLogRow]:
        return (
            await self._session.scalars(
                select(AuditLogRow).order_by(AuditLogRow.created_at.desc()).limit(limit)
            )
        ).all()
