# Stage 4.6A — Alpaca Paper, read-only connection and reconciliation

Stock Agent can show **one Alpaca PAPER trading account** next to the Stage 4.5 local simulator and describe how the
two differ. The connection is **read only**. It is observational: nothing is submitted, changed, cancelled, closed, synced,
imported or linked, on either side.

| File | Role |
|---|---|
| `paper/alpaca_readonly.py` | The one production module that imports `TradingClient`. It exposes four named reads and nothing else. |
| `paper/alpaca_view.py` | The explicit refresh, the connection status, the in-memory snapshot and the payload. |
| `paper/reconcile.py` | Pure comparison functions (local vs Alpaca). It describes differences and never fixes them. |
| `api/routes/alpaca_paper.py` | `GET /status`, `POST /refresh`, `GET /view` under `/api/alpaca-paper`. |
| `frontend/alpaca_paper.js` | The "Alpaca Paper — Read Only" view of the Paper Portfolio workspace. |

## Credentials

The adapter reads exactly two environment variables, from the local `.env` file (git-ignored), which you edit yourself:

```
ALPACA_PAPER_API_KEY=<your paper key id>
ALPACA_PAPER_SECRET_KEY=<your paper secret key>
```

- There is **no fallback**. The market-data pair (`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`) is never read for this, and a
  failed paper login is never retried with other keys or another host.
- If either variable is missing, the state is **NOT CONFIGURED**, no network call is made, and the local simulator keeps
  working.
- Values are never printed, logged, stored in the database, returned by the API or sent to the browser. The API
  returns only the variable *names* and "configured / not configured".
- Restart the app after editing `.env`.

## Paper-only enforcement

- The client is constructed in exactly one place, always as `TradingClient(key, secret, paper=True)`. No parameter,
  browser field, setting or environment variable can change `paper=True`. There is no live mode, no base-URL
  environment variable, and no `url_override`.
- Safety never depends on what a key looks like (no key-prefix checks). It comes from the paper-only variable names,
  the hard-wired `paper=True`, the guarded transport below, and the tests.
- **Guarded transport.** Every HTTP request of a reader passes through one requests adapter. It works as follows:
  - It allows only `GET`, only `https://paper-api.alpaca.markets`, and only these paths:
    - `/v2/account`
    - `/v2/positions`
    - `/v2/orders`
    - `/v2/account/activities`
  - Each path is requested **at most once per refresh**.
  - Every request gets a finite timeout of `(5 s connect, 10 s read)`.
  - Anything else — a POST, DELETE or PATCH, the live host, another path, or a second request — is refused **before it
    is sent**. That includes a write method if one were ever called by mistake.
  - The SDK's automatic retries (3× on 429/504 in alpaca-py 0.44) are switched off. Redirects are never followed.
  - If the SDK's internal layout ever changes, the reader refuses to start (it fails closed).

## The read-only method allow-list

| Reader method | Request |
|---|---|
| `get_account()` | `GET /v2/account` |
| `get_positions()` | `GET /v2/positions` |
| `get_recent_orders(limit)` | `GET /v2/orders?status=all&limit=100` (one page, newest first) |
| `get_recent_fills(limit)` | `GET /v2/account/activities?activity_types=FILL&direction=desc&page_size=100` (one page) |

The installed alpaca-py `TradingClient` has no activities method. The fill read is therefore one exact helper:
`GET /v2/account/activities` on the constant paper host (`paper-api.alpaca.markets`), sent through the same guarded
transport. There is no generic `call` / `request` / raw-client accessor, and the raw client is never returned.

## Refresh behavior

- Broker data is read **only** when you press **Refresh Alpaca Paper**. There is no read at startup, none when a view
  opens, and no timer, polling, scheduler or trading stream.
- A refresh makes **at most 4** requests, in this order: account, positions, recent orders, recent FILL activities.
  - Each gets one attempt.
  - An authentication failure (HTTP 401/403) or a connection failure stops the remaining reads. They would fail the
    same way, and skipping them keeps within the budget.
