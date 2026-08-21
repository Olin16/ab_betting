"""The only execution adapter in this build: one that cannot trade.

Every method raises :class:`LiveTradingDisabledError`. This is not a feature flag that a
config change can flip — there is no alternative implementation to switch to. Enabling
live trading requires writing new code, which is exactly the friction intended.
"""

from __future__ import annotations

from decimal import Decimal

from event_arb.domain.enums import Venue
from event_arb.execution.base import ExecutionAdapter, OrderRequest, OrderResult
from event_arb.observability.logging import get_logger

__all__ = ["DisabledExecutionAdapter", "LiveTradingDisabledError"]

log = get_logger(__name__)

_MESSAGE = (
    "Live trading is not implemented in this build. Phase 7 was intentionally not "
    "built: the scanner and paper-trading results must be reviewed first, and live "
    "execution requires an explicit, separate implementation with the safety controls "
    "described in docs/operations.md."
)


class LiveTradingDisabledError(RuntimeError):
    """Raised on any attempt to reach a live order path."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{_MESSAGE} {detail}".strip())


class DisabledExecutionAdapter(ExecutionAdapter):
    """Refuses every order operation, regardless of configuration."""

    def __init__(self, trading_enabled: bool = False) -> None:
        # Recorded only so that a misconfiguration is visible in logs. It grants nothing:
        # even trading_enabled=True cannot make this adapter submit an order.
        self._configured_trading_enabled = trading_enabled
        if trading_enabled:
            log.warning(
                "execution.trading_enabled_ignored",
                reason="live trading is not implemented; the setting has no effect",
            )

    async def submit(self, request: OrderRequest) -> OrderResult:
        log.error(
            "execution.submit_blocked",
            venue=request.venue.value,
            market_id=request.market_id,
            quantity=str(request.quantity),
        )
        raise LiveTradingDisabledError(
            f"refused to submit {request.quantity} on {request.venue.value}"
        )

    async def cancel(self, venue: Venue, venue_order_id: str) -> OrderResult:
        raise LiveTradingDisabledError(f"refused to cancel on {venue.value}")

    async def get_order(self, venue: Venue, client_order_id: str) -> OrderResult:
        raise LiveTradingDisabledError(f"refused to query orders on {venue.value}")

    @staticmethod
    def zero_result(venue: Venue, client_order_id: str) -> OrderResult:
        """A never-filled result, for tests that need the shape without a submission."""
        return OrderResult(
            venue=venue,
            client_order_id=client_order_id,
            venue_order_id=None,
            state="rejected",
            filled_quantity=Decimal("0"),
            average_price=Decimal("0"),
            message=_MESSAGE,
        )
