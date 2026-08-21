"""Console and webhook alerting.

Webhook payloads are built from an explicit allowlist of fields. That is deliberate:
serializing an opportunity wholesale would eventually leak whatever new field someone
adds upstream, and webhooks go to third-party endpoints. No credential, header, signed
request, or raw API metadata ever enters a payload.
"""

from __future__ import annotations

from typing import Any

import httpx

from event_arb.config import AlertSettings
from event_arb.domain.models import Opportunity
from event_arb.observability.logging import get_logger

__all__ = ["AlertDispatcher", "build_alert_payload"]

log = get_logger(__name__)


def build_alert_payload(opportunity: Opportunity) -> dict[str, Any]:
    """Build a webhook-safe payload from an explicit field allowlist."""
    return {
        "type": "event_arb.opportunity",
        # The label is first so a reader cannot miss whether this is a verified pair.
        "label": opportunity.label.value,
        "is_confirmed": opportunity.is_confirmed,
        "pair_id": opportunity.pair_id,
        "direction": opportunity.direction.value,
        "executable_quantity": str(opportunity.executable_quantity),
        "gross_acquisition_cost": str(opportunity.gross_acquisition_cost),
        "expected_payout": str(opportunity.expected_payout),
        "kalshi_fees": str(opportunity.kalshi_fees),
        "polymarket_fees": str(opportunity.polymarket_fees),
        "slippage_allowance": str(opportunity.slippage_allowance),
        "operational_cost_allowance": str(opportunity.operational_cost_allowance),
        "usdc_conversion_allowance": str(opportunity.usdc_conversion_allowance),
        "net_profit": str(opportunity.net_profit),
        "return_on_capital_pct": str(opportunity.return_on_capital),
        "kalshi_leg": {
            "market_id": opportunity.kalshi_leg.market_id,
            "outcome": opportunity.kalshi_leg.outcome.value,
            "vwap": str(opportunity.kalshi_leg.vwap),
            "book_age_ms": opportunity.kalshi_leg.book_age_ms,
        },
        "polymarket_leg": {
            "market_id": opportunity.polymarket_leg.market_id,
            "outcome": opportunity.polymarket_leg.outcome.value,
            "vwap": str(opportunity.polymarket_leg.vwap),
            "book_age_ms": opportunity.polymarket_leg.book_age_ms,
        },
        "risk_flags": [flag.value for flag in opportunity.risk_flags],
        "detected_at": opportunity.detected_at.isoformat(),
        "disclaimer": (
            "Not risk-free. Cross-venue execution is not atomic and settlement "
            "equivalence is only asserted for manually approved pairs."
        ),
    }


def format_console_alert(opportunity: Opportunity) -> str:
    tag = "CONFIRMED ARBITRAGE" if opportunity.is_confirmed else "RESEARCH CANDIDATE"
    flags = ", ".join(flag.value for flag in opportunity.risk_flags) or "none"
    return (
        f"[{tag}] pair={opportunity.pair_id} direction={opportunity.direction.value} "
        f"qty={opportunity.executable_quantity} cost=${opportunity.gross_acquisition_cost} "
        f"net=${opportunity.net_profit} roi={opportunity.return_on_capital}% flags={flags}"
    )


class AlertDispatcher:
    """Sends opportunity alerts to the configured channels."""

    def __init__(self, settings: AlertSettings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    async def dispatch(self, opportunity: Opportunity) -> None:
        if self._settings.console_enabled:
            log.info("alert.console", message=format_console_alert(opportunity))

        if not self._settings.webhook_url:
            return

        payload = build_alert_payload(opportunity)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.webhook_timeout_seconds)
            )
        try:
            response = await self._client.post(self._settings.webhook_url, json=payload)
            if response.status_code >= 400:
                log.warning("alert.webhook_failed", status_code=response.status_code)
        except httpx.HTTPError as exc:
            # An alerting failure must never take down the scanner.
            log.warning("alert.webhook_error", error=type(exc).__name__)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
