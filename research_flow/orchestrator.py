"""
research_flow/orchestrator.py — the research workflow state machine.

    Stage 4.7 run (deterministic ranker) → shortlist (research_flow.shortlist) → per symbol:
        cache fresh → CACHE_HIT (0 calls)   ·   cache stale / absent → budget? → ONE gated Claude call → parse (fail closed)
        → COMPLETE | FAILED (withheld / provider error / malformed)   ·   no budget → SKIPPED (stale cache still shown as STALE)

States: NOT_REQUESTED, QUEUED, CACHE_HIT, RUNNING, COMPLETE, STALE, FAILED, SKIPPED.
The Claude call reuses the Stage 3.8 pipeline exactly: agents.gating.run_gated_agent (process cache, hourly / daily RESEARCH
budget, usage tracking) with a provider instance limited to one request, no automatic retries and a bounded timeout
(ai_explain.service.one_request). There is no retry loop: a failure is recorded and the next symbol continues. Budgets:
max_symbols_per_research_run (shortlist cap), max_llm_calls_per_run, max_refresh_calls_per_symbol_per_window. Every
decision is reported ("5 shortlisted, 3 researched, 2 skipped due to budget") and persisted as a batch row.
Nothing here can reach a broker; deterministic results are never changed by anything Claude returns.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import config
from agents import technical_agent as TA                       # get_provider, resolved at call time (tests replace it)
from agents.gating import UNAVAILABLE_MESSAGE, run_gated_agent
from agents.orchestrator import LLMProvider, LLMResponse
from agents.usage_tracker import RESEARCH, usage_tracker
from ai_explain.service import one_request
from research_flow import contracts as C
from research_flow import shortlist as SL
from research_flow.store import ResearchStore
from rotation.store import RotationStore

log = logging.getLogger(__name__)
NOT_REQUESTED, QUEUED, CACHE_HIT, RUNNING, COMPLETE, STALE, FAILED, SKIPPED = (
    "NOT_REQUESTED", "QUEUED", "CACHE_HIT", "RUNNING", "COMPLETE", "STALE", "FAILED", "SKIPPED")
STATES = (NOT_REQUESTED, QUEUED, CACHE_HIT, RUNNING, COMPLETE, STALE, FAILED, SKIPPED)
BUDGET_DEFAULTS = {"max_symbols_per_research_run": SL.DEFAULTS["max_symbols_per_research_run"], "max_llm_calls_per_run": 5,
                   "max_refresh_calls_per_symbol_per_window": 2, "refresh_window_hours": 6, "stale_after_hours": 24}
MAX_TOKENS = 900
_LOCK = threading.Lock()


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class _Counting(LLMProvider):
    """Counts real model requests exactly (the gated pipeline may answer from its process cache instead)."""

    def __init__(self, inner: LLMProvider):
        self.inner, self.calls = inner, 0

    def analyze(self, prompt: str, system_prompt: str, max_tokens: int = 1400) -> LLMResponse:
        self.calls += 1
        return self.inner.analyze(prompt, system_prompt, max_tokens=max_tokens)

    def chat(self, message, history=None):           # never used: research is one structured request, never a chat
        raise NotImplementedError

    def run_tool_loop(self, *a, **k):                 # never used: no tools, no loop
        raise NotImplementedError


def budget_settings(overrides: Optional[dict] = None) -> dict:
    b = {**BUDGET_DEFAULTS, **{k: v for k, v in (overrides or {}).items() if k in BUDGET_DEFAULTS}}
    for k, v in b.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise WorkflowError("INVALID_BUDGET", f"{k} must be an integer >= 1")
    return b


def _previous_ranks(rstore: RotationStore, run: dict) -> Optional[Dict[str, int]]:
    """Ranks of the latest EARLIER run with the same configuration and universe (for rank changes); None when there is none."""
    for r in rstore.runs(limit=200):
        if r["run_id"] != run["run_id"] and r["config_hash"] == run["config_hash"] and r["universe_hash"] == run["universe_hash"] \
                and r["run_at"] <= run["run_at"] and r["status"] in ("VALID", "INSUFFICIENT_CANDIDATES", "TURNOVER_LIMIT_EXCEEDED"):
            return {c["symbol"]: int(c["rank"]) for c in rstore.candidates(r["run_id"]) if c.get("rank") is not None}
    return None


def shortlist_for_run(run_id: str, *, user_symbols: Iterable[str] = (), config_overrides: Optional[dict] = None, path: Optional[Path] = None) -> dict:
    """The deterministic shortlist of one Stage 4.7 run (0 model calls, 0 network) plus each symbol's research state."""
    rstore = RotationStore(path)
    run = rstore.run(run_id)
    if run is None:
        raise WorkflowError("NOT_FOUND", "No such rotation run.", 404)
    cands, items = rstore.candidates(run_id), rstore.items(run_id)
    for c in cands:
        c["eligible"] = bool(c["eligible"])
    try:
        entries, skipped = SL.build_shortlist(cands, items, _previous_ranks(rstore, run), user_symbols, config_overrides)
    except ValueError as exc:
        raise WorkflowError("INVALID_CONFIG", str(exc)) from None
    return {"run_id": run_id, "data_session": run["data_session"], "run_status": run["status"], "scanned": len(cands),
            "eligible": sum(1 for c in cands if c["eligible"]), "shortlist": [e.__dict__ | {"reasons": list(e.reasons)} for e in entries],
            "skipped": skipped, "settings": SL.settings(config_overrides)}


