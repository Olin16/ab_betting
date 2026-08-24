"""Operational hardening.

An unattended trial changes the risk profile of the dashboard: `/api/pairs/{id}/approve`
is what turns a research candidate into something the system will call a confirmed
arbitrage, and it changes state. These tests pin that it cannot be reached anonymously
from a network interface.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from event_arb.api.app import create_app
from event_arb.config import (
    DataSource,
    IngestionMode,
    OpportunitySettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.scanner import Scanner

TOKEN = "test-token-not-a-real-secret"


def base_settings(fixture_dir: Path, **overrides: object) -> Settings:
    return Settings(
        fixture_dir=fixture_dir,
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
        **overrides,  # type: ignore[arg-type]
    )


def client_for(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, Scanner(settings))) as client:
        yield client


# --------------------------------------------------------------- exposure guard


def test_non_loopback_bind_without_a_token_is_refused(fixture_dir: Path) -> None:
    """Fail closed at startup rather than quietly exposing the approval endpoint."""
    settings = base_settings(fixture_dir, api_host="0.0.0.0")
    with pytest.raises(ValueError, match="EVENT_ARB_API_AUTH_TOKEN"):
        settings.validate_exposure()


def test_non_loopback_bind_with_a_token_is_allowed(fixture_dir: Path) -> None:
    settings = base_settings(fixture_dir, api_host="0.0.0.0", api_auth_token=SecretStr(TOKEN))
    settings.validate_exposure()


def test_loopback_needs_no_token(fixture_dir: Path) -> None:
    base_settings(fixture_dir, api_host="127.0.0.1").validate_exposure()


def test_externally_restricted_bind_is_an_explicit_acknowledgement(
    fixture_dir: Path,
) -> None:
    """Containers bind 0.0.0.0 legitimately; the operator must say so deliberately."""
    settings = base_settings(
        fixture_dir, api_host="0.0.0.0", api_bind_is_externally_restricted=True
    )
    settings.validate_exposure()


# ------------------------------------------------------- Polymarket product guard


def test_fixture_mode_does_not_require_a_live_polymarket_product(fixture_dir: Path) -> None:
    base_settings(fixture_dir).validate_live_market_data()


def test_polymarket_us_live_mode_is_permitted_with_rest_polling(fixture_dir: Path) -> None:
    """The public gateway needs no key, so REST polling is credential-free."""
    settings = base_settings(
        fixture_dir,
        data_source=DataSource.LIVE,
        ingestion_mode=IngestionMode.REST_POLLING,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    settings.validate_live_market_data()


def test_polymarket_us_websocket_mode_is_refused(fixture_dir: Path) -> None:
    """The US WebSocket lives on the authenticated API, so it cannot run without keys."""
    settings = base_settings(
        fixture_dir,
        data_source=DataSource.LIVE,
        ingestion_mode=IngestionMode.WEBSOCKET,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    with pytest.raises(ValueError, match="authenticated API"):
        settings.validate_live_market_data()


def test_default_ingestion_mode_is_credential_free(fixture_dir: Path) -> None:
    """Going live must not imply a stream that cannot authenticate."""
    assert base_settings(fixture_dir).ingestion_mode is IngestionMode.REST_POLLING


def test_polymarket_global_live_mode_requires_explicit_eligibility(fixture_dir: Path) -> None:
    settings = base_settings(
        fixture_dir,
        data_source=DataSource.LIVE,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
    )
    with pytest.raises(ValueError, match="Global live access requires explicit"):
        settings.validate_live_market_data()


def test_polymarket_global_acknowledgement_is_explicit(fixture_dir: Path) -> None:
    settings = base_settings(
        fixture_dir,
        data_source=DataSource.LIVE,
        polymarket=PolymarketSettings(
            product=PolymarketProduct.GLOBAL,
            global_terms_eligibility_confirmed=True,
        ),
    )
    settings.validate_live_market_data()


def test_app_refuses_to_build_when_exposed(fixture_dir: Path) -> None:
    settings = base_settings(fixture_dir, api_host="0.0.0.0")
    with pytest.raises(ValueError):
        create_app(settings, Scanner(settings))


# ------------------------------------------------------------------- bearer auth


@pytest.fixture
def authed_client(fixture_dir: Path) -> Iterator[TestClient]:
    settings = base_settings(fixture_dir, api_host="0.0.0.0", api_auth_token=SecretStr(TOKEN))
    yield from client_for(settings)


def test_approval_requires_the_token(authed_client: TestClient) -> None:
    scanner: Scanner = authed_client.app.state.scanner  # type: ignore[attr-defined]
    pair_id = next(iter(scanner.state.pairs))
    response = authed_client.post(
        f"/api/pairs/{pair_id}/approve", json={"approved_by": "attacker@example.com"}
    )
    assert response.status_code == 401


def test_scan_requires_the_token(authed_client: TestClient) -> None:
    assert authed_client.post("/api/scan").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["", "Bearer ", "Bearer wrong-token", "wrong-token", "Basic dXNlcjpwYXNz"],
)
def test_bad_credentials_are_rejected(authed_client: TestClient, header: str) -> None:
    response = authed_client.post("/api/scan", headers={"Authorization": header})
    assert response.status_code == 401


def test_correct_token_is_accepted(authed_client: TestClient) -> None:
    response = authed_client.post("/api/scan", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_read_only_pages_remain_available(authed_client: TestClient) -> None:
    """The guard is on state change; health and metrics stay reachable for monitoring."""
    assert authed_client.get("/health").status_code == 200
    assert authed_client.get("/metrics").status_code == 200


def test_loopback_deployment_needs_no_token(fixture_dir: Path) -> None:
    settings = base_settings(fixture_dir)
    for client in client_for(settings):
        assert client.post("/api/scan").status_code == 200
        break


def test_global_client_refuses_us_configured_settings() -> None:
    """Defence in depth beneath the settings-level guard.

    PolymarketClient hard-codes Global hosts and Global identifier semantics. Constructing
    it from a US-configured settings object would send US-intended traffic to the
    international platform, which is a different venue with different terms.
    """
    from event_arb.venues.polymarket.client import PolymarketClient

    with pytest.raises(ValueError, match="Polymarket US requires its own adapter"):
        PolymarketClient(PolymarketSettings(product=PolymarketProduct.US))


def test_global_client_accepts_global_settings() -> None:
    from event_arb.venues.polymarket.client import PolymarketClient

    client = PolymarketClient(PolymarketSettings(product=PolymarketProduct.GLOBAL))
    assert client.venue.value == "POLYMARKET"


def test_no_polymarket_client_exposes_order_entry() -> None:
    """Neither product may acquire a submission surface."""
    from event_arb.venues.polymarket.client import PolymarketClient

    forbidden = {"submit", "submit_order", "place_order", "create_order", "buy", "sell"}
    assert not (forbidden & set(dir(PolymarketClient)))
