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
from event_arb.config import OpportunitySettings, Settings
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