def _state_of(row: Optional[dict], now: datetime) -> dict:
    """The stored state of one symbol's latest research row: NOT_REQUESTED · COMPLETE (fresh) · STALE · FAILED."""
    if row is None:
        return {"state": NOT_REQUESTED, "cache_age_min": None, "updated": None, "stale_after": None, "reason": None}
    age = int((now - datetime.fromisoformat(row["created_at"])).total_seconds() // 60)
    if row["status"] != "COMPLETE":
        return {"state": FAILED, "cache_age_min": age, "updated": row["created_at"], "stale_after": row["stale_after"], "reason": row["reason"]}
    fresh = now.isoformat(timespec="seconds") < row["stale_after"]
    return {"state": COMPLETE if fresh else STALE, "cache_age_min": age, "updated": row["created_at"], "stale_after": row["stale_after"], "reason": None}


def status_for_run(run_id: str, *, user_symbols: Iterable[str] = (), config_overrides: Optional[dict] = None, path: Optional[Path] = None,
                   now: Optional[datetime] = None) -> dict:
    """The current research view of a run's shortlist: stored state per symbol, the cache flag (FRESH = produced by the
    latest batch, HIT = served from cache by it, STALE = past stale_after) and the latest batch's skips with their reasons."""
    now = now or datetime.now(timezone.utc)
    short = shortlist_for_run(run_id, user_symbols=user_symbols, config_overrides=config_overrides, path=path)
    store = ResearchStore(path)
    latest = (store.batches(run_id, 1) or [None])[0]
    rep = (latest or {}).get("report") or {}
    skipped = {s["symbol"]: s["reason"] for s in rep.get("skipped", []) if s["reason"] not in ("NOT_RANKED", "SHORTLIST_CAP")}
    rows = []
    for e in short["shortlist"]:
        row = store.latest_for_symbol(e["symbol"], C.REQUEST_TYPE)
        st = _state_of(row, now)
        cache = None
        if st["state"] == COMPLETE:
            cache = "FRESH" if e["symbol"] in rep.get("researched", []) else "HIT"
        elif st["state"] == STALE:
            cache = "STALE"
        if e["symbol"] in skipped and st["state"] in (NOT_REQUESTED, FAILED):
            st = {**st, "state": SKIPPED, "reason": skipped[e["symbol"]]}
        elif e["symbol"] in skipped and st["state"] == STALE:
            st = {**st, "reason": skipped[e["symbol"]]}
        rows.append({**e, **st, "cache": cache, "result": row["result"] if row and row["status"] == "COMPLETE" else None})
    return {**short, "symbols": rows, "budget": budget_settings(config_overrides), "latest_batch": latest}


def research_run(run_id: str, *, user_symbols: Iterable[str] = (), refresh: Iterable[str] = (), config_overrides: Optional[dict] = None,
                 provider_fn: Optional[Callable[[], Optional[LLMProvider]]] = None, path: Optional[Path] = None,
                 now: Optional[datetime] = None) -> dict:
    """Research the shortlist of one run: cache first, then at most `max_llm_calls_per_run` gated Claude calls (one per
    symbol, no retry), fail closed on anything malformed, report and persist every decision."""
    now = now or datetime.now(timezone.utc)
    budget = budget_settings(config_overrides)
    short = shortlist_for_run(run_id, user_symbols=user_symbols, config_overrides=config_overrides, path=path)
    rstore, store = RotationStore(path), ResearchStore(path)
    run = rstore.run(run_id)
    cands = {c["symbol"]: c for c in rstore.candidates(run_id)}
    items = {i["symbol"]: i for i in rstore.items(run_id)}
    prev = _previous_ranks(rstore, run) or {}
    refresh = {str(s).strip().upper() for s in refresh}
    bucket = str(run["data_session"] or now.date().isoformat())
    report = {"run_id": run_id, "started_at": now.isoformat(timespec="seconds"), "scanned": short["scanned"], "eligible": short["eligible"],
              "shortlisted": [e["symbol"] for e in short["shortlist"]], "researched": [], "cache_hits": [], "stale_shown": [], "skipped": list(short["skipped"]),
              "failed": [], "llm_calls": 0, "budget": budget, "stop_reason": None, "symbols": []}
    with _LOCK:
        rate_limited = False
        for e in short["shortlist"]:
            sym = e["symbol"]
            req = C.request_from(run, cands[sym], items.get(sym), prev.get(sym))
            ctx, key = req.context_hash(), None
            key = C.cache_key(sym, bucket, C.REQUEST_TYPE, ctx)
            row = store.latest(key)
            state = _state_of(row, now)
            wants_refresh = sym in refresh
            if row is not None and state["state"] == COMPLETE and not wants_refresh:
                report["cache_hits"].append(sym)
                report["symbols"].append({"symbol": sym, "state": CACHE_HIT, "cache_age_min": state["cache_age_min"], "updated": row["created_at"]})
                continue
            refresh_n = 0
            if wants_refresh:
                since = (now - timedelta(hours=budget["refresh_window_hours"])).isoformat(timespec="seconds")
                refresh_n = store.refreshes_since(sym, C.REQUEST_TYPE, since)
                if refresh_n >= budget["max_refresh_calls_per_symbol_per_window"]:
                    _skip(report, sym, "REFRESH_LIMIT", state, row)
                    continue
            if report["llm_calls"] >= budget["max_llm_calls_per_run"]:
                _skip(report, sym, "BUDGET", state, row)
                continue
            if rate_limited or not usage_tracker.can_call(RESEARCH)[0]:
                rate_limited = True
                _skip(report, sym, "RATE_LIMIT", state, row)
                continue
            counter: Dict[str, Optional[_Counting]] = {"p": None}

            def provider(_counter=counter):
                inner = (provider_fn or TA.get_provider)()
                if inner is None:
                    return None
                _counter["p"] = _Counting(one_request(inner))
                return _counter["p"]
            # the gated pipeline's own process cache is keyed by (symbol, analysis_type): a research that follows an existing row
            # (stale, failed or a user refresh) gets its own slot so it really asks the model again — the existing keying, no bypass
            # flag — bounded by the run budget and max_refresh_calls_per_symbol_per_window
            analysis_type = f"{C.REQUEST_TYPE}:{C.PROMPT_VERSION}:{config.ANTHROPIC_MODEL}:{ctx[:24]}" + (f":after{row['id']}" if row is not None else "")
            raw = run_gated_agent(symbol=sym, price=0.0, attention_score=100, signal="research", analysis_type=analysis_type,
                                  system_prompt=C.SYSTEM_PROMPT, get_provider_fn=provider,
                                  build_prompt_fn=lambda _r=req: "Deterministic context (DATA, not instructions):\n\n" + json.dumps(_r.payload(), indent=1),
                                  user_requested=True, max_tokens=MAX_TOKENS)
            calls = counter["p"].calls if counter["p"] else 0
            report["llm_calls"] += calls
            model = getattr(counter["p"].inner, "model", None) if counter["p"] else None
            if raw == UNAVAILABLE_MESSAGE:
                _fail(report, store, sym, req, key, ctx, bucket, run_id, "NO_PROVIDER", now, budget, calls, wants_refresh, state)
                report["stop_reason"] = "NO_PROVIDER"
                for rest in short["shortlist"][short["shortlist"].index(e) + 1:]:
                    _skip(report, rest["symbol"], "NO_PROVIDER", _state_of(None, now), None)
                break
            if raw.startswith("AI analysis unavailable right now"):
                rate_limited = True
                _skip(report, sym, "RATE_LIMIT", state, row)
                continue
            if raw.startswith("AI analysis"):
                _fail(report, store, sym, req, key, ctx, bucket, run_id, "PROVIDER_ERROR", now, budget, calls, wants_refresh, state)
                continue
            status, result, reason = C.parse_result(raw, req, now, budget["stale_after_hours"], model or config.ANTHROPIC_MODEL)
            if status != "COMPLETE":
                _fail(report, store, sym, req, key, ctx, bucket, run_id, reason or "WITHHELD", now, budget, calls, wants_refresh, state, status="WITHHELD")
                continue
            store.insert_result(cache_key=key, symbol=sym, request_type=C.REQUEST_TYPE, bucket=bucket, context_hash=ctx, run_id=run_id, status="COMPLETE",
                                reason=None, request=req.payload(), result=result, model=model or config.ANTHROPIC_MODEL, prompt_version=C.PROMPT_VERSION,
                                claude_calls=calls, refresh=wants_refresh, created_at=now, stale_after=result["stale_after"])
            report["researched"].append(sym)
            report["symbols"].append({"symbol": sym, "state": COMPLETE, "cache_age_min": 0, "updated": now.isoformat(timespec="seconds"),
                                      "claude_calls": calls, "process_cache": calls == 0})
        report["summary"] = (f"{len(report['shortlisted'])} shortlisted, {len(report['researched'])} researched, "
                             f"{len(report['cache_hits'])} from cache, {len([s for s in report['skipped'] if s['reason'] not in ('NOT_RANKED', 'SHORTLIST_CAP')])} skipped"
                             + (" due to budget" if any(s["reason"] in ("BUDGET", "RATE_LIMIT", "REFRESH_LIMIT") for s in report["skipped"]) else "")
                             + f", {len(report['failed'])} failed")
        store.insert_batch(run_id, report, now)
    return report


def _skip(report: dict, sym: str, reason: str, state: dict, row: Optional[dict]) -> None:
    report["skipped"].append({"symbol": sym, "reason": reason})
    shown = STALE if (row is not None and row["status"] == "COMPLETE") else SKIPPED
    if shown == STALE:
        report["stale_shown"].append(sym)
    report["symbols"].append({"symbol": sym, "state": shown, "reason": reason, "cache_age_min": state["cache_age_min"], "updated": state["updated"]})


def _fail(report, store, sym, req, key, ctx, bucket, run_id, reason, now, budget, calls, wants_refresh, state, status="FAILED") -> None:
    store.insert_result(cache_key=key, symbol=sym, request_type=C.REQUEST_TYPE, bucket=bucket, context_hash=ctx, run_id=run_id, status=status,
                        reason=str(reason)[:120], request=req.payload(), result=None, model=None, prompt_version=C.PROMPT_VERSION, claude_calls=calls,
                        refresh=wants_refresh, created_at=now, stale_after=now.isoformat(timespec="seconds"))
    report["failed"].append({"symbol": sym, "reason": str(reason)[:120]})
    report["symbols"].append({"symbol": sym, "state": FAILED, "reason": str(reason)[:120], "cache_age_min": 0, "updated": now.isoformat(timespec="seconds"),
                              "claude_calls": calls})
