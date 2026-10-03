"""
database/paper_migrations.py — Stage 4.5 LOCAL PAPER PORTFOLIO (additive). Idempotent; safe on any database state. Only
NEW tables: no strategy, backtest, forward, scanner, alert, research, AI-history, real-brokerage portfolio or settings table is
touched, and PRAGMA user_version is left alone (like every other Stage 3 / 4 migration).

Money and prices are exact decimal STRINGS written by paper/accounting.py (prices 4 dp, money 2 dp, ROUND_HALF_UP).

  * paper_accounts        one local, USD, cash-only account. starting_cash, base_currency, slippage_bps and
                          commission_per_order are frozen once the account has a fill; only the name stays editable.
  * paper_orders          MARKET orders filled at the next valid session open. PENDING -> FILLED | CANCELLED | REJECTED
                          (once); a resolved order never changes. While PENDING only the wait note may change.
  * paper_fills           one immutable fill per order (UNIQUE order_id), with its price source and cash after the fill
                          (never negative).
  * paper_lots            one immutable FIFO acquisition lot per BUY fill.
  * paper_lot_closures    immutable consumption of lots by SELL fills (FIFO); a lot can never be closed beyond its size.
Nothing is ever deleted or replaced.
"""
import sqlite3

_H = "CHECK (length({c}) = 32)"
_DEC = "CHECK ({c} GLOB '[0-9]*' AND {c} NOT GLOB '*[^0-9.]*' AND length({c}) <= 20)"          # unsigned decimal
_SDEC = "CHECK ({c} NOT GLOB '*[^0-9.-]*' AND length({c}) <= 21)"                             # signed decimal
_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS paper_accounts (
        account_id            TEXT PRIMARY KEY {_H.format(c="account_id")},
        name                  TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
        base_currency         TEXT NOT NULL CHECK (base_currency = 'USD'),
        starting_cash         TEXT NOT NULL {_DEC.format(c="starting_cash")},
        slippage_bps          TEXT NOT NULL {_DEC.format(c="slippage_bps")},
        commission_per_order  TEXT NOT NULL {_DEC.format(c="commission_per_order")},
        status                TEXT NOT NULL CHECK (status = 'ACTIVE'),
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS paper_accounts_identity_fixed BEFORE UPDATE OF account_id, created_at ON paper_accounts
    BEGIN SELECT RAISE(ABORT, 'a paper account id never changes'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS paper_accounts_money_frozen
    BEFORE UPDATE OF starting_cash, base_currency, slippage_bps, commission_per_order ON paper_accounts
    WHEN EXISTS (SELECT 1 FROM paper_fills WHERE account_id = OLD.account_id)
    BEGIN SELECT RAISE(ABORT, 'starting cash, currency and costs are frozen once the paper account has a fill'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_orders (
        order_id               TEXT PRIMARY KEY {_H.format(c="order_id")},
        account_id             TEXT NOT NULL REFERENCES paper_accounts(account_id) ON DELETE RESTRICT,
        symbol                 TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 10 AND symbol = upper(symbol)),
        side                   TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity               INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity BETWEEN 1 AND 1000000),
        order_type             TEXT NOT NULL CHECK (order_type = 'MARKET'),
        timing                 TEXT NOT NULL CHECK (timing = 'NEXT_SESSION_OPEN'),
        origin                 TEXT NOT NULL CHECK (origin = 'MANUAL'),
        strategy_version_id    TEXT CHECK (strategy_version_id IS NULL OR length(strategy_version_id) = 32),
        decision_session       TEXT NOT NULL CHECK (length(decision_session) = 10),
        earliest_fill_session  TEXT NOT NULL CHECK (length(earliest_fill_session) = 10 AND earliest_fill_session > decision_session),
        submitted_at           TEXT NOT NULL,
        estimate_price         TEXT NOT NULL {_DEC.format(c="estimate_price")},
        estimate_amount        TEXT NOT NULL {_DEC.format(c="estimate_amount")},
        status                 TEXT NOT NULL CHECK (status IN ('PENDING', 'FILLED', 'CANCELLED', 'REJECTED')),
        wait_reason            TEXT,
        checked_at             TEXT,
        reject_reason          TEXT,
        resolved_at            TEXT,
        fill_session           TEXT,
        fill_id                TEXT,
        CHECK ((status = 'FILLED') = (fill_id IS NOT NULL AND fill_session IS NOT NULL)),
        CHECK ((status = 'REJECTED') = (reject_reason IS NOT NULL)),
        CHECK ((status = 'PENDING') = (resolved_at IS NULL))
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_paper_orders_status ON paper_orders (account_id, status, submitted_at);",
    """
    CREATE TRIGGER IF NOT EXISTS paper_orders_resolved_final BEFORE UPDATE ON paper_orders WHEN OLD.status != 'PENDING'
    BEGIN SELECT RAISE(ABORT, 'a filled, cancelled or rejected paper order never changes'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS paper_orders_identity_fixed
    BEFORE UPDATE OF order_id, account_id, symbol, side, quantity, order_type, timing, origin, strategy_version_id,
                     decision_session, earliest_fill_session, submitted_at, estimate_price, estimate_amount ON paper_orders
    BEGIN SELECT RAISE(ABORT, 'a paper order is never edited (cancel it and create a new one)'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_fills (
        fill_id            TEXT PRIMARY KEY {_H.format(c="fill_id")},
        order_id           TEXT NOT NULL UNIQUE REFERENCES paper_orders(order_id) ON DELETE RESTRICT,
        account_id         TEXT NOT NULL REFERENCES paper_accounts(account_id) ON DELETE RESTRICT,
        symbol             TEXT NOT NULL,
        side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity           INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
        fill_session       TEXT NOT NULL CHECK (length(fill_session) = 10),
        base_price         TEXT NOT NULL {_DEC.format(c="base_price")},
        slippage_bps       TEXT NOT NULL {_DEC.format(c="slippage_bps")},
        effective_price    TEXT NOT NULL {_DEC.format(c="effective_price")},
        notional           TEXT NOT NULL {_DEC.format(c="notional")},
        commission         TEXT NOT NULL {_DEC.format(c="commission")},
        cash_delta         TEXT NOT NULL {_SDEC.format(c="cash_delta")},
        cash_after         TEXT NOT NULL {_DEC.format(c="cash_after")},
        cost_basis_removed TEXT CHECK (cost_basis_removed IS NULL OR cost_basis_removed NOT GLOB '*[^0-9.]*'),
        realized_pnl       TEXT CHECK (realized_pnl IS NULL OR realized_pnl NOT GLOB '*[^0-9.-]*'),
        price_source       TEXT NOT NULL CHECK (price_source IN ('STAGE_3_2_CACHE', 'MEMORY_CACHE', 'FETCHED_NOW')),
        adjustment         TEXT NOT NULL,
        feed               TEXT NOT NULL,
        dataset_id         TEXT,
        content_hash       TEXT,
        filled_at          TEXT NOT NULL,
        engine_version     TEXT NOT NULL,
        CHECK ((side = 'SELL') = (realized_pnl IS NOT NULL AND cost_basis_removed IS NOT NULL))
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_paper_fills_account ON paper_fills (account_id, fill_session);",
    f"""
    CREATE TABLE IF NOT EXISTS paper_lots (
        lot_id          TEXT PRIMARY KEY {_H.format(c="lot_id")},
        account_id      TEXT NOT NULL REFERENCES paper_accounts(account_id) ON DELETE RESTRICT,
        symbol          TEXT NOT NULL,
        open_fill_id    TEXT NOT NULL UNIQUE REFERENCES paper_fills(fill_id) ON DELETE RESTRICT,
        entry_session   TEXT NOT NULL CHECK (length(entry_session) = 10),
        quantity        INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
        total_cost      TEXT NOT NULL {_DEC.format(c="total_cost")},
        created_at      TEXT NOT NULL
    );
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_lot_closures (
        closure_id      TEXT PRIMARY KEY {_H.format(c="closure_id")},
        account_id      TEXT NOT NULL REFERENCES paper_accounts(account_id) ON DELETE RESTRICT,
        lot_id          TEXT NOT NULL REFERENCES paper_lots(lot_id) ON DELETE RESTRICT,
        sell_fill_id    TEXT NOT NULL REFERENCES paper_fills(fill_id) ON DELETE RESTRICT,
        symbol          TEXT NOT NULL,
        quantity        INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
        cost_basis      TEXT NOT NULL {_DEC.format(c="cost_basis")},
        proceeds        TEXT NOT NULL {_DEC.format(c="proceeds")},
        realized_pnl    TEXT NOT NULL {_SDEC.format(c="realized_pnl")},
        UNIQUE (lot_id, sell_fill_id)
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS paper_lot_closures_never_oversold BEFORE INSERT ON paper_lot_closures
    WHEN NEW.quantity + COALESCE((SELECT SUM(quantity) FROM paper_lot_closures WHERE lot_id = NEW.lot_id), 0)
         > (SELECT quantity FROM paper_lots WHERE lot_id = NEW.lot_id)
    BEGIN SELECT RAISE(ABORT, 'a paper lot can never be sold beyond its size (no short, no negative lot)'); END;
    """,
]

for _t, _key in (("paper_accounts", "account_id"), ("paper_orders", "order_id"), ("paper_fills", "fill_id"),
                 ("paper_lots", "lot_id"), ("paper_lot_closures", "closure_id")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} "
                       f"BEGIN SELECT RAISE(ABORT, 'paper records are never deleted'); END;")
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} "
                       f"WHERE {_key} = NEW.{_key}) BEGIN SELECT RAISE(ABORT, 'paper records are never replaced'); END;")
for _t in ("paper_fills", "paper_lots", "paper_lot_closures"):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_immutable BEFORE UPDATE ON {_t} "
                       f"BEGIN SELECT RAISE(ABORT, 'paper fills and lots are immutable'); END;")
_STATEMENTS.append("CREATE TRIGGER IF NOT EXISTS paper_fills_one_per_order BEFORE INSERT ON paper_fills WHEN EXISTS "
                   "(SELECT 1 FROM paper_fills WHERE order_id = NEW.order_id) "
                   "BEGIN SELECT RAISE(ABORT, 'a paper order fills at most once'); END;")


def run_paper_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
