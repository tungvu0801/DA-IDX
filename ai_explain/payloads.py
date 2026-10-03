"""
ai_explain/payloads.py — the COMPACT deterministic payloads Claude may read, and the local (no-AI) explanations.

Every value is copied from the authoritative Python result and pre-formatted as text ("+5.81%", "-2.61 percentage
points", "3 of 4"), so the model never has to calculate, round or convert anything. Nothing here computes a new metric,
reads market data or touches the database.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

COUNT_NOTE = ("A descriptive count of the saved conditions. The saved rule logic (ALL / ANY), not the count, decides the result, and a count is never a percentage.")
FIT_SCOPE = ("Strategy Fit compares the latest completed daily close with the saved entry rules. It describes current "
             "rule alignment only and says nothing about future prices; exit rules are not evaluated for a stock that "
             "is only being examined.")
EVIDENCE_SCOPE = ("Historical evidence is a simulated backtest (trades with sizing and costs). Forward evidence is a "
                  "journal of reference cycles captured going forward (no orders, no sizing, no costs). They cover "
                  "different periods and are never merged into a single number.")
AI_STATUSES = ("RULES_MET", "RULES_NOT_MET", "INCOMPLETE_DATA")


def _r2(x) -> Optional[str]:
    if x is None:
        return None
    return str(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def pct(x, signed: bool = True) -> Optional[str]:
    if x is None:
        return None
    s = _r2(x)
    if signed and not s.startswith("-") and float(s) != 0:
        s = "+" + s
    return s + "%"


def money(x) -> Optional[str]:
    return None if x is None else f"${float(x):,.2f}"


def day(d: Optional[str]) -> Optional[str]:
    if not d:
        return None
    try:
        x = date.fromisoformat(str(d)[:10])
    except ValueError:
        return str(d)
    return f"{x.strftime('%b')} {x.day}, {x.year}"


def _plural(n, word) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _actual(t: dict) -> Optional[str]:
    """The observed value of one leaf condition, as the UI shows it."""
    if t.get("result") not in ("MET", "NOT_MET"):
        return None
    v, unit = t.get("actual"), t.get("unit")
    if t.get("actual_label") is not None:
        return str(t["actual_label"])
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        if unit == "%":
            return pct(v)
        if unit == "$":
            return money(v)
        if unit == "x":
            return f"{float(v):.2f}x"
        if unit == "0-100":
            return f"{float(v):.1f}"
        return _r2(v) if isinstance(v, float) else str(v)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return None if v is None else str(v)


def _conditions(trace: Optional[List[dict]]) -> List[dict]:
    out = []
    for t in trace or []:
        if "children" in t:
            out.append({"group": f"{t['group']} of {len(t['children'])} (the group is {t['result'].replace('_', ' ')})",
                        "conditions": _conditions(t["children"])})
        else:
            out.append({"saved_condition": t.get("text") or t.get("feature"), "input": t.get("name") or t.get("feature"),
                        "observed_value": _actual(t), "result": str(t.get("result")).replace("_", " "),
                        **({"why_unavailable": str(t.get("reason") or t.get("availability")).replace("_", " ")}
                           if t.get("result") not in ("MET", "NOT_MET") else {})})
    return out


def fit_payload(ctx: dict, s: dict) -> dict:
    """ONE strategy version's Strategy Fit result (Stage 3.4 output) for ONE symbol. No other strategy is included."""
    he, fe = s.get("historical_evidence") or {}, s.get("forward_evidence") or {}
    runs = [r for r in he.get("runs") or [] if r.get("status") == "COMPLETED"]
    latest = runs[0] if runs else None
    hist = {"stored_backtest_runs": he.get("count", 0), "completed_runs": he.get("completed", 0)}
    if latest:
        m = latest.get("metrics") or {}
        hist["most_recent_stored_run"] = {
            "period": f"{day(latest['period']['start'])} to {day(latest['period']['end'])}",
            "closed_trades": m.get("closed_trades"), "total_return": pct(m.get("total_return_pct")),
            "max_drawdown": pct(m.get("max_drawdown_pct")),
            "sample": ((m.get("sample") or {}).get("text"))}
    if s.get("readiness") != "BACKTEST_READY":
        hist["note"] = "Historical backtests are unavailable for these rules (forward test only)."
    j, o = fe.get("journal"), fe.get("symbol")
    fwd = {"journal": "none"} if not j else {
        "journal_status": j.get("status"), "continuity": j.get("continuity"),
        "sessions_captured": j.get("sessions_captured"), "sessions_missed": j.get("sessions_missed"),
        "latest_captured_session": day(j.get("latest_captured_session"))}
    if j and o:
        fwd.update({"latest_stored_decision_for_symbol": o.get("decision"), "shadow_state_for_symbol": o.get("state_after"),
                    "completed_reference_cycles_for_symbol": o.get("completed_reference_cycles")})
    elif j:
        fwd["symbol_note"] = ("No stored observation for this stock in the journal yet." if j.get("universe_includes_symbol")
                              else "This journal's universe does not include this stock.")
    return {
        "explanation_type": "STRATEGY_FIT_EXPLANATION",
        "grounded_in": {"symbol": ctx["symbol"], "decision_session": day(ctx.get("decision_session")),
                        "context_timing": ctx.get("context_timing_label") or s.get("context_timing"),
                        "context_note": ctx.get("context_text")},
        "strategy": {"name": s.get("strategy_name"), "version": s.get("version"), "is_current_version": s.get("is_current"),
                     "readiness": s.get("readiness"), "universe": s.get("universe")},
        "fit": {"status": str(s.get("fit_status")).replace("_", " "), "status_text": s.get("status_text"),
                "entry_rule": s.get("entry_text"), "entry_rule_logic": s.get("logic"),
                "entry_group_result": str(s.get("group_result")).replace("_", " "),
                "result_could_be_decided": bool(s.get("determinate")),
                "conditions_met": f"{s.get('conditions_met')} of {s.get('conditions_total')}",
                "conditions_not_met": s.get("conditions_not_met"), "conditions_unavailable": s.get("conditions_unavailable"),
                "count_note": COUNT_NOTE},
        "conditions": _conditions(s.get("trace")),
        "unavailable_inputs": [{"input": u.get("name") or u.get("feature"), "reason": u.get("reason_text")}
                               for u in s.get("unavailable") or []],
        "warnings": [w.get("text") for w in s.get("warnings") or [] if w.get("text")],
        "evidence": {"historical": hist, "forward": fwd,
                     "note": "Stored evidence only; it does not change the current rule result."},
        "scope": FIT_SCOPE,
    }


