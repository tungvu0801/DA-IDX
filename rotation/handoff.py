"""
rotation/handoff.py — Stage 4.7 Phase 5 PURE handoff eligibility (DESIGN_47_PORTFOLIO_ROTATION §17, CLOSED source rule).

A rebalance item may be handed to the EXISTING, frozen Stage 4.6B "Alpaca Paper — Manual Orders" preview form only when
the run's portfolio source is ALPACA_PAPER_VIEW. ROBINHOOD_READ_ONLY and LOCAL_SIMULATOR runs are DISPLAY ONLY: no draft,
no prefill, no write path — Robinhood and Alpaca positions are different accounts and a Robinhood quantity is never
transferred to an Alpaca order.

The draft is derived from the stored, deterministic rebalance item alone (symbol, action → side, est_qty_diff → whole
shares) and shaped to the frozen 4.6B preview contract (symbol SYMBOL_RE, side BUY | SELL, quantity 1..MAX_QTY whole
shares). Everything else (validation V1–V17, client_order_id, the single POST, account checks, reconciliation) stays in
Stage 4.6B: the browser only fills its form; the user still clicks Preview and then Confirm there. Nothing here
constructs, previews or sends an order.
"""
from __future__ import annotations

from typing import Mapping, Optional

from paper import alpaca_order_rules as RU                       # MAX_QTY / SYMBOL_RE only (pure constants of the frozen module)
from rotation import rules as R
from rotation import snapshots as SN

MODE_PREFILL, MODE_DISPLAY = "ALPACA_PAPER_PREFILL", "DISPLAY_ONLY"
SOURCE_LABELS = {SN.ALPACA_PAPER_VIEW: "ALPACA PAPER", SN.ROBINHOOD_READ_ONLY: "ROBINHOOD — READ ONLY", SN.LOCAL_SIMULATOR: "LOCAL SIMULATOR"}
EXPLANATIONS = {
    SN.ALPACA_PAPER_VIEW: ("Prepare Paper Order fills the existing Alpaca Paper — Manual Orders form with this item's symbol, side "
                           "and whole-share quantity. You review it there, then Preview and Confirm; nothing is sent by this page."),
    SN.ROBINHOOD_READ_ONLY: "Robinhood portfolio is read-only. No quantity is transferred to Alpaca.",
    SN.LOCAL_SIMULATOR: "Local simulator results cannot create a broker draft.",
}
CONTROL_LABELS = {SN.ALPACA_PAPER_VIEW: "Prepare Paper Order", SN.ROBINHOOD_READ_ONLY: "Paper handoff unavailable", SN.LOCAL_SIMULATOR: "Display only"}
BUY_ACTIONS, SELL_ACTIONS = (R.ADD, R.INCREASE), (R.DECREASE, R.EXIT)
REASON_TEXT = {
    "INVALID_SOURCE": "unknown portfolio source", "SOURCE_DISPLAY_ONLY": "display only for this source",
    "RUN_NOT_VALID": "run is not VALID", "NO_ACTION": "no action", "NOT_ALLOWED": "not allowed by the run",
    "NO_WHOLE_SHARES": "below one whole share", "ABOVE_46B_MAX_QTY": f"above the {RU.MAX_QTY:,}-share paper limit",
    "INVALID_SYMBOL": "invalid symbol", "SIDE_MISMATCH": "inconsistent side", "MISSING_DATA": "missing run or item data",
}


def mode_for_source(source: Optional[str]) -> str:
    return MODE_PREFILL if source == SN.ALPACA_PAPER_VIEW else MODE_DISPLAY


def side_for_action(action: Optional[str]) -> Optional[str]:
    """ADD / INCREASE → BUY, DECREASE / EXIT → SELL, HOLD / NONE / anything else → no handoff."""
    return "BUY" if action in BUY_ACTIONS else "SELL" if action in SELL_ACTIONS else None


def evaluate(run: Optional[Mapping], item: Optional[Mapping]) -> dict:
    """Handoff metadata for one rebalance item. Fails closed: any missing or inconsistent field → not eligible."""
    source = (run or {}).get("portfolio_source")
    mode = mode_for_source(source)
    out = {"mode": mode, "source_label": SOURCE_LABELS.get(source), "control_label": CONTROL_LABELS.get(source),
           "explanation": EXPLANATIONS.get(source), "eligible": False, "reason": None, "reason_text": None, "draft": None}

    def refuse(code: str) -> dict:
        return {**out, "reason": code, "reason_text": REASON_TEXT[code]}

    if not run or not item:
        return refuse("MISSING_DATA")
    if source not in SN.SOURCES:
        return refuse("INVALID_SOURCE")
    if mode != MODE_PREFILL:
        return refuse("SOURCE_DISPLAY_ONLY")
    if run.get("status") != "VALID":
        return refuse("RUN_NOT_VALID")
    side = side_for_action(item.get("action"))
    if side is None:
        return refuse("NO_ACTION")
    if not item.get("handoff_allowed"):
        return refuse("NOT_ALLOWED")
    qty = item.get("est_qty_diff")
    if isinstance(qty, bool) or not isinstance(qty, int):
        return refuse("MISSING_DATA")
    if qty < 1:
        return refuse("NO_WHOLE_SHARES")
    if qty > RU.MAX_QTY:
        return refuse("ABOVE_46B_MAX_QTY")
    symbol = str(item.get("symbol") or "")
    if not RU.SYMBOL_RE.match(symbol):
        return refuse("INVALID_SYMBOL")
    if item.get("side_hint") not in (None, side):
        return refuse("SIDE_MISMATCH")
    return {**out, "eligible": True, "draft": {"symbol": symbol, "side": side, "quantity": qty}}
