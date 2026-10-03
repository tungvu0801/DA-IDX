# Stage 4.5 — Local deterministic paper portfolio

Strategy Lab → **Paper Portfolio** (always labelled **PAPER · SIMULATED · NO REAL ORDERS**). A local simulation to
practise orders without any broker: virtual USD cash, whole shares, long only, market orders filled at the next valid
session open from daily bars. Not Alpaca paper trading, not Robinhood, not live execution; no margin, shorting, options,
AI trade generation, optimisation or automatic execution. Never combined with the real (read-only brokerage) portfolio.

## Flow

```
explicit user order (symbol, side, whole shares)  -> server validation + server-defined sessions and estimate
  -> PENDING paper order -> "Process pending paper orders" (explicit) -> fill at the next valid session open
  -> immutable fill + FIFO lot / lot closures (one transaction) -> cash, positions, P&L, equity (paper/accounting.py)
```

## Account

One local account (`paper_accounts`): name, USD, **starting cash entered by the user — no default** (1,000 to
100,000,000), slippage in bps per side (0–500) and a fixed commission per order (0–1,000). Slippage and commission are
pre-filled with the Stage 3.2 backtester's defaults (0 / $0, labelled "an optimistic, cost-free assumption"). Starting
cash, currency, slippage and commission are frozen by a database trigger once the account has a fill; only the name stays
editable. No reset and no deletion in Stage 4.5.

## Orders and timing (the Stage 3.2 contract)

MARKET BUY / SELL, whole shares 1–1,000,000, origin MANUAL, optional `strategy_version_id` (validated; provenance only —
the order never implies that a rule result means "trade").

* `decision_session` = the latest **completed** session at submission (`backtest.bars.last_complete_session_date` + the
  SPY market calendar, `fit.current.resolve_session` — the rule Strategy Fit and the forward journal use).
* `earliest_fill_session` = the first weekday whose **09:30 New York open is after the submission time**. An order
  created during a session can therefore never fill at that session's already-known open (no look-ahead). Holidays are
  not known in advance: the fill session is the first SPY session on or after that date.
* The browser never sends a price, session, date or time; the server derives them. No backdated or future orders.
* BUY is accepted when the estimate (latest completed close with slippage, plus commission) fits the cash not already
  reserved by pending buys; SELL when the held shares minus pending sells cover it. The fill re-checks both.
* `PENDING -> FILLED | CANCELLED | REJECTED` exactly once; only PENDING can be cancelled (FILLED -> 409 ALREADY_FILLED,
  CANCELLED -> no-op). Resolved orders and every fill / lot / closure are immutable (triggers); nothing is deleted.

## Fill model

"Process pending paper orders" (explicit; no scheduler, no automatic execution) fills an order only when its fill session
has **completed** under the same rule. Base price = that session's daily **open** for the stock, from the existing
read-only bar path (`fit.current.load_bars`: the stored Stage 3.2 cache or an in-memory fetch through the existing
`data.market_data.fetch_daily_bars` — nothing is written to the backtest tables), adjustment `all`. Each fill records
base price, slippage, effective price, notional, commission, cash change, cash after, and the price source (cache /
memory / fetched, dataset id + content hash when cached, feed, adjustment).

* effective BUY = open × (1 + bps/10000); effective SELL = open × (1 − bps/10000) — the Stage 3.2 cost model.
* Cash only: a BUY whose actual cost exceeds cash is **REJECTED** (`INSUFFICIENT_CASH_AT_FILL`) — no fill, no lot, cash
  never negative (also a CHECK on `cash_after`). Long only: a SELL beyond held shares is rejected (`INSUFFICIENT_SHARES`).
* No open for the stock at the fill session: the order **stays PENDING** (`NEXT_OPEN_UNAVAILABLE`) — no invented price,
  no quote. Session not completed yet: `NOT_YET_AVAILABLE`. Calendar not confirmed: `DATA_WAIT_CALENDAR`.
* At each open, SELLs before BUYs (the Stage 3.2 order), then submission time.
* At most once: ONE transaction (`BEGIN IMMEDIATE`) re-reads the order as PENDING and writes the fill, its lot or
  closures and FILLED; `UNIQUE(order_id)` on fills plus a no-second-fill trigger. Restarts, repeated clicks and concurrent
  servers never duplicate a fill (tested with threads). Pending orders survive restarts and are never filled early.

## Accounting (paper/accounting.py — pure Decimal)

Prices 4 dp, money (notional, commission, cash, cost basis, P&L) 2 dp, `ROUND_HALF_UP`; slippage 2 dp. The Stage 3.2
engine uses floats; the paper ledger uses Decimal so cash and P&L are exact to the cent.

* BUY: cash −= notional + commission; lot total cost = notional + commission (average cost includes costs).
* SELL: cash += notional − commission (net proceeds). **FIFO**: the oldest lots (entry session, then fill order) close
  first. A partial close takes cost basis pro rata in cents; the closure that empties a lot takes exactly what remains,
  so a lot's closures always add up to its total cost. Net proceeds are allocated across closures the same way.
* Realized P&L = net proceeds − FIFO cost basis. Unrealized P&L = shares × mark − remaining cost basis.
* Mark = the latest **completed daily close** ("Marked at the Oct 23 close") — never an intraday quote. A position
  without a close for that session is unmarked and equity is shown as incomplete, never guessed.
* Equity = cash + marked market value (not real net worth).

## API

`GET /api/paper-portfolio`, `POST /api/paper-portfolio/account`, `POST /api/paper-portfolio/account/settings`,
`GET|POST /api/paper-orders`, `POST /api/paper-orders/preview` (validation + estimate, nothing stored),
`POST /api/paper-orders/{id}/cancel`, `POST /api/paper-orders/process`, `GET /api/paper-fills`. Strict bodies
(`extra="forbid"`, whole shares as strict integers); no fill price, session, date or time is accepted; no delete.

## Tables (additive; `PRAGMA user_version` untouched)

`paper_accounts`, `paper_orders`, `paper_fills`, `paper_lots`, `paper_lot_closures` (`paper_lot_closures` makes FIFO
consumption append-only instead of mutating lots) — see `database/paper_migrations.py`.

## Known limitations

Local simulation only; long only; whole shares; market orders at the next open only; no intraday execution; no margin,
shorting or options; no dividends and no corporate-action simulation (a split between a fill and a later mark is not
adjusted — marks and fills use adjusted daily bars as captured); latest-completed-close marks; no automatic strategy
execution (a RULES MET result, a forward-journal decision or a saved-scan alert never creates an order); no broker
reconciliation; one account; Strategy Fit / Forward Journal "create paper order" buttons deferred (Stage 3.4's frozen
language tests forbid order wording in Strategy Fit).