def _latest_completed(v: dict) -> Optional[str]:
    """The run the Evidence view picks by default (runs are listed most recent first)."""
    return next((r["run_id"] for r in (v.get("selection") or {}).get("runs") or [] if r.get("status") == "COMPLETED"), None)


def _hist(h: dict, latest: Optional[str] = None) -> dict:
    if h.get("status") != "AVAILABLE":
        return {"status": str(h.get("status")).replace("_", " "), "message": h.get("message"), "reason": h.get("reason")}
    m, a = h["metrics"], h["assumptions"]
    sym = h.get("symbol_breakdown") or {}
    return {
        "run": f"#{h['run']['run_id'][:6]}", "selection": ("most recent stored run" if h.get("selection") == "MOST_RECENT_COMPLETED"
                      or h["run"]["run_id"] == latest else "an earlier stored run (selected)"),   # same words however it was chosen
        "period": f"{day(a['period']['start'])} to {day(a['period']['end'])}", "sessions": m.get("sessions"),
        "closed_trades": m.get("closed_trades"), "sample": (h.get("sample") or {}).get("text"),
        "average_trade_return": pct(m.get("average_trade_return_pct")), "median_trade_return": pct(m.get("median_trade_return_pct")),
        "win_rate": f"{pct(m.get('win_rate_pct'), signed=False)} ({m.get('winning_trades')} of {m.get('closed_trades')} closed trades)"
        if m.get("win_rate_pct") is not None else None,
        "average_winner": pct(m.get("average_winner_pct")), "average_loser": pct(m.get("average_loser_pct")),
        "total_return": pct(m.get("total_return_pct")), "max_drawdown": pct(m.get("max_drawdown_pct")),
        "average_holding": f"{_r2(m.get('average_holding_days'))} sessions" if m.get("average_holding_days") is not None else None,
        "average_mfe": pct(m.get("average_mfe_pct")), "average_mae": pct(m.get("average_mae_pct")),
        "open_positions_at_end": m.get("open_positions_at_end"),
        "costs": f"slippage {a.get('slippage_bps_per_side')} bps per side, commission {money(a.get('commission_per_order'))} per order",
        "warnings": [w.get("text") for w in h.get("warnings") or []][:8],
        "by_symbol": [{"symbol": s, "closed_trades": v.get("closed_trades"), "average_return": pct(v.get("average_return_pct"))}
                      for s, v in sorted(sym.items())],
    }


