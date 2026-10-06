# Stage 4.6B — Manual Alpaca PAPER orders

Stock Agent can send a **manual MARKET / DAY order for whole shares to your Alpaca PAPER account** — only after a
preview and an explicit click on **Confirm Paper Order**. The authoritative specification is DESIGN_46B_FINAL.
Everything is paper: there is no live trading, no automation, no AI-originated order, no Robinhood write, and no cancel /
replace / close / account-configuration change anywhere.

| File | Role |
|---|---|
| `paper/alpaca_order_writer.py` | the only broker write: one `POST /v2/orders` to `paper-api.alpaca.markets` per attempt |
| `paper/alpaca_order_reads.py` | exact paper GETs (account, configurations, asset, clock, position, order by id / client order id) |
| `paper/alpaca_order_rules.py` | pure rules V1–V17, outcome classes O1–O9, status map, texts |
| `paper/alpaca_order_store.py` · `database/alpaca_order_migrations.py` | the three `alpaca_paper_order_*` tables |
| `paper/alpaca_orders.py` | the service (settings, preview, confirm, retry, abandon, status) |
| `api/routes/alpaca_paper_orders.py` · `frontend/alpaca_orders.js` | API and the "Alpaca Paper — Manual Orders" pane |

## Credentials and account

- Only `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY` from the local `.env` (never the market-data pair, no fallback).
  Values are never stored, logged, returned or shown; the account appears masked (`••••1234`) and as a sha256 fingerprint.
- Manual orders are **OFF** by default. **Link** the paper account explicitly, then **Turn on**. A different account needs an
  explicit **Relink** (only when no order has an unresolved outcome); relinking turns manual orders off again.
- **Long only:** Alpaca's `no_shorting` must be ON (set it in your Alpaca paper dashboard). Stock Agent only reads this
  setting (`GET /v2/account/configurations`) — it never changes it. It is checked at enable, preview, confirm and retry.

## Preview → Confirm

A preview makes no order. It reads the account, the no_shorting setting, the asset, the market clock and (for a sell) the
position — at most 5 paper GETs — plus the reference price, and stores an immutable preview with a new client order id
(`sa46b-` + a random uuid). **Confirm Paper Order** (click only; Enter never confirms) is available for 120 seconds, only
during regular market hours and not within 5 minutes of the close. Confirm re-checks every gate on fresh reads, commits the
order as SUBMISSION_PENDING, and then sends exactly the stored payload once.

### Reference price and the 5% cash guard

- The reference price is the **previous completed daily close** from the existing Stage 3.2/3.4 bar layer (local bar
  cache → in-memory cache → at most one market-data request with the market-data key pair). It is informational — **not a
  quote, not the fill price**. A market order fills at the prevailing market price.
- The **5% cash guard** is Stock Agent's own conservative check for BUY orders: `shares × previous close × 1.05` must fit in
  your Alpaca paper cash (cash, not margin buying power). It is NOT a guarantee of the execution price and NOT a guarantee
  that Alpaca will accept the order. **Alpaca decides buying power**: its decision is authoritative, and an Alpaca rejection
  is recorded as such.
- Limits: 10,000 shares per order · $100,000 estimated notional per order · 20 confirmed orders per New York trading day.

## Idempotency and reconciliation

- One order keeps one client order id forever, including retries. The POST is never retried automatically.
- Only a failure before any byte left the machine counts as "not sent". Any other doubtful outcome (timeouts, HTTP 429 /
  5xx / 401 / 400, unreadable answers, a duplicate-id 422) is RECONCILIATION REQUIRED and is resolved only by an exact
  lookup of the client order id. Only a definitive, non-duplicate Alpaca validation rejection becomes "Rejected by Alpaca".
- **Retry** (explicit) re-uses the same client order id and always looks the order up first; after an uncertain outcome it
  also needs at least two lookups finding nothing over at least 30 seconds. **Abandon** likewise never hides an existing order.
- Orders are linked by exact identifiers only (client order id / Alpaca order id) — never by symbol, quantity, time or
  price. The Stage 4.6A read-only pane and the Stage 4.5 simulator are unchanged and never written.
- Every action that reads orders or can change one first checks that the credentials point to the **linked** account
  (Check order status and Abandon: one `GET /v2/account`). If `.env` now holds a different paper account, the action stops
  with ACCOUNT_NOT_LINKED before any order lookup or state change — an order on the linked account is never looked up on,
  or abandoned because of, another account.

## Network budget per action

| Action | Paper GET | Market data | POST |
|---|---|---|---|
| Open the pane, list, settings view, Turn off | 0 | 0 | 0 |
| Link / Relink / Turn on | 2 | 0 | 0 |
| Preview | ≤ 5 | ≤ 1 | 0 |
| Confirm | ≤ 4 + ≤ 1 lookup | 0 | exactly 1 |
| Retry | 1 lookup + ≤ 4 + ≤ 1 lookup | 0 | ≤ 1 |
| Abandon | 1 account + 1 lookup | 0 | 0 |
| Check order status | 1 account + ≤ 20 lookups | 0 | 0 |
