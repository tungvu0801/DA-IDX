"""
api/server.py — FastAPI application entry point.

Run with (from the stock-agent/ directory, with the venv active):
    uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload

Serves the JSON API under /api/* and the static dashboard frontend at /.
Missing Alpaca credentials do NOT prevent the server from starting — the
dashboard shell still loads, and individual endpoints return a clear 503
until .env is filled in. This module never imports Alpaca's trading
client, so nothing here can place an order.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from api.routes import (agent, ai_explain, alerts, alpaca_paper, alpaca_paper_orders, backtests, daily_brief, daily_brief_delivery,
                        daily_review,
                        evidence_comparison, explanation_history, paper,
                        forward_automation, forward_tests,
                        insights, market, ops_status, portfolio, research, research_history, saved_scans, stock_decision, stocks,
                        strategies, strategy_fit, strategy_scanner, trade_review, watchlist)
from api.routes import portfolio_rotation
from api.routes import research_workflow
from api.routes import portfolio_backtest
from api.routes import portfolio_walkforward
from api.routes import model_campaign
from api.routes import rotation_diagnostics
from database.database import get_db
from models.schemas import HealthResponse

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

@asynccontextmanager
async def _lifespan(_app):
    """Stage 2.7G.1: start warming the Stage 2.6 event calendars in a daemon thread. Returns immediately — startup
    never waits for FRED, a failure is only logged, and no Claude call is made."""
    if config.EVENT_WARMUP_ON_STARTUP:
        try:
            from insights.warmup import start_event_warmup
            start_event_warmup()
        except Exception as exc:  # noqa: BLE001 - warming is an optimisation; the server must start regardless
            logging.getLogger(__name__).warning("Event calendar warm-up could not start: %s", exc)
    # Stage 4.4: desktop notifications already ON (e.g. enabled in Stage 4.3) keep their per-user notification identity
    try:
        from notifications import delivery as _dlv
        from notifications import identity as _ident
        if _dlv.DeliveryStore().settings()["enabled"]:
            _ident.register()
    except Exception as exc:  # noqa: BLE001 - branding is optional; the server must start regardless
        logging.getLogger(__name__).warning("Notification identity not registered: %s", type(exc).__name__)
    # Stage 3.7: the opt-in automatic forward capture starts only if the user turned it on (default OFF)
    try:
        from forward.automation import start_if_enabled
        start_if_enabled()
    except Exception as exc:  # noqa: BLE001 - automation must never stop the server from starting
        logging.getLogger(__name__).warning("AUTO_CAPTURE could not start: %s", type(exc).__name__)
    yield
    try:
        from forward.automation import shutdown
        shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("AUTO_CAPTURE did not stop cleanly: %s", type(exc).__name__)
    try:                                                    # Stage 4.4: icon removed, window destroyed, loop stopped
        from notifications import windows as _nw
        _nw.shutdown()
    except Exception as exc:  # noqa: BLE001 - shutting down regardless
        logging.getLogger(__name__).warning("Desktop notification adapter did not stop cleanly: %s", type(exc).__name__)


app = FastAPI(
    lifespan=_lifespan,
    title="Stock Agent Dashboard API",
    description=(
        "Market monitoring, watchlist, and AI technical-analysis endpoints. "
        "Analysis and research only — this API never places, modifies, or "
        "cancels a trade."
    ),
    version="1.0.0",
)

# Initializes data/stock_agent.db and runs (idempotent) migrations once per
# process. Deliberately at import time, not a startup event, so the database
# is ready before the first request even under test clients that skip lifespan.
get_db()

app.include_router(market.router)
app.include_router(watchlist.router)
app.include_router(stocks.router)
app.include_router(research.router)
app.include_router(research_history.router)
app.include_router(alerts.router)
app.include_router(agent.router)
app.include_router(portfolio.router)  # Stage 2.7C read-only portfolio (feature-flagged, off by default)
app.include_router(insights.router)  # Stage 2.7E beginner market context (read-only)
app.include_router(trade_review.router)  # Stage 2.7E educational trader review (read-only)
app.include_router(daily_review.router)  # Stage 2.7G portfolio + watchlist decision report (read-only)
app.include_router(ops_status.router)  # Stage 2.7G.1 operational status (read-only, no external calls)
app.include_router(stock_decision.router)  # Stage 2.7G.3 one-stock decision after Analyze (read-only)
app.include_router(strategies.router)  # Stage 3.1 Strategy Lab: versioned strategy definitions (no backtest, no orders)
app.include_router(backtests.router)  # Stage 3.2 historical backtests of saved BACKTEST READY versions (no orders, no AI)
app.include_router(forward_tests.router)  # Stage 3.3 forward-test signal journal: manual captures only (no orders, no AI)
app.include_router(strategy_fit.router)  # Stage 3.4 Strategy Fit: current rule fit of saved versions (read-only, no AI)
app.include_router(evidence_comparison.router)  # Stage 3.5: stored historical vs forward evidence (read-only, no AI)
app.include_router(forward_automation.router)  # Stage 3.7: opt-in automatic after-close forward capture (capture only)
app.include_router(ai_explain.router)  # Stage 3.8: explicit, grounded AI explanation of Strategy Fit / Evidence (<=1 call)
app.include_router(explanation_history.router)  # Stage 3.9: optional, local, append-only AI explanation history
app.include_router(strategy_scanner.router)  # Stage 4.0: deterministic read-only Strategy Scanner (one version, one list)
app.include_router(saved_scans.router)  # Stage 4.1: saved scans (opt-in, local) + in-app RULES MET change alerts
app.include_router(saved_scans.alerts_router)
app.include_router(daily_brief.router)  # Stage 4.2: read-only Daily Brief of stored data (GET only)
app.include_router(daily_brief_delivery.router)  # Stage 4.3: opt-in local desktop delivery of the Daily Brief
app.include_router(paper.portfolio_router)  # Stage 4.5: LOCAL paper portfolio — simulated, no broker, no real order
app.include_router(paper.orders_router)
app.include_router(paper.fills_router)
app.include_router(alpaca_paper.router)  # Stage 4.6A: Alpaca PAPER account, READ ONLY (explicit refresh; no broker action)
app.include_router(alpaca_paper_orders.router)  # Stage 4.6B: manual PAPER orders — preview, then explicit click Confirm only
app.include_router(portfolio_rotation.router)  # Stage 4.7: deterministic portfolio rotation PROPOSALS (display only; no order path)
app.include_router(research_workflow.router)  # research workflow: shortlist-before-LLM, cached, budgeted; research only, no order path
app.include_router(portfolio_backtest.router)  # Stage 4.8: historical rotation backtest — research only, nothing is traded
app.include_router(portfolio_walkforward.router)  # Stage 4.9: walk-forward robustness — research only, nothing is traded or deployed
app.include_router(model_campaign.router)  # Stage 5.0: model evaluation campaign / leaderboard — research only, nothing is traded or activated
app.include_router(rotation_diagnostics.router)  # Stage 5.1: attribution & benchmark diagnostics — research only


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        alpaca_configured=config.has_credentials(),
        llm_configured=config.has_llm_credentials(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):  # noqa: ANN001 - FastAPI signature
    """Last-resort catch-all so an unexpected error never leaks internals (or a stack trace) to the client."""
    logging.getLogger(__name__).error("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Static dashboard frontend (plain HTML/CSS/JS — see project plan for why,
# pending a Node.js/React upgrade). Mounted last so it doesn't shadow /api/*.
app.mount("/", StaticFiles(directory=str(config.PROJECT_ROOT / "frontend"), html=True), name="frontend")
