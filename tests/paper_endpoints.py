"""Stage 4.5: the ONLY order-like API paths in the app — the LOCAL, simulated paper portfolio (api/routes/paper.py; no
broker, no real order). Earlier stages' "no order / cancel / execution / paper endpoint" invariants exempt exactly these
paths with exactly these methods; every other path keeps the original rules."""
PAPER_ENDPOINTS = {
    "/api/paper-portfolio": {"get"},
    "/api/paper-portfolio/account": {"post"},
    "/api/paper-portfolio/account/settings": {"post"},
    "/api/paper-orders": {"get", "post"},
    "/api/paper-orders/preview": {"post"},
    "/api/paper-orders/process": {"post"},
    "/api/paper-orders/{order_id}/cancel": {"post"},
    "/api/paper-fills": {"get"},
}
# Stage 4.6A: the Alpaca PAPER account, READ ONLY (api/routes/alpaca_paper.py). The refresh POST performs only GET reads
# at the paper broker; no path here can submit, cancel, replace or close anything.
ALPACA_PAPER_ENDPOINTS = {
    "/api/alpaca-paper/status": {"get"},
    "/api/alpaca-paper/refresh": {"post"},
    "/api/alpaca-paper/view": {"get"},
}
# Stage 4.6B: manual Alpaca PAPER orders (api/routes/alpaca_paper_orders.py). The ONLY broker write is POST /confirm (and the
# explicit POST /{intent_id}/retry of the same order) — one POST /v2/orders to the paper host; no cancel / replace / close.
ALPACA_PAPER_ORDER_ENDPOINTS = {
    "/api/alpaca-paper-orders": {"get"},
    "/api/alpaca-paper-orders/settings": {"get"},
    "/api/alpaca-paper-orders/settings/link-account": {"post"},
    "/api/alpaca-paper-orders/settings/relink-account": {"post"},
    "/api/alpaca-paper-orders/settings/enable": {"post"},
    "/api/alpaca-paper-orders/settings/disable": {"post"},
    "/api/alpaca-paper-orders/preview": {"post"},
    "/api/alpaca-paper-orders/confirm": {"post"},
    "/api/alpaca-paper-orders/{intent_id}/retry": {"post"},
    "/api/alpaca-paper-orders/{intent_id}/abandon": {"post"},
    "/api/alpaca-paper-orders/status": {"post"},
}


def paper_exempt(path: str, ops) -> bool:
    """True for a Stage 4.5 local paper path, a Stage 4.6A read-only Alpaca paper path or a Stage 4.6B manual paper order
    path (after checking its exact methods); False for every other path."""
    for allowed in (PAPER_ENDPOINTS, ALPACA_PAPER_ENDPOINTS, ALPACA_PAPER_ORDER_ENDPOINTS):
        if path in allowed:
            assert set(ops) == allowed[path], (path, sorted(ops))
            return True
    return False