def _fwd(f: dict) -> dict:
    if f.get("status") != "AVAILABLE":
        return {"status": str(f.get("status")).replace("_", " "), "message": f.get("message")}
    mt, xm, tb = f.get("reference_cycle_metrics"), f.get("excursion_metrics"), f["timing_breakdown"]
    out = {"journal_status": f["journal_status"], "continuity": f["continuity"],
           "captured_sessions": f["captured_sessions"], "missed_sessions": f["missed_sessions"],
           "completed_reference_cycles": f["completed_cycles"], "open_reference_cycles": f["open_cycles"],
           "blocked_reference_cycles": f["blocked_cycles"], "sample": f["sample"]["text"],
           "context_timing_sessions": {"strict_forward": tb["strict_forward"], "post_close_context": tb["post_close_forward_context"],
                                       "captured_after_next_open": tb["captured_after_next_open"]},
           "warnings": [w.get("text") for w in f.get("warnings") or []][:6]}
    if mt:
        out.update({"positive_completed_cycles": f"{mt['positive_cycles']} of {mt['completed_cycles']}",
                    "average_reference_move": pct(mt["average_reference_move_pct"]),
                    "median_reference_move": pct(mt["median_reference_move_pct"]),
                    "average_holding": f"{_r2(mt['average_holding_sessions'])} sessions" if mt.get("average_holding_sessions") is not None else None})
    if xm:
        out["mfe_mae"] = {"tracked_sample": f"{xm['tracked_completed_cycles']} tracked of {xm['completed_cycles']} completed cycles",
                          "average_mfe": pct(xm.get("average_mfe_pct")), "average_mae": pct(xm.get("average_mae_pct")),
                          "untracked_note": xm.get("legacy_note"),
                          "rule": "Averages use tracked completed cycles only; untracked cycles are not counted."}
    else:
        out["mfe_mae"] = (f.get("mfe_mae") or {}).get("text") or "Not tracked"
    return out


def evidence_payload(v: dict) -> dict:
    """ONE exact strategy version's selected Evidence view (Stage 3.5 / 3.6 output). No other version is included."""
    idn = v["identity"]
    rows = []
    for r in v["comparison"]["compatible_metrics"]:
        h = r.get("historical_text") or (pct(r["historical_display"]) if r.get("unit") == "%" and r.get("historical_display") is not None
                                          else f"{r['historical_display']} sessions" if r.get("unit") == "sessions" and r.get("historical_display") is not None
                                          else r.get("historical_display"))
        fv = r.get("forward_text") or (pct(r["forward_display"]) if r.get("unit") == "%" and r.get("forward_display") is not None
                                        else f"{r['forward_display']} sessions" if r.get("unit") == "sessions" and r.get("forward_display") is not None
                                        else r.get("forward_display"))
        rows.append({"measure": r["label"], "historical": h, "forward": fv,
                     "difference_forward_minus_historical": (r.get("difference_text") or "").replace("−", "-") or None,
                     "what_each_side_measures": r.get("semantic_note")})
    return {
        "explanation_type": "EVIDENCE_EXPLANATION",
        "strategy": {"name": idn["strategy_name"], "version": idn["version_number"], "readiness": idn["readiness_label"]},
        "historical": _hist(v["historical"], _latest_completed(v)), "forward": _fwd(v["forward"]),
        "comparison": {"compatible_measures": rows,
                       "summary": (v["comparison"].get("summary_text") or "").replace("−", "-") or None,
                       "not_compared": [f"{x['label']}: {x['why']}" for x in v["comparison"]["not_compared"]],
                       "rule": "Differences are forward minus historical, already calculated; they are not adjusted for sample size."},
        "limitations": [n["text"] for n in v.get("quality_notes") or []][:10],
        "scope": EVIDENCE_SCOPE,
    }


