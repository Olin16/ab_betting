"""Offline tests for the supervised smoke-test safety boundary."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from event_arb.candidates import (
    CandidateRecord,
    DiscoveryLimits,
    DiscoveryReport,
    build_request_plan,
)
from event_arb.domain.enums import VenueProduct
from event_arb.domain.models import MarketPair
from event_arb.matching.readiness import ReadinessReport
from event_arb.matching.rules_diff import RulesComparison
from event_arb.smoke_test import (
    SmokeRequestGuard,
    SmokeTestError,
    assert_documented_request_contract,
    select_smoke_candidates,
)


class StubAdapter:
    def __init__(self, url: str, credentials: bool = False) -> None:
        self.discovery_base_url = url
        self.sends_credentials = credentials


def production_plan(credentials: bool = False):  # type: ignore[no-untyped-def]
    return build_request_plan(
        DiscoveryLimits(),
        StubAdapter("https://external-api.kalshi.com/trade-api/v2", credentials),
        StubAdapter("https://gateway.polymarket.us"),
        VenueProduct.POLYMARKET_US,
        orderbook_pairs=2,
    )


def record(style: str, index: int, score: str) -> CandidateRecord:
    title = {
        "fed": "Will the Federal Reserve cut rates at the FOMC meeting?",
        "cpi": "Will CPI inflation exceed 3.0%?",
        "weather": "Will the temperature exceed 90 degrees?",
        "other": "Will the event happen?",
    }[style]
    pair = MarketPair(
        id=f"pair-{index}",
        kalshi_market_id=f"K-{index}",
        polymarket_market_id=f"P-{index}",
        polymarket_product=VenueProduct.POLYMARKET_US,
        semantic_score=Decimal(score),
    )
    return CandidateRecord(
        pair=pair,
        score=Decimal(score),
        kalshi_title=title,
        polymarket_title=title,
        comparison=RulesComparison(differences=()),
        readiness=ReadinessReport(),
    )


def test_production_plan_matches_documented_contract() -> None:
    assert_documented_request_contract(production_plan())


def test_documented_contract_refuses_credentials() -> None:
    with pytest.raises(SmokeTestError, match="credentials"):
        assert_documented_request_contract(production_plan(credentials=True))


def test_smoke_test_has_no_authenticated_mode_escape_hatch() -> None:
    """§1 and §7.3 both promise credentials cannot be waived. Hold the parser to it.

    ``--authenticated-mode`` is a real flag on ``candidates discover``, and §1 of the plan
    once said it applied here too. It does not, and the operator is asked to authorize §1
    as written, so the absence is pinned rather than left to prose.
    """
    from event_arb.main import build_parser

    parser = build_parser()
    parser.parse_args(["smoke-test", "--database", "/tmp/does-not-matter.db"])

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["smoke-test", "--database", "/tmp/does-not-matter.db", "--authenticated-mode"]
        )


def test_selection_prefers_two_distinct_settlement_styles() -> None:
    report = DiscoveryReport(
        records=[
            record("other", 0, "0.99"),
            record("cpi", 1, "0.8"),
            record("fed", 2, "0.7"),
        ]
    )
    selected = select_smoke_candidates(report)
    assert [item.pair.id for item in selected] == ["pair-2", "pair-1"]


@pytest.mark.asyncio
async def test_guard_rejects_an_unexpected_endpoint_before_send() -> None:
    guard = SmokeRequestGuard()
    guard.configure(production_plan())
    request = httpx.Request("GET", "https://api.polymarket.us/v1/markets")
    with pytest.raises(SmokeTestError, match="unexpected endpoint"):
        await guard.on_request(request)
    assert guard.records == []


@pytest.mark.asyncio
async def test_guard_rejects_credentials_before_send() -> None:
    guard = SmokeRequestGuard()
    guard.configure(production_plan())
    request = httpx.Request(
        "GET",
        "https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=250",
        headers={"Authorization": "secret"},
    )
    with pytest.raises(SmokeTestError, match="credential"):
        await guard.on_request(request)
    assert guard.records == []


@pytest.mark.asyncio
async def test_guard_records_status_and_elapsed_time() -> None:
    guard = SmokeRequestGuard()
    guard.configure(production_plan())
    request = httpx.Request(
        "GET",
        "https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=250",
    )
    await guard.on_request(request)
    await guard.on_response(httpx.Response(200, request=request))
    assert len(guard.records) == 1
    assert guard.records[0].status == 200
    assert guard.records[0].elapsed_seconds is not None
