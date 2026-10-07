# Alpaca PAPER one-POST validation — FINAL (successful)

Repository-safe summary of the controlled live validation of the frozen Stage 4.6B manual order path, run from the Stage 4.7 MVP
branch. Source of record: session scratchpad `s47/live_validation/ALPACA_PAPER_ONE_POST_VALIDATION.md` (attempts 1–8). This file
contains no credential, no full account number and no .env content.

## Result

| Item | Value |
|---|---|
| Date / time | 2026-10-07, 17:37–17:42 UTC (13:37–13:42 ET, regular session open, ~2 h 20 m before the close) |
| Branch / SHA | stage-47 @ 59fb134 (main 6e65e5a) — working tree clean, source code unchanged during the run |
| Paper account | ••••LTN9 (masked), ACTIVE, USD, no blocked / suspended flags; fingerprint matched the linked record |
| no_shorting | true (rule V15 ok) |
| Market hours | clock is_open true; next close 16:00 ET, 8 506 s away at pre-check (rules V16 and V17 ok; confirm window until 15:55 ET) |
| Reference price (V10) | F previous close 12.2700 (session 2026-10-06) from the production bar layer; cash guard 12.88 vs cash 100000.00 |
| Enable | existing Stage 4.6B Enable path, HTTP 200, 2 paper GETs, 0 order POSTs |
| Preview | existing Stage 4.6B Preview path, HTTP 200, intent #2 PREVIEWED, rules V1–V8, V10, V11, V12a, V12b, V15, V16, V17 all ok, confirmable |
| Test order | F · BUY · 1 share · MARKET · DAY |
| client_order_id | `sa46b-be1324c851d642d7b7b2e196acf5ecc4` |
| Confirm | exactly once; outcome class O1 (broker accepted, HTTP 200); submit_attempts 1; confirmed 17:38:51 UTC |
| **Alpaca PAPER order POSTs** | **exactly 1** (POST /v2/orders on paper-api.alpaca.markets, through `paper/alpaca_order_writer.py` only) |
| Broker order id | `cbd4f557-32c2-4518-8494-75bc68fbfff3` |
| Fill | status filled; submitted 17:38:52.254 UTC; filled 17:38:53.245 UTC |
| Filled quantity | 1 |
| Average fill price | 12.10 |
| Reconciliation | existing Stage 4.6B Check Status: exact lookup by Alpaca order id → found, consistent (client_order_id, symbol, side, qty, account fingerprint) → state FILLED, terminal |
| Duplicate check | frozen Stage 4.6A read-only refresh: exactly 1 order for this client_order_id; 1 F order; 1 `sa46b-` order on the page; F position qty 1 @ 12.10; cash 99987.90 |
| UI reflection | Stage 4.6B intent list shows #2 FILLED with order reference cbd4f557; Stage 4.6A view shows the fill and the position |
| Cancel | not cancelled (filled; nothing to cancel) |
| Manual orders afterwards | returned to OFF through the existing disable path (0 broker requests); account remains linked |

## Network accounting (attempt 8)

| Counter | Count |
|---|---|
| Alpaca PAPER order POSTs | 1 |
| Alpaca PAPER GETs | 17 (pre-check 3, enable 2, preview 4, confirm 3, status 2, 4.6A refresh 3) |
| Alpaca market-data requests | 2 |
| Alpaca LIVE trading calls | 0 |
| Robinhood calls / writes | 0 / 0 |

Cumulative over attempts 1–8: PAPER order POSTs 1 (attempt 8 only); attempts 1–7 were hard stops before any POST (no_shorting false
×3, market closed ×2, frozen-module UTC bug, market-data 401, paper-key rotation).

## Tests after the validation (tree unchanged at 59fb134)

- Stage 4.6B 83 · Stage 4.6A 33 · Stage 4.7 Phase 5 handoff 11 · UTC regression 3 · protected-file pin 1 → 131 passed
- Full suite: 1068 passed
- Protected files: 21 Stage 4.6A/4.6B files byte-identical to the 4.6B freeze; `paper/alpaca_orders.py` at the approved pin 391cd2f2 (one-token UTC fix, commit 59fb134)

## Safety statements

Exactly one real Alpaca PAPER order POST was attempted. No live Alpaca trading endpoint was used. No Robinhood call or write occurred.
No automatic trading was introduced. Manual orders were returned to OFF. Nothing was merged to main during the validation.
