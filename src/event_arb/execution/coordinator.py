"""Two-leg execution coordination (Phase 7 scaffolding — refuses to run).

A real coordinator would sequence leg 1 and leg 2, resolve unknown order states after
timeouts, and unwind exposure when the second leg fails. None of that is implemented.
:class:`ExecutionCoordinator` exists to hold the shape and to guarantee that the disabled
adapter is the only thing wired into it.
"""

from __future__ import annotations

from event_arb.domain.models import MarketPair, Opportunity
from event_arb.execution.base import ExecutionAdapter
from event_arb.execution.controls import ControlDecision, TradingControls
from event_arb.execution.disabled import DisabledExecutionAdapter, LiveTradingDisabledError
from event_arb.observability.logging import get_logger

__all__ = ["ExecutionCoordinator"]

log = get_logger(__name__)


class ExecutionCoordinator:
    """Gate in front of any execution adapter. Denies by default."""

    def __init__(
        self,
        controls: TradingControls | None = None,
        adapter: ExecutionAdapter | None = None,
    ) -> None:
        self.controls = controls or TradingControls()
        # Defaulting to the disabled adapter means a caller who forgets to pass one gets
        # the safe behaviour rather than an accidental live path.
        self.adapter = adapter or DisabledExecutionAdapter()

    def would_allow(
        self, opportunity: Opportunity, pair: MarketPair, manually_approved_trade: bool = False
    ) -> ControlDecision:
        """Evaluate the controls without attempting anything."""
        return self.controls.evaluate(opportunity, pair, manually_approved_trade)

    async def execute(
        self, opportunity: Opportunity, pair: MarketPair, manually_approved_trade: bool = False
    ) -> None:
        """Always raises. Live execution is not implemented in this build."""
        decision = self.would_allow(opportunity, pair, manually_approved_trade)
        log.error(
            "execution.blocked",
            pair_id=pair.id,
            allowed_by_controls=decision.allowed,
            reasons=list(decision.reasons),
        )
        raise LiveTradingDisabledError(
            f"controls_allowed={decision.allowed}; {decision.denied_reason}"
        )
