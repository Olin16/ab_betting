"""The append-only cash ledger behind the simulated portfolio.

Balances are **derived**, never stored as a running total. A stored total cannot be
reconciled and cannot survive a partial write: a process that dies between debiting cash
and persisting the trade leaves a number nobody can check. Deriving the balance from
entries makes every movement attributable, and makes replay safe.

Replay safety is the point of :attr:`LedgerEntry.entry_id`. Every entry's id is derived
from what it *is* — ``{trade_id}:leg1_debit``, not a counter — so applying the same entry
twice is a no-op rather than a double debit. That is what lets startup replay the stored
ledger without reconciling it against what memory already knows, and what lets a retried
settlement credit a payout exactly once.

Reservations are entries too. A reservation reduces *available* funds without reducing the
balance, so two opportunities racing through the same account cannot both be told the cash
is there. It is released, in whole or in part, once the fills are known.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from event_arb.domain.money import ZERO, quantize_price
from event_arb.paper.accounts import AccountId

__all__ = ["EntryKind", "LedgerEntry", "PaperLedger"]


class EntryKind(StrEnum):
    """Why money moved, or why it was held.

    ``RESERVATION`` and ``RELEASE`` never change a balance; they change what is
    *available* to spend. Everything else changes the balance.
    """

    OPENING_BALANCE = "opening_balance"
    #: Net of ledger entries removed by retention compaction. Carries their cash forward
    #: exactly so pruning history cannot change a balance.
    CARRY_FORWARD = "carry_forward"
    RESERVATION = "reservation"
    RELEASE = "release"
    TRADE_DEBIT = "trade_debit"
    FEE_DEBIT = "fee_debit"
    SETTLEMENT_CREDIT = "settlement_credit"

    @property
    def moves_cash(self) -> bool:
        return self not in {EntryKind.RESERVATION, EntryKind.RELEASE}

    @property
    def is_reservation(self) -> bool:
        return self in {EntryKind.RESERVATION, EntryKind.RELEASE}


@dataclass(frozen=True)
class LedgerEntry:
    """One movement of, or hold on, simulated cash.

    ``amount`` is signed in the direction of the balance for cash entries: a debit is
    negative, a credit positive. For reservation entries it is the size of the hold
    (positive) or of the release (negative).
    """

    entry_id: str
    account: AccountId
    kind: EntryKind
    amount: Decimal
    trade_id: str = ""
    detail: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "account": self.account.key,
            "kind": self.kind.value,
            "amount": str(quantize_price(self.amount)),
            "trade_id": self.trade_id,
            "detail": self.detail,
            "created_at": self.created_at.isoformat(),
        }


class PaperLedger:
    """Derives balances and holds from an append-only, idempotent entry list."""

    def __init__(self, opening: Mapping[AccountId, Decimal] | None = None) -> None:
        self._entries: dict[str, LedgerEntry] = {}
        self._order: list[str] = []
        for account, amount in (opening or {}).items():
            self.append(
                LedgerEntry(
                    entry_id=f"opening:{account.key}",
                    account=account,
                    kind=EntryKind.OPENING_BALANCE,
                    amount=amount,
                    detail="configured starting balance",
                )
            )

    # ------------------------------------------------------------------- mutation

    def append(self, entry: LedgerEntry) -> bool:
        """Record ``entry``. Returns False when it was already applied.

        Idempotency lives here rather than at every call site, because every call site is
        somewhere a retry, a restart or a replay can land.
        """
        if entry.entry_id in self._entries:
            return False
        self._entries[entry.entry_id] = entry
        self._order.append(entry.entry_id)
        return True

    def extend(self, entries: Iterable[LedgerEntry]) -> int:
        """Apply many entries, ignoring ones already present. Returns how many applied."""
        return sum(1 for entry in entries if self.append(entry))

    def has(self, entry_id: str) -> bool:
        return entry_id in self._entries

    def entry(self, entry_id: str) -> LedgerEntry | None:
        return self._entries.get(entry_id)

    @staticmethod
    def reservation_id(trade_id: str, account: AccountId) -> str:
        return f"{trade_id}:reserve:{account.key}"

    @staticmethod
    def release_id(trade_id: str, account: AccountId) -> str:
        return f"{trade_id}:release:{account.key}"

    def holds_for(self, trade_id: str) -> tuple[LedgerEntry, ...]:
        """The reservations taken under ``trade_id`` that have not been released.

        A release is the undo of one specific hold, so it has to be derived from the hold
        rather than recomputed from a funding requirement. Recomputing it is how a release
        came to be written for a hold that was never taken.
        """
        return tuple(
            entry
            for entry in self.entries
            if entry.kind is EntryKind.RESERVATION
            and entry.trade_id == trade_id
            and not self.has(self.release_id(trade_id, entry.account))
        )

    def outstanding_holds(self) -> tuple[LedgerEntry, ...]:
        """Every reservation with no matching release, in the order they were taken."""
        return tuple(
            entry
            for entry in self.entries
            if entry.kind is EntryKind.RESERVATION
            and not self.has(self.release_id(entry.trade_id, entry.account))
        )

    # -------------------------------------------------------------------- reading

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        """Entries in the order they were applied."""
        return tuple(self._entries[entry_id] for entry_id in self._order)

    def entries_for_trade(self, trade_id: str) -> tuple[LedgerEntry, ...]:
        return tuple(entry for entry in self.entries if entry.trade_id == trade_id)

    def accounts(self) -> tuple[AccountId, ...]:
        seen: dict[AccountId, None] = {}
        for entry in self.entries:
            seen.setdefault(entry.account, None)
        return tuple(sorted(seen))

    def balance(self, account: AccountId) -> Decimal:
        """Cash actually in the account, ignoring holds."""
        total = sum(
            (e.amount for e in self.entries if e.account == account and e.kind.moves_cash),
            ZERO,
        )
        return quantize_price(total)

    def reserved(self, account: AccountId) -> Decimal:
        """Cash currently held against in-flight trades."""
        total = sum(
            (e.amount for e in self.entries if e.account == account and e.kind.is_reservation),
            ZERO,
        )
        return quantize_price(total)

    def available(self, account: AccountId) -> Decimal:
        """What a new trade may spend: balance minus outstanding holds."""
        return quantize_price(self.balance(account) - self.reserved(account))

    def balances(self) -> dict[AccountId, Decimal]:
        return {account: self.balance(account) for account in self.accounts()}

    def as_dict(self) -> dict[str, object]:
        return {
            account.key: {
                "balance": str(self.balance(account)),
                "reserved": str(self.reserved(account)),
                "available": str(self.available(account)),
            }
            for account in self.accounts()
        }

    # ---------------------------------------------------------------- compaction

    def compact_before(self, cutoff: datetime, keep_trade_ids: frozenset[str]) -> int:
        """Replace entries older than ``cutoff`` with one carry-forward per account.

        Retention must bound the ledger without changing a single balance, so pruned
        entries are summed per account and re-appended as a single entry. Entries
        belonging to ``keep_trade_ids`` — trades whose position is still unresolved — are
        never compacted, because their per-trade detail is what reconstructs the exposure.

        Returns how many entries were removed.
        """
        removable = [
            entry
            for entry in self.entries
            if entry.created_at < cutoff
            and entry.kind is not EntryKind.CARRY_FORWARD
            # A hold and its release are a matched pair. Folding either into a
            # carry-forward would leave the other unmatched and free cash that was never
            # spent, so reservations age out only by being released.
            and not entry.kind.is_reservation
            and (not entry.trade_id or entry.trade_id not in keep_trade_ids)
        ]
        if not removable:
            return 0

        carried: dict[AccountId, Decimal] = {}
        for entry in removable:
            carried[entry.account] = carried.get(entry.account, ZERO) + entry.amount
            del self._entries[entry.entry_id]
        self._order = [entry_id for entry_id in self._order if entry_id in self._entries]

        stamp = cutoff.isoformat()
        for account, amount in carried.items():
            self.append(
                LedgerEntry(
                    entry_id=f"carry:{stamp}:{account.key}",
                    account=account,
                    kind=EntryKind.CARRY_FORWARD,
                    amount=amount,
                    detail=f"net of {len(removable)} entries pruned before {stamp}",
                    created_at=cutoff,
                )
            )
        return len(removable)
