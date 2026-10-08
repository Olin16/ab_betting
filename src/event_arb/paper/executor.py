"""Simulated two-leg execution.

The execution is deliberately **sequential**, because that is what really happens: there
is no atomic cross-venue transaction between Kalshi and Polymarket. Leg 1 is sent, it
fills (or partially fills), and only then does leg 2 go out — into a book that has had
time to move.

This ordering is the whole point of paper trading here. The interesting outcomes are not
the clean double fills; they are:

- leg 1 fills, leg 2 fails entirely  -> fully unhedged directional exposure;
- leg 1 fills 100, leg 2 fills 60    -> 40 contracts unhedged;
- leg 2's price moved                -> the realized spread is worse than the detected one.

Every one of those is recorded on the resulting :class:`PaperTrade` rather than being
retried away, so the daily report reflects what the strategy would actually have suffered.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.config import PaperSettings
from event_arb.domain.enums import (
    ArbDirection,
    Outcome,
    PaperTradeStatus,
    SettlementAsset,
    Venue,
)
from event_arb.domain.models import Opportunity, OrderBook, VenueMarket
from event_arb.domain.money import CENT_EXP, ONE_DOLLAR, ZERO, ceil_to, quantize_price, quantize_qty
from event_arb.fees import taker_fee_for
from event_arb.observability import metrics
from event_arb.observability.logging import get_logger
from event_arb.opportunities.depth import Fill
from event_arb.orderbooks.freshness import FreshnessPolicy
from event_arb.paper.accounts import account_for
from event_arb.paper.evidence import EvidenceGrade, ProvidedBook, grade_leg2
from event_arb.paper.fills import SimulatedOrder, simulate_buy
from event_arb.venues.kalshi.fees import KalshiOrderFeeAccumulator

__all__ = [
    "ExecutionInterruptedError",
    "PaperExecutor",
    "PaperTrade",
    "SettlementOutcomes",
    "TradeCheckpoint",
    "settle",
]

log = get_logger(__name__)


class ExecutionInterruptedError(Exception):
    """Leg 1 filled and then the attempt was interrupted before leg 2 was tried.

    Carries the partially-executed trade, because the leg-1 fill is simulated exposure
    that already exists. Raising a bare error would discard it: the caller never receives
    the trade, so nothing records it, and the position silently ceases to have happened —
    the one direction of error that understates risk.

    The original ``cause`` is kept so the caller can re-raise it and leave cancellation
    semantics intact after it has made the exposure durable.
    """

    def __init__(self, trade: PaperTrade, cause: BaseException) -> None:
        super().__init__(f"execution interrupted after leg 1 of {trade.id}: {cause!r}")
        self.trade = trade
        self.cause = cause


#: Injected so tests can advance simulated time without sleeping. Returns the book *and*
#: how it was obtained, because a hedge result is only evidence if the venue was actually
#: re-observed after the latency elapsed. See :mod:`event_arb.paper.evidence`.
BookProvider = Callable[[Venue, str, int], Awaitable[ProvidedBook]]

#: Called once leg 1 has filled and **before** leg 2 is attempted, to make the exposure
#: leg 1 created durable. Catching an exception after leg 2's await is not a checkpoint:
#: it only runs when the process survives long enough to catch, so an abrupt death
#: between the legs leaves nothing behind. Raising from here aborts the attempt before
#: any further exposure is taken, which is the correct response to storage refusing it.
TradeCheckpoint = Callable[["PaperTrade"], Awaitable[None]]


@dataclass
class PaperTrade:
    """The full record of one simulated arbitrage attempt."""

    id: str
    pair_id: str
    direction: ArbDirection
    opportunity: Opportunity
    leg1: SimulatedOrder | None = None
    leg2: SimulatedOrder | None = None
    status: PaperTradeStatus = PaperTradeStatus.PENDING
    leg1_latency_ms: int = 0
    leg2_latency_ms: int = 0
    kalshi_fees: Decimal = ZERO
    polymarket_fees: Decimal = ZERO
    settlement_result: dict[str, object] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    settled_at: datetime | None = None
    notes: list[str] = field(default_factory=list)
    #: How much the leg-2 outcome is worth as evidence. A hedge success graded
    #: ``unchanged_reread`` says the book did not move in this sample — never that hedging
    #: survives adverse movement.
    leg2_evidence: EvidenceGrade = EvidenceGrade.UNCHANGED_REREAD
    #: What **execution** ended in, kept separately from :attr:`status`.
    #:
    #: ``status`` moves to ``settled`` when a settlement is recorded, and it used to be the
    #: only record of how the trade filled — so settling a partially-hedged trade erased it
    #: from the partially-hedged count and made the hedge-success rate improve purely
    #: because time had passed. Execution and settlement are different facts about a trade
    #: and are now stored as two.
    execution_status: PaperTradeStatus = PaperTradeStatus.PENDING

    # ---------------------------------------------------------------- lifecycle

    @property
    def is_settled(self) -> bool:
        """Settlement is recorded by a timestamp, not inferred from a status string."""
        return self.settled_at is not None

    def set_execution_status(self, status: PaperTradeStatus) -> None:
        """Record an execution outcome on both the live status and the durable one."""
        self.status = status
        self.execution_status = status

    # ------------------------------------------------------------------ exposure

    @property
    def hedged_quantity(self) -> Decimal:
        """Quantity covered on *both* legs — the only genuinely arbitraged size."""
        if self.leg1 is None or self.leg2 is None:
            return ZERO
        return quantize_qty(min(self.leg1.filled_quantity, self.leg2.filled_quantity))

    @property
    def unhedged_quantity(self) -> Decimal:
        """Quantity held on one venue with no offsetting position on the other.

        This is a naked directional position. It is the number that matters most in a
        paper-trading report, because it is the loss the strategy was not designed to take.
        """
        leg1_filled = self.leg1.filled_quantity if self.leg1 else ZERO
        leg2_filled = self.leg2.filled_quantity if self.leg2 else ZERO
        return quantize_qty(abs(leg1_filled - leg2_filled))

    @property
    def unhedged_venue(self) -> Venue | None:
        leg1_filled = self.leg1.filled_quantity if self.leg1 else ZERO
        leg2_filled = self.leg2.filled_quantity if self.leg2 else ZERO
        if leg1_filled == leg2_filled:
            return None
        return Venue.KALSHI if leg1_filled > leg2_filled else Venue.POLYMARKET

    @property
    def total_cost(self) -> Decimal:
        cost = ZERO
        for leg in (self.leg1, self.leg2):
            if leg is not None:
                cost += leg.gross_cost
        return quantize_price(cost)

    @property
    def total_fees(self) -> Decimal:
        return quantize_price(self.kalshi_fees + self.polymarket_fees)

    # ------------------------------------------------------------------- results

    @property
    def guaranteed_payout(self) -> Decimal:
        """Only the hedged portion pays $1.00 regardless of outcome."""
        return quantize_price(self.hedged_quantity * ONE_DOLLAR)

    @property
    def realized_pnl_if_hedged(self) -> Decimal:
        """P&L assuming the unhedged remainder settles worthless.

        This is the pessimistic view and the one reported by default: an unhedged leg is
        a coin flip, and a paper-trading report that assumes coin flips land favourably
        is worse than no report.
        """
        return quantize_price(self.guaranteed_payout - self.total_cost - self.total_fees)

    @property
    def realized_spread(self) -> Decimal:
        """Actual spread achieved, for comparison with the detected spread."""
        if self.hedged_quantity <= ZERO:
            return ZERO
        return quantize_price((self.guaranteed_payout - self.total_cost) / self.hedged_quantity)

    @property
    def slippage_vs_detected(self) -> Decimal:
        """How much worse execution was than detection predicted, per contract."""
        detected = self.opportunity
        if detected.executable_quantity <= ZERO or self.hedged_quantity <= ZERO:
            return ZERO
        expected_per_unit = (
            detected.expected_payout - detected.gross_acquisition_cost
        ) / detected.executable_quantity
        return quantize_price(expected_per_unit - self.realized_spread)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pair_id": self.pair_id,
            "direction": self.direction.value,
            "status": self.status.value,
            "execution_status": self.execution_status.value,
            "is_settled": self.is_settled,
            "hedged_quantity": str(self.hedged_quantity),
            "unhedged_quantity": str(self.unhedged_quantity),
            "unhedged_venue": self.unhedged_venue.value if self.unhedged_venue else None,
            "total_cost": str(self.total_cost),
            "total_fees": str(self.total_fees),
            "realized_pnl_if_hedged": str(self.realized_pnl_if_hedged),
            "detected_net_profit": str(self.opportunity.net_profit),
            "realized_spread": str(self.realized_spread),
            "slippage_vs_detected": str(self.slippage_vs_detected),
            "leg1_latency_ms": self.leg1_latency_ms,
            "leg2_latency_ms": self.leg2_latency_ms,
            "leg2_evidence": self.leg2_evidence.value,
            "created_at": self.created_at.isoformat(),
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "settlement_result": self.settlement_result,
            "notes": list(self.notes),
            # Enough to rebuild the trade after a restart. The derived figures above are
            # for humans and exports; these are the inputs they are derived from, so a
            # restored trade recomputes rather than trusting a stored total.
            "legs": {
                "leg1": _order_as_dict(self.leg1),
                "leg2": _order_as_dict(self.leg2),
            },
            "kalshi_fees": str(self.kalshi_fees),
            "polymarket_fees": str(self.polymarket_fees),
        }

    @classmethod
    def from_record(cls, payload: dict[str, object], opportunity: Opportunity) -> PaperTrade:
        """Rebuild a trade from its stored record.

        The opportunity comes from its own stored snapshot rather than from this payload,
        because the opportunity is what the legs are priced and accounted against. A trade
        whose record cannot be rebuilt raises rather than returning a partial object: a
        half-restored trade would understate exposure, which is the one direction that
        matters.
        """
        legs = payload.get("legs")
        legs = legs if isinstance(legs, dict) else {}
        status = PaperTradeStatus(str(payload["status"]))
        execution_status = PaperTradeStatus(
            str(payload.get("execution_status") or _execution_status_fallback(status))
        )
        settled_raw = payload.get("settled_at")
        notes = payload.get("notes")
        settlement = payload.get("settlement_result")
        return cls(
            id=str(payload["id"]),
            pair_id=str(payload["pair_id"]),
            direction=ArbDirection(str(payload["direction"])),
            opportunity=opportunity,
            leg1=_order_from_dict(legs.get("leg1"), Venue.KALSHI),
            leg2=_order_from_dict(legs.get("leg2"), Venue.POLYMARKET),
            status=status,
            execution_status=execution_status,
            leg1_latency_ms=int(str(payload.get("leg1_latency_ms") or 0)),
            leg2_latency_ms=int(str(payload.get("leg2_latency_ms") or 0)),
            kalshi_fees=Decimal(str(payload.get("kalshi_fees") or "0")),
            polymarket_fees=Decimal(str(payload.get("polymarket_fees") or "0")),
            settlement_result=settlement if isinstance(settlement, dict) else None,
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            settled_at=datetime.fromisoformat(str(settled_raw)) if settled_raw else None,
            notes=[str(n) for n in notes] if isinstance(notes, list) else [],
            leg2_evidence=EvidenceGrade(
                str(payload.get("leg2_evidence") or EvidenceGrade.UNCHANGED_REREAD.value)
            ),
        )


def _execution_status_fallback(status: PaperTradeStatus) -> str:
    """Read an execution status from a record written before the two were separated.

    Rows stored then kept only one status, so a settled trade's fill classification was
    already lost by the time it was written. ``pending`` is the honest answer — it says
    "not recorded" rather than inventing a hedge outcome that would land in the counts.
    """
    if status is PaperTradeStatus.SETTLED:
        return PaperTradeStatus.PENDING.value
    return status.value


def _order_as_dict(order: SimulatedOrder | None) -> dict[str, object] | None:
    if order is None:
        return None
    return {
        "venue": order.venue.value,
        "market_id": order.market_id,
        "outcome": order.outcome.value,
        "requested_quantity": str(order.requested_quantity),
        "filled_quantity": str(order.filled_quantity),
        "gross_cost": str(order.gross_cost),
        "limit_price": str(order.limit_price) if order.limit_price is not None else None,
        "client_order_id": order.client_order_id,
        "fills": [{"price": str(f.price), "quantity": str(f.quantity)} for f in order.fills],
    }


def _order_from_dict(raw: object, venue: Venue) -> SimulatedOrder | None:
    if not isinstance(raw, dict):
        return None
    fills_raw = raw.get("fills")
    fills = tuple(
        Fill(price=Decimal(str(f["price"])), quantity=Decimal(str(f["quantity"])))
        for f in (fills_raw if isinstance(fills_raw, list) else [])
        if isinstance(f, dict)
    )
    limit = raw.get("limit_price")
    return SimulatedOrder(
        venue=Venue(str(raw.get("venue") or venue.value)),
        market_id=str(raw.get("market_id", "")),
        outcome=Outcome(str(raw["outcome"])),
        requested_quantity=Decimal(str(raw.get("requested_quantity") or "0")),
        filled_quantity=Decimal(str(raw.get("filled_quantity") or "0")),
        fills=fills,
        gross_cost=Decimal(str(raw.get("gross_cost") or "0")),
        limit_price=Decimal(str(limit)) if limit is not None else None,
        client_order_id=str(raw.get("client_order_id", "")),
    )


class PaperExecutor:
    """Simulates two-leg execution against evolving order books."""

    def __init__(
        self,
        settings: PaperSettings,
        book_provider: BookProvider,
        kalshi_direct_member: bool = False,
        freshness: FreshnessPolicy | None = None,
        checkpoint: TradeCheckpoint | None = None,
    ) -> None:
        self._settings = settings
        self._book_provider = book_provider
        self._kalshi_direct_member = kalshi_direct_member
        self._freshness = freshness or FreshnessPolicy()
        self._checkpoint = checkpoint

    def _usable(
        self, provided: ProvidedBook, venue: Venue, market_id: str
    ) -> tuple[OrderBook | None, str]:
        """Validate a book at simulated arrival time.

        A book that the opportunity engine would have refused must not be filled against
        here either. Paper results feed the live-readiness gate in docs/operations.md, so
        a simulator that trades on stale or mismatched books lets that gate certify
        itself on evidence it should have thrown away.
        """
        book = provided.book
        if book is None:
            return None, f"no {venue.value} book at arrival time"
        if book.venue is not venue or book.market_id != market_id:
            return None, (
                f"book mismatch at arrival: expected {venue.value}/{market_id}, "
                f"got {book.venue.value}/{book.market_id}"
            )
        verdict = self._freshness.evaluate(book)
        if not verdict.is_fresh:
            return None, f"{venue.value} book unusable at arrival: {verdict.reason}"
        return book, ""

    async def execute(
        self,
        opportunity: Opportunity,
        kalshi_market: VenueMarket,
        polymarket_market: VenueMarket,
    ) -> PaperTrade:
        """Run one simulated arbitrage attempt, leg 1 then leg 2."""
        trade = PaperTrade(
            id=str(uuid.uuid4()),
            pair_id=opportunity.pair_id,
            direction=opportunity.direction,
            opportunity=opportunity,
            leg1_latency_ms=self._settings.leg1_latency_ms,
            leg2_latency_ms=self._settings.leg2_latency_ms,
        )

        # --- Leg 1: Kalshi ------------------------------------------------------
        kalshi_provided = await self._book_provider(
            Venue.KALSHI, opportunity.kalshi_leg.market_id, self._settings.leg1_latency_ms
        )
        kalshi_book, problem = self._usable(
            kalshi_provided, Venue.KALSHI, opportunity.kalshi_leg.market_id
        )
        if kalshi_book is None:
            # Nothing was sent, so there is no exposure — not a failed hedge.
            trade.set_execution_status(PaperTradeStatus.PENDING)
            trade.notes.append(f"leg 1 not attempted: {problem}")
            metrics.paper_trades.labels(status=trade.status.value).inc()
            return trade

        trade.leg1 = simulate_buy(
            book=kalshi_book,
            outcome=opportunity.kalshi_leg.outcome,
            quantity=opportunity.executable_quantity,
            client_order_id=f"{trade.id}-leg1",
            # Never pay more than the detected worst price: a real order would be a limit.
            limit_price=opportunity.kalshi_leg.worst_price,
            adverse_pct=self._settings.adverse_fill_pct,
        )
        trade.kalshi_fees = self._kalshi_fee_for(trade.leg1, kalshi_market)

        if trade.leg1.is_empty:
            trade.set_execution_status(PaperTradeStatus.PENDING)
            trade.notes.append("leg 1 did not fill; no exposure taken")
            metrics.paper_trades.labels(status=trade.status.value).inc()
            return trade

        trade.set_execution_status(PaperTradeStatus.LEG1_FILLED)
        if not trade.leg1.is_complete:
            trade.notes.append(
                f"leg 1 partially filled: {trade.leg1.filled_quantity} of "
                f"{trade.leg1.requested_quantity}"
            )

        # --- Leg 2: Polymarket, into a book that has moved -----------------------
        # Only hedge what leg 1 actually got. Sizing leg 2 to the original quantity
        # would create exposure in the opposite direction.
        hedge_quantity = trade.leg1.filled_quantity

        try:
            # Everything from here to the end of leg 2's await is where an interruption
            # lands, and from here on leg 1 is already filled. Letting the exception
            # escape bare discarded the trade with it, so simulated exposure that had
            # genuinely been taken simply stopped existing — invisibly, and only ever
            # understating risk.
            if self._checkpoint is not None:
                # Before leg 2, not after it: a checkpoint that only runs when the
                # process lives to catch an exception is not a checkpoint at all.
                await self._checkpoint(trade)
            polymarket_provided = await self._book_provider(
                Venue.POLYMARKET,
                opportunity.polymarket_leg.market_id,
                self._settings.leg2_latency_ms,
            )
        except BaseException as exc:
            trade.set_execution_status(PaperTradeStatus.INTERRUPTED)
            trade.notes.append(
                f"interrupted after leg 1 and before leg 2 was attempted ({exc!r}); "
                f"{trade.unhedged_quantity} contracts unhedged. This attempt is not "
                "evidence about hedging — leg 2 was never tried"
            )
            metrics.paper_trades.labels(status=trade.status.value).inc()
            log.error(
                "paper.trade.interrupted",
                trade_id=trade.id,
                unhedged=str(trade.unhedged_quantity),
                error=repr(exc),
            )
            raise ExecutionInterruptedError(trade, exc) from exc
        # A configured adverse-movement model overrides whatever the provider observed:
        # once leg 2 is priced against a synthetic move, the result describes the model.
        leg2_model_pct = self._settings.leg2_adverse_model_pct
        trade.leg2_evidence = grade_leg2(
            refetched=polymarket_provided.refetched,
            changed=polymarket_provided.changed,
            modelled=polymarket_provided.modelled or leg2_model_pct > ZERO,
        )
        metrics.paper_trade_evidence.labels(grade=trade.leg2_evidence.value).inc()
        if not trade.leg2_evidence.supports_hedge_claims:
            detail = (
                f"modelled at {leg2_model_pct} depth removal "
                f"({self._settings.leg2_adverse_model_source})"
                if leg2_model_pct > ZERO
                else "the book did not move between legs"
            )
            trade.notes.append(
                f"leg 2 evidence is {trade.leg2_evidence.value} — {detail}; this trade's "
                "hedge outcome does not demonstrate behaviour under adverse movement"
            )
        polymarket_book, problem = self._usable(
            polymarket_provided, Venue.POLYMARKET, opportunity.polymarket_leg.market_id
        )
        if polymarket_book is None:
            # Leg 1 is already on the book, so this is genuine unhedged exposure.
            trade.set_execution_status(PaperTradeStatus.LEG2_FAILED)
            trade.notes.append(
                f"leg 2 failed: {problem}; {trade.unhedged_quantity} contracts unhedged"
            )
            metrics.paper_trades.labels(status=trade.status.value).inc()
            return trade

        trade.leg2 = simulate_buy(
            book=polymarket_book,
            outcome=opportunity.polymarket_leg.outcome,
            quantity=hedge_quantity,
            client_order_id=f"{trade.id}-leg2",
            limit_price=opportunity.polymarket_leg.worst_price,
            adverse_pct=self._settings.adverse_fill_pct + leg2_model_pct,
        )
        trade.polymarket_fees = self._polymarket_fee_for(trade.leg2, polymarket_market)

        if trade.leg2.is_empty:
            trade.set_execution_status(PaperTradeStatus.LEG2_FAILED)
            trade.notes.append(
                f"leg 2 did not fill; {trade.unhedged_quantity} contracts unhedged on Kalshi"
            )
        elif trade.leg2.filled_quantity < hedge_quantity:
            trade.set_execution_status(PaperTradeStatus.PARTIALLY_HEDGED)
            trade.notes.append(
                f"leg 2 partially filled; {trade.unhedged_quantity} contracts unhedged"
            )
        else:
            trade.set_execution_status(PaperTradeStatus.BOTH_LEGS_FILLED)

        if trade.slippage_vs_detected > ZERO:
            trade.notes.append(
                f"realized spread {trade.slippage_vs_detected} worse per contract than detected"
            )

        metrics.paper_trades.labels(status=trade.status.value).inc()
        log.info(
            "paper.trade.executed",
            trade_id=trade.id,
            status=trade.status.value,
            hedged=str(trade.hedged_quantity),
            unhedged=str(trade.unhedged_quantity),
            pnl=str(trade.realized_pnl_if_hedged),
        )
        return trade

    def _kalshi_fee_for(self, order: SimulatedOrder, market: VenueMarket) -> Decimal:
        accumulator = KalshiOrderFeeAccumulator(
            config=market.fee_config, direct_member=self._kalshi_direct_member
        )
        total = sum(
            (accumulator.charge_fill(fill.quantity, fill.price).net for fill in order.fills), ZERO
        )
        return ceil_to(total, CENT_EXP)

    def _polymarket_fee_for(self, order: SimulatedOrder, market: VenueMarket) -> Decimal:
        return quantize_price(
            sum(
                (
                    taker_fee_for(fill.quantity, fill.price, market.fee_config)
                    for fill in order.fills
                ),
                ZERO,
            )
        )


@dataclass(frozen=True)
class SettlementOutcomes:
    """The outcome each venue actually resolved to, recorded per venue.

    Two venues can resolve the *same* question differently — a disputed source, a
    different cutoff reading, an amended rulebook — and that discrepancy is one of the
    real risks of a cross-venue strategy. Collapsing settlement into one shared winner
    would make it unrepresentable, so the outcomes are recorded separately even though
    they are usually derived from one event.

    :meth:`for_event` is the ordinary case: one event outcome, mapped through the pair's
    reviewer-recorded ``outcomes_inverted`` flag. Construct the class directly to record a
    genuine discrepancy.
    """

    kalshi: Outcome
    polymarket: Outcome
    #: Free text naming the settlement authority and evidence the operator relied on.
    #: Empty is permitted for programmatic use but the CLI requires it.
    evidence: str = ""
    recorded_by: str = ""

    @property
    def outcomes_differ(self) -> bool:
        """Whether the two venues resolved to different outcome *labels*.

        This is a fact about the labels, not a verdict. For an inverted pair, differing
        labels is exactly what a consistent settlement looks like. Whether the hedge
        actually held is decided by the payouts, not by comparing these two values.
        """
        return self.kalshi is not self.polymarket

    @classmethod
    def for_event(
        cls,
        winning_outcome: Outcome,
        *,
        outcomes_inverted: bool = False,
        evidence: str = "",
        recorded_by: str = "",
    ) -> SettlementOutcomes:
        """Map one event outcome onto both venues.

        ``outcomes_inverted`` is the reviewer's recorded statement that Polymarket YES
        corresponds to Kalshi NO. It is never inferred; it comes from the approved pair.
        """
        return cls(
            kalshi=winning_outcome,
            polymarket=winning_outcome.opposite if outcomes_inverted else winning_outcome,
            evidence=evidence,
            recorded_by=recorded_by,
        )


def settle(
    trade: PaperTrade,
    winning_outcome: Outcome | SettlementOutcomes,
    settled_at: datetime | None = None,
    *,
    polymarket_winning_outcome: Outcome | None = None,
) -> PaperTrade:
    """Reconcile a trade against the real settlement outcome, per venue and per account.

    Each leg pays $1.00 per filled contract **on its own venue, in its own asset**, if and
    only if the side that leg holds won *there*. That is the only formulation that is
    correct for an inverted pair, for a settlement discrepancy between the two venues, and
    for a partially-hedged trade — the previous one compared both legs against a single
    shared winner, so an inverted pair was scored against the wrong side of one book.

    The payouts land in ``settlement_result["payouts_by_account"]`` keyed by account, so
    the portfolio credits the venue that actually paid rather than a pooled currency
    balance.
    """
    if isinstance(winning_outcome, SettlementOutcomes):
        outcomes = winning_outcome
    else:
        outcomes = SettlementOutcomes(
            kalshi=winning_outcome,
            polymarket=polymarket_winning_outcome or winning_outcome,
        )

    kalshi_plan = trade.opportunity.kalshi_leg
    polymarket_plan = trade.opportunity.polymarket_leg
    payouts: dict[str, Decimal] = {}
    per_leg: list[dict[str, object]] = []
    total_payout = ZERO

    for label, leg, plan, resolved in (
        ("kalshi", trade.leg1, kalshi_plan, outcomes.kalshi),
        ("polymarket", trade.leg2, polymarket_plan, outcomes.polymarket),
    ):
        account = account_for(plan.venue, plan.product, plan.settlement_asset)
        filled = leg.filled_quantity if leg is not None else ZERO
        won = leg is not None and leg.outcome is resolved
        payout = quantize_price(filled * ONE_DOLLAR) if won else ZERO
        payouts[account.key] = payouts.get(account.key, ZERO) + payout
        total_payout += payout
        per_leg.append(
            {
                "leg": label,
                "account": account.key,
                "asset": plan.settlement_asset.value,
                "held_outcome": leg.outcome.value if leg is not None else None,
                "winning_outcome": resolved.value,
                "filled_quantity": str(filled),
                "payout": str(payout),
                "won": won,
            }
        )

    final_pnl = total_payout - trade.total_cost - trade.total_fees
    # The hedge held if, and only if, the hedged portion actually paid $1.00 a contract.
    # Comparing the two venues' outcome labels cannot answer this — differing labels are
    # correct for an inverted pair and wrong for a straight one — but the money can.
    hedge_held = total_payout >= trade.guaranteed_payout
    kalshi_asset = kalshi_plan.settlement_asset
    polymarket_asset = polymarket_plan.settlement_asset

    trade.settlement_result = {
        # Retained for continuity with earlier records; the per-venue outcomes below are
        # the authoritative fields, because a single winner cannot describe a discrepancy.
        "winning_outcome": outcomes.kalshi.value,
        "kalshi_winning_outcome": outcomes.kalshi.value,
        "polymarket_winning_outcome": outcomes.polymarket.value,
        "outcome_labels_differ": outcomes.outcomes_differ,
        "hedge_held": hedge_held,
        "legs": per_leg,
        "payouts_by_account": {key: str(amount) for key, amount in sorted(payouts.items())},
        "hedged_quantity": str(trade.hedged_quantity),
        "unhedged_quantity": str(trade.unhedged_quantity),
        "unhedged_venue": (
            trade.unhedged_venue.value if trade.unhedged_venue is not None else None
        ),
        "total_payout": str(quantize_price(total_payout)),
        "final_pnl": str(quantize_price(final_pnl)),
        "execution_status": trade.execution_status.value,
        "recorded_by": outcomes.recorded_by,
        "evidence": outcomes.evidence,
        # Settlement assets are read from the legs, not assumed. Polymarket US settles in
        # USD and Polymarket Global in USDC, so hardcoding either misreports one of them.
        "kalshi_settlement_asset": kalshi_asset.value,
        "polymarket_settlement_asset": polymarket_asset.value,
        "settlement_assets_differ": kalshi_asset is not polymarket_asset,
    }

    if polymarket_asset is SettlementAsset.USDC:
        # Only the Global path carries conversion and depeg exposure.
        polymarket_account = account_for(
            polymarket_plan.venue, polymarket_plan.product, polymarket_asset
        )
        trade.settlement_result["usdc_denominated_payout"] = str(
            payouts.get(polymarket_account.key, ZERO)
        )
        trade.settlement_result["settlement_asset_note"] = (
            f"Polymarket proceeds settle in {SettlementAsset.USDC.value}, "
            f"Kalshi in {kalshi_asset.value}; conversion and depeg risk apply"
        )
    else:
        trade.settlement_result["settlement_asset_note"] = (
            f"Both legs settle in {polymarket_asset.value}; "
            "no cross-asset conversion or depeg exposure"
        )

    if not hedge_held:
        trade.notes.append(
            f"settlement discrepancy: the hedged {trade.hedged_quantity} contracts paid "
            f"{quantize_price(total_payout)}, not the {trade.guaranteed_payout} a holding "
            "hedge pays. Both legs lost, or the two venues resolved inconsistently for "
            "this pair's recorded inversion — the position was not actually hedged"
        )

    # ``status`` records settlement; ``execution_status`` keeps how the trade filled.
    trade.status = PaperTradeStatus.SETTLED
    trade.settled_at = settled_at or datetime.now(UTC)
    return trade
