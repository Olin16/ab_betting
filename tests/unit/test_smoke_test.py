"""Offline tests for the supervised smoke-test safety boundary."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from event_arb.candidates import (
    CandidateRecord,
    DiscoveryLimits,
    DiscoveryReport,
    build_request_plan,
)
from event_arb.config import (
    DataSource,
    IngestionMode,
    KalshiEnvironment,
    KalshiSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import VenueProduct
from event_arb.domain.models import MarketPair
from event_arb.matching.readiness import ReadinessReport
from event_arb.matching.rules_diff import RulesComparison
from event_arb.smoke_test import (
    SmokeRequestGuard,
    SmokeTestError,
    _validate_preflight,
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


_ROW1 = "https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=250"
_ROW2 = "https://gateway.polymarket.us/v1/markets?active=true&closed=false&limit=250"


async def _fill_the_budget(guard: SmokeRequestGuard) -> None:
    """Send exactly the 31 requests §2 allows, one row at a time."""
    await guard.on_request(httpx.Request("GET", _ROW1))
    await guard.on_request(httpx.Request("GET", _ROW2))
    for index in range(25):
        await guard.on_request(
            httpx.Request("GET", f"https://gateway.polymarket.us/v1/market/slug/slug-{index}")
        )
    for index in range(2):
        await guard.on_request(
            httpx.Request(
                "GET",
                f"https://external-api.kalshi.com/trade-api/v2/markets/TICK-{index}/orderbook",
            )
        )
    for index in range(2):
        await guard.on_request(
            httpx.Request("GET", f"https://gateway.polymarket.us/v1/markets/id-{index}/book")
        )


@pytest.mark.asyncio
async def test_the_row_maxima_sum_to_the_documented_thirty_one() -> None:
    """§2's total is the sum of §1's per-row counts, not a separate number to keep in step."""
    guard = SmokeRequestGuard()
    guard.configure(production_plan())

    await _fill_the_budget(guard)

    assert len(guard.records) == 31


@pytest.mark.asyncio
async def test_guard_refuses_the_thirty_second_request() -> None:
    """The ceiling is enforced at send time, not only asserted in the derived plan."""
    guard = SmokeRequestGuard()
    guard.configure(production_plan())
    await _fill_the_budget(guard)

    with pytest.raises(SmokeTestError, match="request budget exceeded"):
        await guard.on_request(httpx.Request("GET", _ROW1))

    assert len(guard.records) == 31


@pytest.mark.asyncio
async def test_guard_refuses_a_write_verb_on_an_allowed_url() -> None:
    """Read-only is a property of the guard, not only of the clients that call through it."""
    guard = SmokeRequestGuard()
    guard.configure(production_plan())

    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(SmokeTestError, match="unexpected endpoint"):
            await guard.on_request(httpx.Request(verb, _ROW1))

    assert guard.records == []


@pytest.mark.asyncio
async def test_guard_refuses_an_undeclared_parameter() -> None:
    """§2: a returned cursor is counted and reported, never acted on.

    A cursor is followed by re-requesting the catalogue with an extra parameter, so the
    guard refusing undeclared parameters is what makes "pagination is not followed"
    structural rather than a promise about the calling code.
    """
    guard = SmokeRequestGuard()
    guard.configure(production_plan())

    with pytest.raises(SmokeTestError, match="unexpected endpoint or params"):
        await guard.on_request(httpx.Request("GET", _ROW1 + "&cursor=next-page"))

    assert guard.records == []


@pytest.mark.asyncio
async def test_guard_refuses_a_second_catalogue_page() -> None:
    """Even with §1's exact parameters, row 1 is allowed once."""
    guard = SmokeRequestGuard()
    guard.configure(production_plan())
    await guard.on_request(httpx.Request("GET", _ROW1))

    with pytest.raises(SmokeTestError, match="request-row budget exceeded"):
        await guard.on_request(httpx.Request("GET", _ROW1))

    assert len(guard.records) == 1


