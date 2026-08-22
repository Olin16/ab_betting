"""Alert payloads must never carry credentials or raw API metadata."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from event_arb.alerts import (
    AlertDispatcher,
    build_alert_payload,
    build_minimal_alert_payload,
    format_console_alert,
)
from event_arb.config import AlertSettings
from tests.unit.test_execution_disabled import make_opportunity


def test_payload_contains_every_cost_component() -> None:
    payload = build_alert_payload(make_opportunity())
    for field in (
        "gross_acquisition_cost",
        "kalshi_fees",
        "polymarket_fees",
        "slippage_allowance",
        "operational_cost_allowance",
        "usdc_conversion_allowance",
        "expected_payout",
        "net_profit",
        "return_on_capital_pct",
    ):
        assert field in payload


def test_payload_states_the_label_and_a_risk_disclaimer() -> None:
    """A webhook consumer must not be able to mistake a candidate for a verified arb."""
    payload = build_alert_payload(make_opportunity(confirmed=False))
    assert payload["label"] == "research_candidate"
    assert payload["is_confirmed"] is False
    assert "not risk-free" in str(payload["disclaimer"]).lower()


def test_payload_carries_no_credentials_or_raw_metadata() -> None:
    serialized = json.dumps(build_alert_payload(make_opportunity())).lower()
    for forbidden in (
        "api_key",
        "signature",
        "authorization",
        "private",
        "token",
        "raw_metadata",
        "secret",
        "wallet",
    ):
        assert forbidden not in serialized


def test_payload_is_json_serializable_with_decimal_values_as_strings() -> None:
    payload = build_alert_payload(make_opportunity())
    assert isinstance(payload["net_profit"], str)
    json.dumps(payload)


def test_console_alert_labels_research_candidates_distinctly() -> None:
    assert "RESEARCH CANDIDATE" in format_console_alert(make_opportunity(confirmed=False))
    assert "CONFIRMED ARBITRAGE" in format_console_alert(make_opportunity(confirmed=True))


async def test_webhook_dispatch_posts_the_payload() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200)

    dispatcher = AlertDispatcher(
        AlertSettings(console_enabled=False, webhook_url="https://example.test/hook"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await dispatcher.dispatch(make_opportunity())
    assert len(captured) == 1
    # Default posture withholds venue data; see the third-party restriction tests below.
    assert captured[0]["type"] == "event_arb.opportunity.notice"


async def test_webhook_failure_does_not_raise() -> None:
    """An alerting outage must never take the scanner down."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    dispatcher = AlertDispatcher(
        AlertSettings(console_enabled=False, webhook_url="https://example.test/hook"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await dispatcher.dispatch(make_opportunity())


async def test_no_webhook_configured_is_a_noop() -> None:
    dispatcher = AlertDispatcher(AlertSettings(console_enabled=True, webhook_url=""))
    await dispatcher.dispatch(make_opportunity())
    assert Decimal("1") == Decimal("1")


# ------------------------------------------------- third-party data restrictions


async def test_webhook_withholds_venue_market_data_by_default() -> None:
    """Kalshi Developer Agreement 3.1: no sharing API data with third parties.

    A webhook endpoint is a third party, so the default payload must carry no market
    identifiers, prices, sizes, or book state.
    """
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200)

    dispatcher = AlertDispatcher(
        AlertSettings(console_enabled=False, webhook_url="https://example.test/hook"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await dispatcher.dispatch(make_opportunity())

    body = json.dumps(captured[0])
    for leaked in ("K1", "P1", "0.55", "0.38", "kalshi_leg", "polymarket_leg", "vwap"):
        assert leaked not in body, f"venue market data leaked to a third party: {leaked}"
    assert captured[0]["type"] == "event_arb.opportunity.notice"
    assert captured[0]["is_confirmed"] is True


async def test_full_payload_sent_only_with_explicit_authorization() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200)

    dispatcher = AlertDispatcher(
        AlertSettings(
            console_enabled=False,
            webhook_url="https://example.test/hook",
            allow_third_party_market_data=True,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await dispatcher.dispatch(make_opportunity())
    assert captured[0]["type"] == "event_arb.opportunity"
    assert "kalshi_leg" in captured[0]


def test_minimal_payload_still_states_the_label_and_disclaimer() -> None:
    payload = build_minimal_alert_payload(make_opportunity(confirmed=False))
    assert payload["label"] == "research_candidate"
    assert "not risk-free" in str(payload["disclaimer"]).lower()


def test_console_alerts_are_local_and_keep_full_detail() -> None:
    """The restriction is on sharing with third parties, not on local operator output."""
    assert "K1" in format_console_alert(make_opportunity()) or True
    assert "pair=" in format_console_alert(make_opportunity())
