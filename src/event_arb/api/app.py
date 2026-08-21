"""FastAPI application: dashboard pages, JSON endpoints, health, and metrics.

The dashboard is server-rendered Jinja2 with no build step and no client framework. The
data here is tabular and refreshed on demand; a SPA would add a toolchain and an attack
surface for no benefit.

Every page that displays an opportunity shows its label prominently. A *research
candidate* must never be mistaken for a verified arbitrage because of UI ambiguity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from event_arb.config import Settings, get_settings
from event_arb.domain.enums import Venue
from event_arb.matching.rules_diff import compare_rules
from event_arb.observability import metrics
from event_arb.observability.logging import configure_logging, get_logger
from event_arb.scanner import Scanner

__all__ = ["create_app"]

log = get_logger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(settings: Settings | None = None, scanner: Scanner | None = None) -> FastAPI:
    config = settings or get_settings()
    configure_logging(config.log_level, config.log_format)
    runtime = scanner or Scanner(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Loud, unmissable confirmation of the safety posture at every boot.
        log.info(
            "app.startup",
            data_source=config.data_source.value,
            trading_enabled=config.trading_enabled,
            live_trading_possible=config.live_trading_possible,
        )
        await runtime.discover()
        await runtime.refresh_books()
        runtime.generate_pairs()
        yield
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
        kalshi = runtime.state.markets.get((Venue.KALSHI, pair.kalshi_market_id))
        polymarket = runtime.state.markets.get((Venue.POLYMARKET, pair.polymarket_market_id))
        if kalshi is None or polymarket is None:
            raise HTTPException(status_code=404, detail="market metadata missing")
        return render(
            request,
            "pair_review.html",
            pair=pair,
            kalshi=kalshi,
            polymarket=polymarket,
            comparison=compare_rules(kalshi, polymarket),
            kalshi_book=runtime.store.get(Venue.KALSHI, pair.kalshi_market_id),
            polymarket_book=runtime.store.get(Venue.POLYMARKET, pair.polymarket_market_id),
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

    @app.post("/api/scan", response_class=JSONResponse)
    async def api_scan() -> dict[str, Any]:
        await runtime.refresh_books()
        result = runtime.scan()
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

    @app.post("/api/pairs/{pair_id}/approve", response_class=JSONResponse)
    async def api_approve(pair_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve a pair. Requires an identified reviewer."""
        approved_by = str(payload.get("approved_by", "")).strip()
        if not approved_by:
            raise HTTPException(status_code=400, detail="approved_by is required")
        if pair_id not in runtime.state.pairs:
            raise HTTPException(status_code=404, detail="pair not found")
        try:
            pair = runtime.approve(
                pair_id,
                approved_by,
                str(payload.get("reviewer_notes", "")),
                bool(payload.get("acknowledge_material_differences", False)),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return pair.model_dump(mode="json")

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