def _live_settings(
    *,
    kalshi: KalshiSettings | None = None,
    polymarket: PolymarketSettings | None = None,
    ingestion_mode: IngestionMode = IngestionMode.REST_POLLING,
    data_source: DataSource = DataSource.LIVE,
) -> Settings:
    """The posture §7.3 requires, so each test can spoil exactly one part of it."""
    return Settings(
        data_source=data_source,
        kalshi=kalshi or KalshiSettings(environment=KalshiEnvironment.PROD),
        polymarket=polymarket or PolymarketSettings(product=PolymarketProduct.US),
        ingestion_mode=ingestion_mode,
        database_url="sqlite+aiosqlite:///:memory:",
    )


def test_preflight_accepts_a_new_file_under_the_temporary_directory(tmp_path: Path) -> None:
    _validate_preflight(_live_settings(), tmp_path / "smoke.db")


def test_preflight_refuses_the_trial_database() -> None:
    with pytest.raises(SmokeTestError, match=re.escape("data/event_arb.db")):
        _validate_preflight(_live_settings(), Path("data/event_arb.db"))


def test_preflight_refuses_a_path_outside_the_temporary_directory() -> None:
    """The dedicated database must not be able to land next to real trial evidence."""
    with pytest.raises(SmokeTestError, match="temporary directory"):
        _validate_preflight(_live_settings(), Path("build/smoke-not-temp.db"))


def test_preflight_refuses_an_existing_database(tmp_path: Path) -> None:
    """A fresh file is the point: appending to a previous run would blur the evidence."""
    existing = tmp_path / "already-here.db"
    existing.touch()

    with pytest.raises(SmokeTestError, match="already exists"):
        _validate_preflight(_live_settings(), existing)


def test_preflight_refuses_configured_credentials(tmp_path: Path) -> None:
    """§1 and §7.3 both promise a credential-free run; hold the preflight to it."""
    credentialled = KalshiSettings(
        environment=KalshiEnvironment.PROD,
        api_key_id="key-id",
        private_key_path=tmp_path / "key.pem",
    )
    assert credentialled.has_credentials

    with pytest.raises(SmokeTestError, match="credentials are configured"):
        _validate_preflight(_live_settings(kalshi=credentialled), tmp_path / "smoke.db")


def test_preflight_refuses_the_demo_environment(tmp_path: Path) -> None:
    """The question is whether *production* still matches, so demo cannot stand in."""
    demo = KalshiSettings(environment=KalshiEnvironment.DEMO)

    with pytest.raises(SmokeTestError, match="production"):
        _validate_preflight(_live_settings(kalshi=demo), tmp_path / "smoke.db")


def test_preflight_refuses_polymarket_global(tmp_path: Path) -> None:
    """Global is a different product under different terms and is never this test's target."""
    global_product = PolymarketSettings(
        product=PolymarketProduct.GLOBAL, global_terms_eligibility_confirmed=True
    )

    with pytest.raises(SmokeTestError, match="Polymarket US"):
        _validate_preflight(_live_settings(polymarket=global_product), tmp_path / "smoke.db")


def test_preflight_refuses_websocket_ingestion(tmp_path: Path) -> None:
    """Both venues sign the WebSocket handshake, so streaming cannot be credential-free."""
    with pytest.raises(SmokeTestError, match="REST polling"):
        _validate_preflight(
            _live_settings(ingestion_mode=IngestionMode.WEBSOCKET), tmp_path / "smoke.db"
        )


def test_preflight_refuses_fixture_mode(tmp_path: Path) -> None:
    """A fixture run would produce a green result having contacted nothing."""
    with pytest.raises(SmokeTestError, match="DATA_SOURCE=live"):
        _validate_preflight(_live_settings(data_source=DataSource.FIXTURE), tmp_path / "smoke.db")
