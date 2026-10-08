"""Paper portfolio: per-account balances, capital limits, exposure, and reporting.

Balances are held **per account**, where an account is ``(venue, product, asset)`` — see
:mod:`event_arb.paper.accounts`. Netting them into one number per currency hides exactly
the risk this project exists to surface: Kalshi USD and Polymarket US USD are both dollars
and are not interchangeable, and a Kalshi profit does not offset a Polymarket loss until
someone converts, at a rate that is not guaranteed to be 1.0000.

Cash is derived from an append-only ledger rather than kept as a running total, so every
movement is attributable and replay is idempotent. See :mod:`event_arb.paper.ledger`.

Three numbers in the reporting are deliberately kept apart, because conflating them is how
a paper trial reports a profit it never had:

``execution_pnl_estimate``
    The pessimistic view at execution time — the hedged portion pays out and the unhedged
    remainder is assumed worthless. Available immediately; it is an estimate.
``settled_pnl``
    What settlement actually paid, per venue and asset. Available only after a settlement
    has been recorded, and attributed to the day it settled.
``open_exposure``
    Positions with neither of the above yet: still outstanding, still able to move.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from event_arb.config import PaperSettings
from event_arb.domain.enums import PaperTradeStatus, SettlementAsset, Venue, VenueProduct
from event_arb.domain.models import LegPlan, Opportunity, RejectedOpportunity
from event_arb.domain.money import ZERO, quantize_price
from event_arb.observability.logging import get_logger
from event_arb.paper.accounts import AccountId, account_for
from event_arb.paper.durability import InvalidatedAttempt
from event_arb.paper.evidence import EvidenceGrade
from event_arb.paper.executor import PaperTrade
from event_arb.paper.ledger import EntryKind, LedgerEntry, PaperLedger

__all__ = [
    "CapitalCheck",
    "DailyReport",
    "PaperPortfolio",
    "TradeFunding",
    "expected_accounts",
    "opening_balances_from_settings",
]

log = get_logger(__name__)


@dataclass(frozen=True)
class TradeFunding:
    """What one prospective trade needs, per account, before it is attempted."""

    requirements: dict[AccountId, Decimal]
    notional: Decimal

    @property
    def total(self) -> Decimal:
        return quantize_price(sum(self.requirements.values(), ZERO))


@dataclass(frozen=True)
class CapitalCheck:
    """Whether a prospective trade may proceed, and why not.

    ``reasons`` is never empty when ``allowed`` is false: a refusal that cannot say what
    it refused is indistinguishable from a bug, and the operator has to be able to fix the
    configuration or fund the account.
    """

    allowed: bool
    reasons: tuple[str, ...] = ()
    shortfalls: dict[str, str] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class DailyReport:
    """One day's paper-trading summary.

    **Date semantics.** Execution counters — ``trades``, the hedge classes, fees, detected
    profit and evidence grades — count trades *opened* on ``day``, because they describe
    what execution did on that day. ``settled_pnl`` and ``settled_trades`` count trades
    *settled* on ``day``, whenever they were opened, because a payout is cash received on
    the settlement date. The two therefore do not describe the same set of trades, and
    that is intentional: a trade opened on Monday and settled on Friday is execution
    evidence for Monday and cash for Friday.
    """

    day: date
    trades: int = 0
    fully_hedged: int = 0
    partially_hedged: int = 0
    leg2_failures: int = 0
    #: Pessimistic execution-time estimate for trades opened today. Not settled cash.
    execution_pnl_estimate: Decimal = ZERO
    total_fees: Decimal = ZERO
    total_unhedged_quantity: Decimal = ZERO
    detected_profit: Decimal = ZERO
    #: Trades whose settlement was recorded today, and the cash it actually paid.
    settled_trades: int = 0
    settled_pnl: Decimal = ZERO
    #: Settled P&L broken out per account, because a USD gain and a USDC loss do not net.
    settled_pnl_by_account: dict[str, str] = field(default_factory=dict)
    missed_opportunities: dict[str, int] = field(default_factory=dict)
    #: Trades by leg-2 evidence grade. Only ``measured`` trades observed a book that
    #: actually moved; the rest cannot support a hedge-rate claim.
    evidence: dict[str, int] = field(default_factory=dict)
    #: Attempts whose accounting storage refused, and which are therefore counted
    #: nowhere else in this report. Any number above zero means the day's execution
    #: record has a hole in it. See :mod:`event_arb.paper.durability`.
    invalidated_attempts: int = 0
    #: Earlier runs against this database that never recorded a clean shutdown. What they
    #: did before dying is unknown by construction, so their existence is enough to make
    #: this report incomplete. Populated from the journal, which is the only place an
    #: abrupt death is visible at all. See :mod:`event_arb.paper.journal`.
    unfinished_runs: int = 0

    @property
    def measured_trades(self) -> int:
        return self.evidence.get(EvidenceGrade.MEASURED.value, 0)

    @property
    def hedge_rate_is_measured(self) -> bool:
        """True only when every counted trade re-observed a book that had moved.

        Read :attr:`hedge_success_rate` alongside this. A 100% rate over trades that all
        re-read an unchanged book means the book never moved in the sample — it is not
        evidence that hedging survives adverse movement, and quoting it as such would
        overstate readiness in exactly the direction that costs money.
        """
        return self.trades > 0 and self.measured_trades == self.trades and self.is_complete_evidence

    @property
    def is_complete_evidence(self) -> bool:
        """False when anything happened that this report cannot account for.

        Read before any rate in this report is quoted. An invalidated attempt is not a
        trade that went badly — it is a trade whose outcome nobody knows, so every
        denominator here is missing it. An unfinished run is the same problem one level
        up: a process that died without shutting down may have been mid-attempt, and
        storage looks identical either way.

        Both fail closed. The alternative was the defect this gate now exists to stop —
        a restart reading zero invalidations off the same database and calling the run
        complete, which turned a known hole into a clean report.
        """
        return self.invalidated_attempts == 0 and self.unfinished_runs == 0

    @property
    def evidence_caveat(self) -> str:
        """One line stating what the hedge rate does and does not show."""
        if self.invalidated_attempts:
            return (
                f"INCOMPLETE: {self.invalidated_attempts} attempt(s) could not be stored "
                "and are counted nowhere in this report; no rate below is over the full "
                "set of attempts the run made"
            )
        if self.unfinished_runs:
            return (
                f"INCOMPLETE: {self.unfinished_runs} earlier run(s) against this database "
                "ended without a clean shutdown, so what they attempted is unknown; "
                "review and acknowledge them before quoting any rate below"
            )
        if self.trades == 0:
            return "no trades"
        if self.hedge_rate_is_measured:
            return "measured: every leg 2 priced against a book observed to have moved"
        unmeasured = self.trades - self.measured_trades
        return (
            f"NOT measured: {unmeasured} of {self.trades} trades priced leg 2 against a book "
            "that did not move (or was modelled); this rate does not show hedging surviving "
            "adverse movement"
        )

    @property
    def hedge_success_rate(self) -> Decimal:
        """Fraction of attempts that ended fully hedged.

        This, not P&L, is the headline number for a cross-venue strategy: an unhedged
        leg is the failure mode that turns arbitrage into gambling.

        It is only meaningful when :attr:`hedge_rate_is_measured` is true. See
        :attr:`evidence_caveat`.
        """
        if self.trades == 0:
            return ZERO
        return quantize_price(Decimal(self.fully_hedged) / Decimal(self.trades) * Decimal("100"))

    @property
    def realization_rate(self) -> Decimal:
        """Execution-estimate P&L as a percentage of the profit detection predicted.

        This compares two execution-time numbers, so it says nothing about settlement.
        It can exceed 100%, and that is not a bug: detection subtracts the slippage,
        operational, and USDC-conversion **buffers**, which are safety margins rather
        than costs a clean fill actually incurs. A rate below 100% is the meaningful
        signal — it means execution gave back edge that detection had counted on.
        """
        if self.detected_profit <= ZERO:
            return ZERO
        return quantize_price(self.execution_pnl_estimate / self.detected_profit * Decimal("100"))

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "trades_opened": self.trades,
            "fully_hedged": self.fully_hedged,
            "partially_hedged": self.partially_hedged,
            "leg2_failures": self.leg2_failures,
            "hedge_success_rate_pct": str(self.hedge_success_rate),
            "hedge_rate_is_measured": self.hedge_rate_is_measured,
            "evidence_caveat": self.evidence_caveat,
            "evidence": dict(self.evidence),
            "invalidated_attempts": self.invalidated_attempts,
            "unfinished_runs": self.unfinished_runs,
            "is_complete_evidence": self.is_complete_evidence,
            "execution_pnl_estimate": str(quantize_price(self.execution_pnl_estimate)),
            "settled_trades": self.settled_trades,
            "settled_pnl": str(quantize_price(self.settled_pnl)),
            "settled_pnl_by_account": dict(self.settled_pnl_by_account),
            "total_fees": str(quantize_price(self.total_fees)),
            "detected_profit": str(quantize_price(self.detected_profit)),
            "realization_rate_pct": str(self.realization_rate),
            "total_unhedged_quantity": str(self.total_unhedged_quantity),
            "missed_opportunities": dict(self.missed_opportunities),
            "pnl_note": (
                "execution_pnl_estimate counts trades opened on this day and assumes any "
                "unhedged remainder expires worthless; settled_pnl counts trades settled "
                "on this day, whenever they were opened. They are not the same trades."
            ),
        }


class PaperPortfolio:
    """Tracks simulated per-account balances, capital limits, positions, and history."""

    def __init__(
        self,
        starting_usd: Decimal = Decimal("10000"),
        starting_usdc: Decimal = Decimal("10000"),
        *,
        opening_balances: dict[AccountId, Decimal] | None = None,
        max_trade_notional: Decimal | None = None,
        max_open_notional: Decimal | None = None,
        max_open_unhedged_quantity: Decimal | None = None,
        min_account_balance: Decimal = ZERO,
    ) -> None:
        """Open a portfolio.

        ``starting_usd`` / ``starting_usdc`` are the per-asset defaults every account in
        that asset opens with; ``opening_balances`` names accounts explicitly and wins.
        The defaults are not a pool being divided — see
        :meth:`event_arb.config.PaperSettings.starting_balance_for`.

        A limit left as ``None`` is not enforced. That is for tests and demos of fill
        behaviour; a trial configures all of them, and the preflight refuses to start a
        live-data trial that has not.
        """
        self._default_by_asset: dict[SettlementAsset, Decimal] = {
            SettlementAsset.USD: starting_usd,
            SettlementAsset.USDC: starting_usdc,
        }
        self._explicit_opening: dict[AccountId, Decimal] = dict(opening_balances or {})
        self.ledger = PaperLedger(self._explicit_opening)
        self.max_trade_notional = max_trade_notional
        self.max_open_notional = max_open_notional
        self.max_open_unhedged_quantity = max_open_unhedged_quantity
        self.min_account_balance = min_account_balance
        self.trades: list[PaperTrade] = []
        self._trade_ids: set[str] = set()
        self.rejections: list[RejectedOpportunity] = []
        #: Attempts storage refused, and which are therefore absent from every count
        #: above. Held so the run can state the gap rather than resume as though clean.
        self.invalidated: list[InvalidatedAttempt] = []
        #: Run ids from the journal that opened and never closed. See
        #: :meth:`record_unfinished_runs`.
        self.unfinished_runs: list[str] = []

    # ------------------------------------------------------------------ accounts

    def _ensure_account(self, account: AccountId) -> None:
        """Open an account at its configured starting balance, once."""
        entry_id = f"opening:{account.key}"
        if self.ledger.has(entry_id):
            return
        amount = self._explicit_opening.get(
            account, self._default_by_asset.get(account.asset, ZERO)
        )
        self.ledger.append(
            LedgerEntry(
                entry_id=entry_id,
                account=account,
                kind=EntryKind.OPENING_BALANCE,
                amount=amount,
                detail="configured starting balance",
            )
        )

    def balance(self, account: AccountId | SettlementAsset) -> Decimal:
        """Cash in one account.

        A bare :class:`SettlementAsset` sums every account in that asset. It is a
        reporting convenience only — it is never used to decide whether a trade can be
        funded, because summing across venues is precisely the error that let a trade
        spend money it did not have where it was spending it.
        """
        if isinstance(account, SettlementAsset):
            return quantize_price(
                sum(
                    (self.ledger.balance(a) for a in self.ledger.accounts() if a.asset is account),
                    ZERO,
                )
            )
        self._ensure_account(account)
        return self.ledger.balance(account)

    def available(self, account: AccountId) -> Decimal:
        """Balance minus outstanding reservations: what a new trade may spend."""
        self._ensure_account(account)
        return self.ledger.available(account)

    @property
    def balances(self) -> dict[str, str]:
        """Per-account balances, keyed by ``venue_product:asset``."""
        return {a.key: str(self.ledger.balance(a)) for a in self.ledger.accounts()}

    @property
    def balances_by_asset(self) -> dict[str, str]:
        """Totals per asset, for a report header. Never for a funding decision."""
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for account in self.ledger.accounts():
            totals[account.asset.value] += self.ledger.balance(account)
        return {asset: str(quantize_price(amount)) for asset, amount in sorted(totals.items())}

    def has_capacity(self, account: AccountId | SettlementAsset, amount: Decimal) -> bool:
        """Whether ``amount`` is available in ``account`` right now."""
        if isinstance(account, SettlementAsset):
            return self.balance(account) >= amount
        return self.available(account) >= amount

    # ------------------------------------------------------------------- funding

    @staticmethod
    def account_for_leg(leg: LegPlan) -> AccountId:
        """The simulated account a planned leg draws on."""
        return account_for(leg.venue, leg.product, leg.settlement_asset)

    @classmethod
    def funding_for(cls, opportunity: Opportunity) -> TradeFunding:
        """What an opportunity commits, per account, including fees.

        Fees are part of the requirement, not an afterthought: a trade that can afford the
        contracts but not the fee is not affordable.
        """
        requirements: dict[AccountId, Decimal] = defaultdict(lambda: ZERO)
        for leg, fees in (
            (opportunity.kalshi_leg, opportunity.kalshi_fees),
            (opportunity.polymarket_leg, opportunity.polymarket_fees),
        ):
            requirements[cls.account_for_leg(leg)] += quantize_price(leg.gross_cost + fees)
        return TradeFunding(
            requirements=dict(requirements),
            notional=quantize_price(opportunity.gross_acquisition_cost),
        )

    def check_capital(self, opportunity: Opportunity) -> CapitalCheck:
        """Decide whether this opportunity may be attempted at all.

        Refuses rather than sizing down. An opportunity's profitability was assessed at its
        executable quantity against real depth, and the operational-cost allowance does not
        scale with size, so trading a smaller quantity would be trading economics nobody
        evaluated. The shortfall is reported so the operator can fund the account or lower
        ``opportunity.max_quantity`` deliberately.
        """
        funding = self.funding_for(opportunity)
        reasons: list[str] = []
        shortfalls: dict[str, str] = {}

        for account, required in sorted(funding.requirements.items()):
            available = self.available(account)
            usable = available - self.min_account_balance
            if usable < required:
                short = quantize_price(required - usable)
                shortfalls[account.key] = str(short)
                floor = (
                    f" while retaining the {self.min_account_balance} minimum"
                    if self.min_account_balance > ZERO
                    else ""
                )
                reasons.append(
                    f"{account.key} needs {quantize_price(required)}{floor} but only "
                    f"{quantize_price(available)} is available (short {short})"
                )

        if self.max_trade_notional is not None and funding.notional > self.max_trade_notional:
            reasons.append(
                f"notional {funding.notional} exceeds max_trade_notional {self.max_trade_notional}"
            )

        if self.max_open_notional is not None:
            projected = self.open_notional + funding.notional
            if projected > self.max_open_notional:
                reasons.append(
                    f"open notional would reach {quantize_price(projected)}, above "
                    f"max_open_notional {self.max_open_notional}"
                )

        if self.max_open_unhedged_quantity is not None:
            outstanding = self.total_unhedged_quantity
            if outstanding > self.max_open_unhedged_quantity:
                reasons.append(
                    f"{outstanding} contracts are already unhedged, at or above "
                    f"max_open_unhedged_quantity {self.max_open_unhedged_quantity}; "
                    "resolve the open exposure before opening more"
                )

        return CapitalCheck(allowed=not reasons, reasons=tuple(reasons), shortfalls=shortfalls)

    def reserve(self, trade_id: str, funding: TradeFunding) -> list[LedgerEntry]:
        """Hold the cash a trade is about to commit.

        Taken *before* execution and released against actual fills afterwards, so two
        opportunities racing through the same account cannot both be told the money is
        there. Idempotent by entry id, so a retried attempt re-reserves nothing.
        """
        applied: list[LedgerEntry] = []
        for account, amount in sorted(funding.requirements.items()):
            self._ensure_account(account)
            entry = LedgerEntry(
                entry_id=PaperLedger.reservation_id(trade_id, account),
                account=account,
                kind=EntryKind.RESERVATION,
                amount=quantize_price(amount),
                trade_id=trade_id,
                detail="held pending execution",
            )
            if self.ledger.append(entry):
                applied.append(entry)
        return applied

    def release(self, trade_id: str) -> list[LedgerEntry]:
        """Give back the cash held under ``trade_id``, and only cash actually held.

        The released amount comes from the hold itself, and a ``trade_id`` holding nothing
        releases nothing. Both matter, because a release used to be recomputed from the
        funding requirement and appended unconditionally: releasing a hold that was never
        taken did not merely fail to free cash, it drove ``reserved`` *below* zero and so
        inflated ``available`` by the full cost of the trade.

        That is not hypothetical. :meth:`record` releases under the trade's id, while the
        automated path holds under the opportunity's id, so the two never matched — every
        automated trade appended a phantom release that cancelled its own debit exactly,
        and the capital gate went on reading the opening balance no matter how much had
        been spent. Deriving the release from the hold makes the mismatch a no-op instead
        of an invisible credit, and keeps ``reserved`` non-negative by construction.

        Debits for what actually filled are recorded separately, by :meth:`record`.
        """
        applied: list[LedgerEntry] = []
        for held in self.ledger.holds_for(trade_id):
            entry = LedgerEntry(
                entry_id=PaperLedger.release_id(trade_id, held.account),
                account=held.account,
                kind=EntryKind.RELEASE,
                amount=quantize_price(-held.amount),
                trade_id=trade_id,
                detail="reservation released after execution",
            )
            if self.ledger.append(entry):
                applied.append(entry)
        return applied

    def release_stale_holds(self) -> list[LedgerEntry]:
        """Release every hold left outstanding by a process that is gone.

        A hold is taken immediately before execution and released immediately after, so it
        can only still be outstanding when a portfolio is restored if the process holding
        it died in between. Nothing is in flight in a process that has not started trading
        yet, so replaying such a hold reserves cash against a trade that will never
        finish — permanently, because retention deliberately refuses to compact an
        unreleased reservation. Left alone, every interrupted run would shrink the paper
        account's usable capital a little further, and only the capital gate would notice.
        """
        released: list[LedgerEntry] = []
        for hold in self.ledger.outstanding_holds():
            released.extend(self.release(hold.trade_id))
        return released

    # -------------------------------------------------------------------- record

    def plan_record(self, trade: PaperTrade) -> list[LedgerEntry]:
        """The cash entries ``trade`` implies, computed **without** applying any of them.

        Planning is separated from applying so a caller can store the entries before
        publishing them to memory. That ordering is the whole of the durability policy:
        see :mod:`event_arb.paper.durability`. Nothing here mutates, so a plan that is
        never committed leaves no trace — which is what makes a failed write recoverable
        by dropping the attempt rather than by unwinding entries other coroutines may
        already have appended alongside.

        Returns the **full** deterministic set, including entries already present in the
        ledger, so a retry after a failed write re-persists everything the attempt owes.
        The entry ids make both the storage write and :meth:`commit_record` idempotent, so
        returning them twice cannot become a second debit. Returning only the *newly
        applied* subset is precisely the bug that made a failed settlement write
        permanent — see :meth:`credit_settlement`.
        """
        entries: list[LedgerEntry] = []
        for label, leg, plan, fees in (
            ("leg1", trade.leg1, trade.opportunity.kalshi_leg, trade.kalshi_fees),
            ("leg2", trade.leg2, trade.opportunity.polymarket_leg, trade.polymarket_fees),
        ):
            if leg is None:
                continue
            account = self.account_for_leg(plan)
            entries.append(
                LedgerEntry(
                    entry_id=f"{trade.id}:{label}:cost",
                    account=account,
                    kind=EntryKind.TRADE_DEBIT,
                    amount=quantize_price(-leg.gross_cost),
                    trade_id=trade.id,
                    detail=f"{label} {leg.filled_quantity} @ vwap {leg.vwap}",
                )
            )
            if fees > ZERO:
                entries.append(
                    LedgerEntry(
                        entry_id=f"{trade.id}:{label}:fees",
                        account=account,
                        kind=EntryKind.FEE_DEBIT,
                        amount=quantize_price(-fees),
                        trade_id=trade.id,
                        detail=f"{label} taker fees",
                    )
                )

        # Releases are derived from holds actually outstanding, exactly as in
        # :meth:`release`, so planning cannot invent a release for a hold never taken.
        for held in self.ledger.holds_for(trade.id):
            entries.append(
                LedgerEntry(
                    entry_id=PaperLedger.release_id(trade.id, held.account),
                    account=held.account,
                    kind=EntryKind.RELEASE,
                    amount=quantize_price(-held.amount),
                    trade_id=trade.id,
                    detail="reservation released after execution",
                )
            )
        return entries

    def commit_record(self, trade: PaperTrade, entries: list[LedgerEntry]) -> list[LedgerEntry]:
        """Publish a planned trade to memory. Returns the entries that were new.

        Call this only once the entries are durable. Every account touched is opened at
        its configured starting balance first, so the opening entry a plan assumed is
        present before its debit lands.
        """
        if trade.id in self._trade_ids:
            # A trade already here is a leg-1 checkpoint being superseded by the finished
            # attempt. Replace it in place rather than skipping: the checkpoint published
            # a snapshot of the trade as it stood *then*, deliberately detached from the
            # live object the executor goes on mutating, so keeping it would leave the
            # portfolio showing an attempt frozen before its hedge.
            self.trades = [trade if t.id == trade.id else t for t in self.trades]
        else:
            self.trades.append(trade)
            self._trade_ids.add(trade.id)
        applied: list[LedgerEntry] = []
        for entry in entries:
            self._ensure_account(entry.account)
            if self.ledger.append(entry):
                applied.append(entry)
        return applied

    def record(self, trade: PaperTrade) -> list[LedgerEntry]:
        """Plan and immediately apply a completed trade's cash effects.

        Leg 2 used to be debited from USDC unconditionally, which was wrong for every
        Polymarket US trade: both its legs settle in USD, so a US trade drew down a USDC
        balance that no US account holds and left the USD balance untouched.

        Any reservation still outstanding for this trade is released here, so the unused
        remainder of a partial fill becomes available again rather than staying held.
        Idempotent: recording the same trade twice does not double-debit.

        Publishing before storage is what this method cannot express, and callers with a
        database must not use it for that reason: use :meth:`plan_record`, store, then
        :meth:`commit_record`. This remains the path for the one-shot ``paper`` command
        and for unit tests of fill behaviour, where memory is the only store there is.
        """
        return self.commit_record(trade, self.plan_record(trade))

    def credit_settlement(self, trade: PaperTrade) -> list[LedgerEntry]:
        """Credit a settled trade's payouts to the accounts that received them.

        Requires ``trade.settlement_result`` to carry per-account payouts, which
        :func:`event_arb.paper.executor.settle` writes. Idempotent by entry id, so
        re-running settlement — after a crash, or because an operator repeated the
        command — credits the payout exactly once.

        Returns **every** entry the settlement implies, not only the ones this call newly
        appended to the in-memory ledger. That distinction is what made a failed write
        permanent: the credit is applied to memory before the transaction that stores it,
        so when that transaction failed, the retry found the entries already present,
        returned an empty list for the caller to persist, and reported ``already_settled``.
        The trade ended up marked settled in storage with its payout missing from the
        account — silently, and for good. Handing back the full set means a retry
        re-persists it, and the deterministic entry ids keep that a no-op when the first
        write did land.
        """
        result = trade.settlement_result or {}
        payouts = result.get("payouts_by_account")
        if not isinstance(payouts, dict):
            return []
        entries: list[LedgerEntry] = []
        if trade.id not in self._trade_ids:
            self.trades.append(trade)
            self._trade_ids.add(trade.id)
        for key, amount in sorted(payouts.items()):
            account = AccountId.parse(str(key))
            self._ensure_account(account)
            entry = LedgerEntry(
                entry_id=f"{trade.id}:settlement:{account.key}",
                account=account,
                kind=EntryKind.SETTLEMENT_CREDIT,
                amount=quantize_price(Decimal(str(amount))),
                trade_id=trade.id,
                detail=f"settlement payout ({result.get('winning_outcome_note', 'recorded')})",
            )
            self.ledger.append(entry)
            entries.append(entry)
        return entries

    def record_rejections(self, rejections: list[RejectedOpportunity]) -> None:
        self.rejections.extend(rejections)

    def record_unfinished_runs(self, run_ids: list[str]) -> None:
        """Note earlier runs against this database that never shut down cleanly.

        Held as a property of the *portfolio* rather than of a day, unlike
        :attr:`invalidated`, because a process that died mid-attempt casts doubt on the
        stored state itself and does not stop doing so at midnight. Replacing rather than
        appending keeps a second restore from double-counting the same run.
        """
        self.unfinished_runs = list(run_ids)

    def record_invalidation(self, attempt: InvalidatedAttempt) -> None:
        """Note an attempt whose accounting storage would not accept.

        The attempt is deliberately *not* added to :attr:`trades` and its cash is
        deliberately *not* applied — that is what keeps memory equal to what a restart
        reads. Recording it here is the other half of the bargain: the observation the run
        lost is named, so the daily report can refuse to call the evidence complete.
        """
        self.invalidated.append(attempt)
        log.error("paper.attempt_invalidated", **{k: str(v) for k, v in attempt.as_dict().items()})

    def resolve_invalidation(self, trade_id: str) -> int:
        """Drop invalidations for a trade that has since been stored. Returns how many.

        A failed write that a retry repairs is not a permanent hole in the evidence, and
        leaving it recorded would make every repaired run look incomplete. Only a
        confirmed write calls this.
        """
        before = len(self.invalidated)
        self.invalidated = [a for a in self.invalidated if a.trade_id != trade_id]
        return before - len(self.invalidated)

    def apply_entries(self, entries: list[LedgerEntry]) -> int:
        """Replay stored ledger entries. Returns how many were new."""
        return self.ledger.extend(entries)

    def adopt(self, trades: list[PaperTrade]) -> int:
        """Attach restored trade history without re-applying its cash. Returns how many.

        Cash comes from the ledger; this is the history, exposure and reporting side. The
        two are separated so a pruned trade row can never silently change a balance.
        """
        added = 0
        for trade in trades:
            if trade.id in self._trade_ids:
                continue
            self.trades.append(trade)
            self._trade_ids.add(trade.id)
            added += 1
        self.trades.sort(key=lambda t: t.created_at)
        return added

    # ------------------------------------------------------------------ exposure

    def open_exposure(self) -> list[dict[str, object]]:
        """Unhedged positions still outstanding, by trade."""
        exposure: list[dict[str, object]] = []
        for trade in self.trades:
            if trade.is_settled or trade.unhedged_quantity <= ZERO:
                continue
            venue = trade.unhedged_venue
            leg = trade.leg1 if venue is Venue.KALSHI else trade.leg2
            plan = (
                trade.opportunity.kalshi_leg
                if venue is Venue.KALSHI
                else trade.opportunity.polymarket_leg
            )
            exposure.append(
                {
                    "trade_id": trade.id,
                    "pair_id": trade.pair_id,
                    "venue": venue.value if venue else None,
                    "account": self.account_for_leg(plan).key if venue else None,
                    "outcome": leg.outcome.value if leg else None,
                    "quantity": str(trade.unhedged_quantity),
                    "cost_basis": str(leg.vwap) if leg else "0",
                    "status": trade.status.value,
                    "execution_status": trade.execution_status.value,
                }
            )
        return exposure

    @property
    def total_unhedged_quantity(self) -> Decimal:
        return quantize_price(
            sum((t.unhedged_quantity for t in self.trades if not t.is_settled), ZERO)
        )

    @property
    def open_notional(self) -> Decimal:
        """Cost basis committed to trades that have not settled."""
        return quantize_price(sum((t.total_cost for t in self.trades if not t.is_settled), ZERO))

    @property
    def unresolved_trade_ids(self) -> frozenset[str]:
        """Trades whose position is still open, so their history must not be pruned."""
        return frozenset(
            t.id for t in self.trades if not t.is_settled or t.unhedged_quantity > ZERO
        )

    # ----------------------------------------------------------------- reporting

    def daily_report(self, day: date | None = None) -> DailyReport:
        target = day or datetime.now(UTC).date()
        report = DailyReport(day=target)
        settled_by_account: dict[str, Decimal] = defaultdict(lambda: ZERO)

        for trade in self.trades:
            if trade.created_at.date() == target:
                report.trades += 1
                report.execution_pnl_estimate += trade.realized_pnl_if_hedged
                report.total_fees += trade.total_fees
                report.total_unhedged_quantity += trade.unhedged_quantity
                report.detected_profit += trade.opportunity.net_profit
                grade = trade.leg2_evidence.value
                report.evidence[grade] = report.evidence.get(grade, 0) + 1
                # Classified by what execution did, not by the current status. Settling a
                # trade must not retroactively remove it from the hedge counts.
                if trade.execution_status is PaperTradeStatus.BOTH_LEGS_FILLED:
                    report.fully_hedged += 1
                elif trade.execution_status is PaperTradeStatus.PARTIALLY_HEDGED:
                    report.partially_hedged += 1
                elif trade.execution_status is PaperTradeStatus.LEG2_FAILED:
                    report.leg2_failures += 1

            if trade.settled_at is not None and trade.settled_at.date() == target:
                result = trade.settlement_result or {}
                report.settled_trades += 1
                report.settled_pnl += Decimal(str(result.get("final_pnl", "0")))
                payouts = result.get("payouts_by_account")
                if isinstance(payouts, dict):
                    for key, amount in payouts.items():
                        settled_by_account[str(key)] += Decimal(str(amount))

        report.settled_pnl_by_account = {
            key: str(quantize_price(amount)) for key, amount in sorted(settled_by_account.items())
        }

        missed: dict[str, int] = defaultdict(int)
        for rejection in self.rejections:
            if rejection.detected_at.date() == target:
                missed[rejection.reason.value] += 1
        report.missed_opportunities = dict(missed)
        report.invalidated_attempts = sum(
            1 for attempt in self.invalidated if attempt.at.date() == target
        )
        # Not filtered by date, unlike everything above it: an unfinished run is a doubt
        # about the stored state this report is built from, and that doubt does not
        # expire at midnight the way a day's execution counters roll over.
        report.unfinished_runs = len(self.unfinished_runs)
        return report

    # -------------------------------------------------------------------- export

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "accounts": self.ledger.as_dict(),
                "balances": self.balances,
                "balances_by_asset": self.balances_by_asset,
                "open_exposure": self.open_exposure(),
                "open_notional": str(self.open_notional),
                "trades": [t.as_dict() for t in self.trades],
                "daily_report": self.daily_report().as_dict(),
            },
            indent=indent,
        )

    def to_csv(self) -> str:
        """Flat per-trade CSV for spreadsheet review."""
        buffer = io.StringIO()
        columns = [
            "id",
            "pair_id",
            "direction",
            "status",
            "execution_status",
            "hedged_quantity",
            "unhedged_quantity",
            "unhedged_venue",
            "total_cost",
            "total_fees",
            "realized_pnl_if_hedged",
            "settled_pnl",
            "detected_net_profit",
            "realized_spread",
            "slippage_vs_detected",
            "created_at",
            "settled_at",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for trade in self.trades:
            row = trade.as_dict()
            result = trade.settlement_result or {}
            row["settled_pnl"] = str(result.get("final_pnl", ""))
            writer.writerow({k: v for k, v in row.items() if k in columns})
        return buffer.getvalue()


def opening_balances_from_settings(
    accounts: list[AccountId], settings: PaperSettings
) -> dict[AccountId, Decimal]:
    """Resolve each account's opening balance from :class:`PaperSettings`.

    Kept here rather than in config so the compatibility policy for the old per-asset
    settings has exactly one implementation. See
    :meth:`event_arb.config.PaperSettings.starting_balance_for`.
    """
    resolved: dict[AccountId, Decimal] = {}
    for account in accounts:
        resolved[account] = settings.starting_balance_for(account.key, account.asset)
    return resolved


def expected_accounts(polymarket_product: VenueProduct) -> list[AccountId]:
    """The accounts a trial on ``polymarket_product`` will actually draw on."""
    polymarket_asset = (
        SettlementAsset.USD
        if polymarket_product is VenueProduct.POLYMARKET_US
        else SettlementAsset.USDC
    )
    return [
        AccountId(Venue.KALSHI, VenueProduct.NONE, SettlementAsset.USD),
        AccountId(Venue.POLYMARKET, polymarket_product, polymarket_asset),
    ]
