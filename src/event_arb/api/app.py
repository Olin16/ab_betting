"""FastAPI application: dashboard pages, JSON endpoints, health, and metrics.

The dashboard is server-rendered Jinja2 with no build step and no client framework. The
data here is tabular and refreshed on demand; a SPA would add a toolchain and an attack
surface for no benefit.

Every page that displays an opportunity shows its label prominently. A *research
candidate* must never be mistaken for a verified arbitrage because of UI ambiguity.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from event_arb.alerts import AlertDispatcher
from event_arb.config import IngestionMode, Settings, get_settings
from event_arb.domain.keys import pair_market_keys
from event_arb.domain.models import ReviewedSource
from event_arb.matching.readiness import assess_readiness
from event_arb.matching.rules_diff import compare_rules
from event_arb.observability import metrics
from event_arb.observability.logging import configure_logging, get_logger
from event_arb.persistence.database import Database
from event_arb.runtime import TrialRunner
from event_arb.scanner import Scanner

__all__ = ["create_app"]

log = get_logger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _reviewed_source(raw: Any, recorded_by: str) -> ReviewedSource | None:
    """Build a reviewed source from a request body, or None when absent.

    Requires both the authority id and the evidence establishing it; a bare name with no
    evidence is an assertion, not a verification.
    """
    if not isinstance(raw, dict):
        return None
    canonical = str(raw.get("canonical_id", "")).strip()
    evidence = str(raw.get("evidence", "")).strip()
    if not canonical:
        return None
    return ReviewedSource(canonical_id=canonical, evidence=evidence, recorded_by=recorded_by)


def _require_auth(config: Settings) -> Callable[[Request], None]:
    """Guard state-changing endpoints when a token is configured.

    Comparison is constant-time: a token check that leaks timing is a token check that
    can be brute-forced.
    """

    def dependency(request: Request) -> None:
        expected = config.api_auth_token.get_secret_value()
        if not expected:
            # Permitted only on loopback; validate_exposure() enforces that at startup.
            return
        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    return dependency


def create_app(settings: Settings | None = None, scanner: Scanner | None = None) -> FastAPI:
    config = settings or get_settings()
    configure_logging(config.log_level, config.log_format)
    # Fail closed before a socket is opened, not after.
    config.validate_exposure()
    database = None if scanner is not None else Database(config.database_url)
    runtime = scanner or Scanner(config, database=database)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Loud, unmissable confirmation of the safety posture at every boot.
        log.info(
            "app.startup",
            data_source=config.data_source.value,
            trading_enabled=config.trading_enabled,
            live_trading_possible=config.live_trading_possible,
        )
        if runtime.database is not None:
            await runtime.database.create_all()
        await runtime.discover()
        # Restore before seeding so stored approvals are re-verified against current
        # rules; a rules change while we were down must suspend, not reinstate.
        report = await runtime.restore()
        if report.suspended:
            log.warning("app.startup.approvals_suspended", **report.as_dict())
        # Pairs must exist before books are refreshed: the refresh is scoped to the
        # markets those pairs reference, so refreshing first would target nothing.
        runtime.seed_watched_pairs()
        runtime.generate_pairs()
        await runtime.persist_markets()
        await runtime.persist_pairs()
        await runtime.refresh_books()

        trial: TrialRunner | None = None
        if config.trial.enabled:
            trial = TrialRunner(
                runtime,
                config,
                alerts=AlertDispatcher(config.alerts),
                # Honour the explicit ingestion mode. Deriving this from "are we live?"
                # was wrong: both venues require an authenticated WebSocket handshake
                # even for public market data, so going live must not silently start a
                # stream that cannot authenticate.
                use_websockets=config.ingestion_mode is IngestionMode.WEBSOCKET,
            )
            app.state.trial = trial
            await trial.start()

        yield

        if trial is not None:
            await trial.stop()
        await runtime.close()

    app = FastAPI(
        lifespan=lifespan,
        title="event-arb",
        description=(
            "Cross-venue prediction-market arbitrage scanner. "
            "Read-only market data and paper trading; live trading is not implemented."
        ),
        version="0.1.0",
    )
    app.state.scanner = runtime
    app.state.trial = None
    app.state.settings = config

    # ------------------------------------------------------------------ operational

    @app.get("/health", response_class=JSONResponse)
    async def health() -> dict[str, Any]:
        return runtime.health().as_dict()

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus_metrics() -> Response:
        return Response(content=metrics.render_metrics(), media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------------- dashboard

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            context={"settings": config, **context},
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        result = runtime.state.last_scan or runtime.scan()
        return render(
            request,
            "index.html",
            health=runtime.health(),
            result=result,
            pairs=list(runtime.state.pairs.values()),
            markets=len(runtime.state.markets),
        )

    @app.get("/adapters", response_class=HTMLResponse)
    async def adapters(request: Request) -> HTMLResponse:
        return render(request, "adapters.html", health=runtime.health())

    @app.get("/markets", response_class=HTMLResponse)
    async def markets(request: Request) -> HTMLResponse:
        return render(
            request,
            "markets.html",
            markets=sorted(runtime.state.markets.values(), key=lambda m: (m.venue.value, m.title)),
        )

    @app.get("/pairs", response_class=HTMLResponse)
    async def pairs(request: Request) -> HTMLResponse:
        return render(
            request,
            "pairs.html",
            pairs=sorted(
                runtime.state.pairs.values(), key=lambda p: p.semantic_score, reverse=True
            ),
        )

    @app.get("/pairs/{pair_id}", response_class=HTMLResponse)
    async def pair_review(request: Request, pair_id: str) -> HTMLResponse:
        pair = runtime.state.pairs.get(pair_id)
        if pair is None:
            raise HTTPException(status_code=404, detail="pair not found")
        kalshi_k, polymarket_k = pair_market_keys(pair)
        kalshi = runtime.state.markets.get(kalshi_k)
        polymarket = runtime.state.markets.get(polymarket_k)
        if kalshi is None or polymarket is None:
            raise HTTPException(status_code=404, detail="market metadata missing")
        comparison = compare_rules(kalshi, polymarket)
        return render(
            request,
            "pair_review.html",
            pair=pair,
            kalshi=kalshi,
            polymarket=polymarket,
            comparison=comparison,
            readiness=assess_readiness(
                pair, kalshi, polymarket, differences=comparison.differences
            ),
            kalshi_book=runtime.store.get(kalshi_k),
            polymarket_book=runtime.store.get(polymarket_k),
        )

    @app.get("/opportunities", response_class=HTMLResponse)
    async def opportunities(request: Request) -> HTMLResponse:
        result = runtime.state.last_scan or runtime.scan()
        return render(request, "opportunities.html", result=result)

    @app.get("/paper", response_class=HTMLResponse)
    async def paper(request: Request) -> HTMLResponse:
        return render(
            request,
            "paper.html",
            portfolio=runtime.portfolio,
            report=runtime.portfolio.daily_report(),
            exposure=runtime.portfolio.open_exposure(),
            trades=runtime.state.paper_trades,
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit(request: Request) -> HTMLResponse:
        events: list[dict[str, Any]] = []
        for pair in runtime.state.pairs.values():
            if pair.approved_at is not None:
                events.append(
                    {
                        "when": pair.approved_at.isoformat(),
                        "actor": pair.approved_by or "unknown",
                        "action": "pair.approved",
                        "subject": pair.id,
                        "detail": pair.reviewer_notes,
                    }
                )
            elif pair.status.value == "suspended":
                events.append(
                    {
                        "when": "-",
                        "actor": "system",
                        "action": "pair.suspended",
                        "subject": pair.id,
                        "detail": pair.reviewer_notes,
                    }
                )
        events.sort(key=lambda item: str(item["when"]), reverse=True)
        return render(request, "audit.html", events=events)

    # ------------------------------------------------------------------ JSON API

    auth = Depends(_require_auth(config))

    @app.post("/api/scan", response_class=JSONResponse, dependencies=[auth])
    async def api_scan() -> dict[str, Any]:
        books = await runtime.refresh_books()
        await runtime.record_books(books)
        result = await runtime.scan_and_persist()
        return {
            "scanned_at": result.scanned_at.isoformat(),
            "confirmed": len(result.confirmed),
            "research_candidates": len(result.research_candidates),
            "rejections": [
                {
                    "pair_id": r.pair_id,
                    "direction": r.direction.value,
                    "reason": r.reason.value,
                    "detail": r.detail,
                }
                for r in result.rejections
            ],
            "opportunities": [o.model_dump(mode="json") for o in result.opportunities],
        }

    @app.post("/api/pairs/{pair_id}/approve", response_class=JSONResponse, dependencies=[auth])
    async def api_approve(pair_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve a pair. Requires an identified reviewer."""
        approved_by = str(payload.get("approved_by", "")).strip()
        if not approved_by:
            raise HTTPException(status_code=400, detail="approved_by is required")
        if pair_id not in runtime.state.pairs:
            raise HTTPException(status_code=404, detail="pair not found")
        try:
            # No override exists: a contradictory pair cannot be acknowledged into
            # equivalence. A reviewer may record the resolution authority they verified
            # for a leg whose venue publishes none — but both legs must resolve to the
            # same authority, which readiness enforces.
            pair = await runtime.approve_and_persist(
                pair_id,
                approved_by,
                str(payload.get("reviewer_notes", "")),
                kalshi_reviewed_source=_reviewed_source(
                    payload.get("kalshi_reviewed_source"), approved_by
                ),
                polymarket_reviewed_source=_reviewed_source(
                    payload.get("polymarket_reviewed_source"), approved_by
                ),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return pair.model_dump(mode="json")

    @app.get("/api/trial", response_class=JSONResponse)
    async def trial_status() -> dict[str, Any]:
        """Live trial counters: scans, detections, paper trades, errors."""
        active: TrialRunner | None = app.state.trial
        if active is None:
            return {"running": False, "reason": "trial.enabled is false"}
        return active.status()

    @app.get("/api/trading-status", response_class=JSONResponse)
    async def trading_status() -> dict[str, Any]:
        """Explicit, machine-readable confirmation that this build cannot trade."""
        return {
            "trading_enabled_setting": config.trading_enabled,
            "live_trading_possible": config.live_trading_possible,
            "reason": (
                "Phase 7 is not implemented. The only execution adapter refuses all "
                "submissions unconditionally."
            ),
        }

    return app
