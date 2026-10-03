"""
api/routes/ai_explain.py — Stage 3.8 GROUNDED AI EXPLANATION (explicit, task-specific, at most 1 Claude call per action).

POST /api/ai-explain/strategy-fit/preview   what "Explain this setup" would cost (0 calls; a local explanation is returned
                                            directly when no AI call is needed)
POST /api/ai-explain/strategy-fit           explain ONE strategy version's Strategy Fit result for ONE symbol
POST /api/ai-explain/evidence/preview       what "Explain this evidence" would cost (0 calls)
POST /api/ai-explain/evidence               explain ONE version's selected Evidence view

Bodies only NAME a deterministic object (strict: unknown fields are rejected; there is no prompt field). The server
loads the authoritative result itself — the exact Strategy Fit result it returned, or the stored Evidence view — and
never accepts financial facts from the browser. No market data, broker or order is involved.

Stage 3.9: every response carries an additive "history" block. Only when the user turned AI explanation history ON is
an ACCEPTED explanation (status OK or LOCAL) appended to ai_explanation_history (ai_explain/history.py); otherwise
nothing is written. History never changes the explanation, the AI cache or any financial result.
"""
import logging
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_explain import history as H
from ai_explain import service as X

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai-explain", tags=["ai-explain"])
HKIND = {X.FIT: H.FIT, X.EVIDENCE: H.EVIDENCE}
_ID = r"^[0-9a-f]{32}$"
_FP = r"^[0-9a-f]{64}$"


class FitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_version_id: str = Field(pattern=_ID)
    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$")
    decision_session: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    evaluated_at: str = Field(min_length=10, max_length=40)
    input_fingerprint: Optional[str] = Field(default=None, pattern=_FP)


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_version_id: str = Field(pattern=_ID)
    backtest_run_id: Optional[str] = Field(default=None, pattern=_ID)
    forward_journal_id: Optional[str] = Field(default=None, pattern=_ID)
    input_fingerprint: Optional[str] = Field(default=None, pattern=_FP)


def _err(exc: X.ExplainError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message}, status_code=exc.status)


def _fit(body: FitBody):
    payload, local = X.fit_request(body.strategy_version_id, body.symbol, body.decision_session, body.evaluated_at)
    ident = {"strategy_version_id": body.strategy_version_id, "symbol": payload["grounded_in"]["symbol"],
             "decision_session": body.decision_session}
    return payload, local, ident


def _evidence(body: EvidenceBody):
    payload, local, v = X.evidence_request(body.strategy_version_id, body.backtest_run_id, body.forward_journal_id,
                                           with_view=True)
    sel = v.get("selection") or {}
    return payload, local, {"strategy_version_id": body.strategy_version_id, "backtest_run_id": sel.get("backtest_run_id"),
                            "forward_journal_id": sel.get("forward_journal_id")}


def _history(kind: str, ident: dict, payload: dict, res: dict, preview: bool = False) -> dict:
    """Additive "history" block. A preview saves only a LOCAL explanation (that preview IS what the user is shown)."""
    try:
        if preview and res.get("mode") != "LOCAL":
            block = {"enabled": H.enabled(), "saved": False, "history_id": None}
        elif preview:
            shown = {**res, "status": "LOCAL", "claude_calls": 0, "cache_hit": False}
            block = H.after_explain(HKIND[kind], ident, payload, shown)
        else:
            block = H.after_explain(HKIND[kind], ident, payload, res)
    except Exception as exc:  # noqa: BLE001 - history is optional: it must never break an explanation
        logger.error("AI explanation history could not be saved: %s", exc)
        block = {"enabled": True, "saved": False, "history_id": None, "error": "History could not be saved."}
    return {**res, "history": block}


@router.post("/strategy-fit/preview")
def fit_preview(body: FitBody) -> JSONResponse:
    try:
        payload, local, ident = _fit(body)
        return JSONResponse(_history(X.FIT, ident, payload, X.preview(X.FIT, payload, local), preview=True))
    except X.ExplainError as exc:
        return _err(exc)


@router.post("/strategy-fit")
def fit_explain(body: FitBody) -> JSONResponse:
    try:
        payload, local, ident = _fit(body)
        return JSONResponse(_history(X.FIT, ident, payload, X.explain(X.FIT, payload, local, body.input_fingerprint)))
    except X.ExplainError as exc:
        return _err(exc)


@router.post("/evidence/preview")
def evidence_preview(body: EvidenceBody) -> JSONResponse:
    try:
        payload, local, ident = _evidence(body)
        return JSONResponse(_history(X.EVIDENCE, ident, payload, X.preview(X.EVIDENCE, payload, local), preview=True))
    except X.ExplainError as exc:
        return _err(exc)


@router.post("/evidence")
def evidence_explain(body: EvidenceBody) -> JSONResponse:
    try:
        payload, local, ident = _evidence(body)
        return JSONResponse(_history(X.EVIDENCE, ident, payload, X.explain(X.EVIDENCE, payload, local, body.input_fingerprint)))
    except X.ExplainError as exc:
        return _err(exc)
