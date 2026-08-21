"""Pre-trade safety controls (Phase 7 scaffolding).

These controls are written and tested *now*, before any submission path exists, so that
the eventual decision to enable live trading is a decision about one narrow adapter
rather than about an entire unwritten safety system.

:class:`TradingControls` implements a defence in depth. Every check must pass; the first
failure denies the trade and says why. The ordering is deliberate — the cheapest and most
absolute checks come first, so a kill-switched or geoblocked system never even prices a
trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from event_arb.domain.enums import SettlementAsset
from event_arb.domain.models import MarketPair, Opportunity
from event_arb.domain.money import ZERO

__all__ = ["ControlDecision", "TradingControls", "TradingLimits"]


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def denied_reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else ""


@dataclass(frozen=True)
class TradingLimits:
    max_order_quantity: Decimal = Decimal("100")
    max_notional_per_order: Decimal = Decimal("500")
    daily_loss_limit: Decimal = Decimal("100")
    max_total_exposure: Decimal = Decimal("2000")
    min_balance: Decimal = Decimal("100")
    max_open_unhedged_quantity: Decimal = Decimal("0")


@dataclass
class TradingControls:
    """Stateful guard evaluated before any hypothetical order."""

    limits: TradingLimits = field(default_factory=TradingLimits)
    trading_enabled: bool = False
    startup_confirmed: bool = False
    geoblock_allowed: bool = False
    manual_approval_required: bool = True
    kill_switch_engaged: bool = False
    circuit_breaker_tripped: bool = False
    consecutive_failures: int = 0
    circuit_breaker_threshold: int = 3
    realized_loss_today: Decimal = ZERO
    open_exposure: Decimal = ZERO
    open_unhedged_quantity: Decimal = ZERO
    balances: dict[SettlementAsset, Decimal] = field(default_factory=dict)
    _loss_day: date = field(default_factory=lambda: datetime.now(UTC).date())

    # ------------------------------------------------------------------- switches

    def engage_kill_switch(self, reason: str = "manual") -> None:
        """Hard stop. Never cleared automatically."""
        self.kill_switch_engaged = True
        self._kill_reason = reason

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            self.circuit_breaker_tripped = True

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_loss(self, amount: Decimal, when: datetime | None = None) -> None:
        """Accumulate realized loss, resetting at the day boundary."""
        today = (when or datetime.now(UTC)).date()
        if today != self._loss_day:
            self._loss_day = today
            self.realized_loss_today = ZERO
        if amount > ZERO:
            self.realized_loss_today += amount

    # --------------------------------------------------------------------- checks

    def evaluate(
        self,
        opportunity: Opportunity,
        pair: MarketPair,
        manually_approved_trade: bool = False,
    ) -> ControlDecision:
        """Return whether a hypothetical trade would be permitted, and why not."""
        reasons: list[str] = []

        if self.kill_switch_engaged:
            reasons.append("kill switch engaged")
        if self.circuit_breaker_tripped:
            reasons.append(
                f"circuit breaker tripped after {self.consecutive_failures} consecutive failures"
            )
        if not self.trading_enabled:
            reasons.append("TradingEnabled is false")
        if not self.startup_confirmed:
            reasons.append("startup confirmation not provided")
        if not self.geoblock_allowed:
            reasons.append("geographic/compliance check has not affirmatively passed")

        if not pair.is_tradable_pair:
            reasons.append("market pair is not manually approved")
        if not opportunity.is_confirmed:
            reasons.append("opportunity is not labeled confirmed")
        if self.manual_approval_required and not manually_approved_trade:
            reasons.append("per-trade manual approval required")

        quantity = opportunity.executable_quantity
        notional = opportunity.gross_acquisition_cost
        if quantity > self.limits.max_order_quantity:
            reasons.append(f"quantity {quantity} exceeds max {self.limits.max_order_quantity}")
        if notional > self.limits.max_notional_per_order:
            reasons.append(f"notional {notional} exceeds max {self.limits.max_notional_per_order}")
        if self.realized_loss_today >= self.limits.daily_loss_limit:
            reasons.append(
                f"daily loss {self.realized_loss_today} has reached the limit "
                f"{self.limits.daily_loss_limit}"
            )
        if self.open_exposure + notional > self.limits.max_total_exposure:
            reasons.append("trade would exceed the maximum total exposure limit")
        if self.open_unhedged_quantity > self.limits.max_open_unhedged_quantity:
            reasons.append(
                f"existing unhedged exposure {self.open_unhedged_quantity} must be "
                "resolved before opening new positions"
            )

        for asset, required in (
            (SettlementAsset.USD, opportunity.kalshi_leg.gross_cost),
            (SettlementAsset.USDC, opportunity.polymarket_leg.gross_cost),
        ):
            available = self.balances.get(asset, ZERO)
            if available - required < self.limits.min_balance:
                reasons.append(
                    f"{asset.value} balance {available} would fall below the minimum "
                    f"{self.limits.min_balance}"
                )

        return ControlDecision(allowed=not reasons, reasons=tuple(reasons))
