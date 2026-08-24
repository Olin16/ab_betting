"""Dashboard and JSON API."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from event_arb.api.app import create_app
from event_arb.config import OpportunitySettings, PolymarketProduct, PolymarketSettings, Settings
from event_arb.scanner import Scanner


@pytest.fixture
def client(fixture_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        # Legacy Polymarket **Global** corpus. Stated explicitly so this path stays
        # clearly separated from the Polymarket US fixtures and tests.
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )
    # Entered as a context manager so the lifespan handler runs discovery and matching.
    with TestClient(create_app(settings, Scanner(settings))) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "path", ["/", "/adapters", "/markets", "/pairs", "/opportunities", "/paper", "/audit"]
)
def test_dashboard_pages_render(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_every_page_states_that_live_trading_is_off(client: TestClient) -> None:
    """The safety posture must be unmissable, on every page."""
    for path in ("/", "/opportunities", "/paper"):
        assert "Live trading is not implemented" in client.get(path).text


def test_opportunities_page_labels_research_candidates(client: TestClient) -> None:
    body = client.get("/opportunities").text
    assert "research candidate" in body


def test_pair_review_shows_side_by_side_rules_and_differences(client: TestClient) -> None:
    app_scanner: Scanner = client.app.state.scanner  # type: ignore[attr-defined]
    pair_id = next(pid for pid, p in app_scanner.state.pairs.items() if p.has_material_differences)
    body = client.get(f"/pairs/{pair_id}").text
    assert "Side-by-side rules" in body
    assert "material rule difference" in body
    assert "comparison_operator" in body


def test_unknown_pair_returns_404(client: TestClient) -> None:
    assert client.get("/pairs/nonexistent").status_code == 404


def test_health_endpoint_reports_live_trading_impossible(client: TestClient) -> None:
    payload = client.get("/health").json()
    assert payload["live_trading_possible"] is False
    assert "adapters" in payload


def test_metrics_endpoint_exposes_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "event_arb_" in response.text


def test_trading_status_endpoint_is_explicit(client: TestClient) -> None:
    payload = client.get("/api/trading-status").json()
    assert payload["live_trading_possible"] is False
    assert payload["trading_enabled_setting"] is False


def test_scan_endpoint_returns_costed_opportunities(client: TestClient) -> None:
    payload = client.post("/api/scan").json()
    assert "opportunities" in payload and "rejections" in payload
    for opportunity in payload["opportunities"]:
        assert opportunity["label"] in {"confirmed", "research_candidate"}
        assert "net_profit" in opportunity


def test_approval_requires_an_identified_reviewer(client: TestClient) -> None:
    app_scanner: Scanner = client.app.state.scanner  # type: ignore[attr-defined]
    pair_id = next(iter(app_scanner.state.pairs))
    assert client.post(f"/api/pairs/{pair_id}/approve", json={}).status_code == 400


def test_approval_of_a_pair_with_material_differences_is_refused(client: TestClient) -> None:
    app_scanner: Scanner = client.app.state.scanner  # type: ignore[attr-defined]
    pair_id = next(pid for pid, p in app_scanner.state.pairs.items() if p.has_material_differences)
    response = client.post(
        f"/api/pairs/{pair_id}/approve", json={"approved_by": "reviewer@example.com"}
    )
    assert response.status_code == 400
    assert "not ready for approval" in response.json()["detail"]


def test_approval_succeeds_for_an_equivalent_pair(client: TestClient) -> None:
    app_scanner: Scanner = client.app.state.scanner  # type: ignore[attr-defined]
    pair_id = next(
        pid for pid, p in app_scanner.state.pairs.items() if not p.has_material_differences
    )
    response = client.post(
        f"/api/pairs/{pair_id}/approve",
        json={"approved_by": "reviewer@example.com", "reviewer_notes": "verified manually"},
    )
    assert response.status_code == 200
    assert response.json()["manually_approved"] is True


def test_pair_review_distinguishes_the_approval_states(client: TestClient) -> None:
    """Similarity, differences, missing info, blockers, readiness and approval are
    six distinct facts and must not be collapsed into one."""
    app_scanner: Scanner = client.app.state.scanner  # type: ignore[attr-defined]
    pair_id = next(iter(app_scanner.state.pairs))
    body = client.get(f"/pairs/{pair_id}").text

    for heading in (
        "Similarity score",
        "Rule differences",
        "Missing information",
        "Unapprovable reasons",
        "Ready for human approval",
        "Approved",
    ):
        assert heading in body, f"pair review does not show {heading!r}"


def test_pair_review_states_there_is_no_acknowledgement_override(
    client: TestClient,
) -> None:
    body = client.get(f"/pairs/{next(iter(client.app.state.scanner.state.pairs))}").text  # type: ignore[attr-defined]
    assert "no acknowledgement override" in body
    assert "acknowledge_material_differences" not in body