- History is bounded: at most 100 recent orders and 100 recent fills, one page each, never a history crawl.
- Single flight: a refresh requested while one is running joins it and reads nothing more. The button is also disabled
  while a refresh is in flight.
- The last refresh is kept **in memory only**. "Broker data refreshed at" shows its time, and it is lost on restart.
  `GET /view` re-compares that snapshot with the *current* local simulator (0 broker calls).

## Connection states

| State | Meaning |
|---|---|
| NOT CONFIGURED | One or both variables are missing. |
| READY | Configured, but not refreshed yet in this session. |
| REFRESHING | A refresh is in flight. |
| CONNECTED | All four reads succeeded. |
| AUTH ERROR | "Alpaca Paper authentication failed." |
| PAPER API UNAVAILABLE | Connection failure, timeouts or server errors, and nothing was read. |
| PARTIAL DATA | Some reads failed. Every successful section is still shown, and the failed ones are listed. |
| ERROR | Anything else (for example, an unreadable answer, or the reader could not be built). |

## Reconciliation semantics (`paper/reconcile.py`)

**Positions.** The comparison takes the union of symbols from the local simulator's open positions and Alpaca's open
positions, alphabetical.

- Each symbol gets a status by share quantity only:
  - `MATCH` — both hold the same quantity.
  - `DIFFERENT` — both hold the symbol, in different quantities.
  - `LOCAL_ONLY` — only the local simulator holds it.
  - `ALPACA_ONLY` — only Alpaca holds it.
- **Share delta = Alpaca paper shares − local simulated shares** (negative: Alpaca holds fewer). A short Alpaca
  position is negative.
- Average costs are shown side by side and never compared: each comes from its own account's fills, and the local one
  includes simulated slippage and commission.
- If Alpaca's positions could not be read, nothing is categorised. An unread account is never reported as LOCAL_ONLY.

**Values (price timing).** Local marks are the latest **completed daily close** (Stage 4.5). Alpaca's current price,
market value and unrealized P&L are as of the broker refresh. Because the timestamps differ, these are
**`NOT_DIRECTLY_COMPARABLE`**. There is no P&L comparison and no verdict.

**Balances.** The two accounts are **INDEPENDENT**: they can have different starting balances and transaction
histories. Local simulated cash and equity are shown separately from Alpaca paper cash and equity. The optional
difference (Alpaca − local) is descriptive only. A difference is not an error.

**Orders and fills.** These are always **`NOT_LINKED`**. A local simulator order carries no Alpaca order id, so
Stage 4.6A never pairs orders or fills by symbol, quantity, time or price. A coincidence such as "local AMD BUY 10" and
"Alpaca AMD BUY 10" does not establish the same user intent. Exact linkage via a broker order id or client order id is
left to a later stage.

**Summary.** The summary is plain counts only:
- local positions and Alpaca positions;
- quantity matches and quantity differences;
- local-only and Alpaca-only;
- linked orders (0) and linked fills (0).

There is no score, confidence or recommendation.

## What never happens

- Alpaca data never overwrites local cash, orders, fills, lots or P&L. Local data is never pushed to Alpaca.
- No database table, no migration, and no broker data persisted. A refresh makes 0 writes to the Stage 4.5 tables.
- There are no Claude calls and no Robinhood access. The existing market-data credentials and behavior are unchanged.

## Live validation budget

Automated tests and the browser harness use fakes only. The harness blocks `paper-api.alpaca.markets`, and any
accidental call fails the run. A real read-only validation needs explicit approval. It makes **at most 4** requests:
account, positions, one page of recent orders, and one page of recent FILL activities. It has no retries, no
pagination, and never a write (no test order of any size). It reports only:
- success or failure;
- the masked account (`••••1234`);
- counts;
- states;
- per-read latencies;
- the reconciliation summary.