# ================================================================================================================
# local explanations (no AI call) for states where the deterministic text already says everything
# ================================================================================================================

def local_fit(ctx: dict, s: dict) -> Optional[dict]:
    st = s.get("fit_status")
    if st in AI_STATUSES:
        return None
    sym, name, n = ctx["symbol"], s.get("strategy_name"), s.get("version")
    if st == "OUTSIDE_UNIVERSE":
        uni = ", ".join((s.get("universe") or [])[:12])
        summary = f"{sym} is not in {name} v{n}'s saved universe, so its entry rules were not evaluated for {sym}."
        true_now = [f"The saved universe is: {uni}." if uni else "The saved universe does not include this stock."]
    else:
        summary = s.get("status_text") or st.replace("_", " ").title()
        true_now = ["This version is not evaluated, and nothing else is substituted for it."]
        errs = [e.get("message") for e in (s.get("eligibility") or {}).get("errors") or [] if e.get("message")]
        true_now += errs[:2]
    return {"summary": summary, "what_is_true_now": true_now, "what_is_not_met": [],
            "evidence_context": [], "limitations": [FIT_SCOPE]}


def needs_ai_evidence(v: dict) -> bool:
    h, f = v["historical"], v["forward"]
    return h.get("status") == "AVAILABLE" and f.get("status") == "AVAILABLE" and bool(f.get("completed_cycles"))


def local_evidence(v: dict) -> Optional[dict]:
    if needs_ai_evidence(v):
        return None
    h, f = v["historical"], v["forward"]
    if h.get("status") == "AVAILABLE":
        m = h["metrics"]
        hist = [f"{_plural(m['closed_trades'], 'closed trade')} in the selected backtest ({day(h['assumptions']['period']['start'])} "
                f"to {day(h['assumptions']['period']['end'])}); average trade return {pct(m.get('average_trade_return_pct'))}."]
    else:
        hist = [h.get("message") or "No historical evidence is available."] + ([h["reason"]] if h.get("reason") else [])
    if f.get("status") == "AVAILABLE":
        fwd = [f"Journal {f['journal_status'].replace('_', ' ').lower()}, {f['continuity'].replace('_', ' ').lower()}: "
               f"{_plural(f['captured_sessions'], 'captured session')}, {_plural(f['missed_sessions'], 'missed session')}, "
               f"{_plural(f['completed_cycles'], 'completed reference cycle')}."]
        if not f["completed_cycles"]:
            fwd.append("No completed forward cycles yet, so there are no forward averages or rates.")
    else:
        fwd = [f.get("message") or "No forward evidence is available."]
    why = ("There is no forward journal yet." if f.get("status") == "NO_JOURNAL" else
           "No forward cycle has completed yet." if f.get("status") == "AVAILABLE" else
           "The historical evidence is unavailable." if h.get("status") != "AVAILABLE" else "One evidence source is unavailable.")
    return {"summary": f"Only part of the evidence exists for {v['identity']['strategy_name']} v{v['identity']['version_number']}: "
                       f"{why} Nothing can be compared side by side yet.",
            "historical": hist, "forward": fwd, "differences": ["No side-by-side comparison is possible yet."],
            "limitations": [EVIDENCE_SCOPE]}
