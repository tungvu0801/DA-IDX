"""Stage 4.7 Phase 2 — the additive migration (database/rotation_migrations.py) and the immutable store (rotation/store.py).
Fully offline: a scratch database per test, no network, no broker, no market data (conftest blocks sockets and tripwires
the Alpaca wires anyway)."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from database import rotation_migrations as M
from rotation import rules as R
from rotation import store as S

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
NOW = datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)
CONFIG = {"weights": R.DEFAULT_WEIGHTS, "portfolio_size": 10, "exit_rank": 15, "cash_buffer_pct": "0.05",
          "rebalance_threshold": "0.01", "max_turnover_per_rotation": "0.50", "max_position_weight": "0.20",
          "min_position_weight": "0.05", "excluded_symbols": ["ko", "AMD", "KO"]}


@pytest.fixture
def st(tmp_path):
    return S.RotationStore(tmp_path / "rotation.db")


def cfg(st, name="base", **over):
    return st.create_config(name, {**CONFIG, **over}, NOW)


def run_rows(status="VALID", symbols=("AMD", "KO", "MU"), **over):
    """A complete run: 3 candidates (2 eligible), 2 targets, 3 items (one EXIT with a fractional current quantity)."""
    targets = [{"symbol": "KO", "rank": 1, "target_weight": D("0.475000"), "reference_price": D("60.0000"),
                "target_notional": D("4750.00"), "est_target_qty": 79, "reason": "TOP_N", "flags": []},
               {"symbol": "AMD", "rank": 2, "target_weight": D("0.475000"), "reference_price": D("101.0000"),
                "target_notional": D("4750.00"), "est_target_qty": 47, "reason": "RETAINED_RANK_BUFFER", "flags": ["RH_SHARES_HELD"]}]
    items = [{"symbol": "AMD", "current_qty": D("10"), "current_weight": D("0.101000"), "target_weight": D("0.475000"),
              "weight_diff": D("0.374000"), "est_qty_diff": 37, "side_hint": "BUY", "action": "INCREASE", "reason": None, "handoff_allowed": 1},
             {"symbol": "KO", "current_qty": D("0"), "current_weight": D("0"), "target_weight": D("0.475000"),
              "weight_diff": D("0.475000"), "est_qty_diff": 79, "side_hint": "BUY", "action": "ADD", "reason": None, "handoff_allowed": 1},
             {"symbol": "MU", "current_qty": D("2.5"), "current_weight": D("0.005000"), "target_weight": D("0"),
              "weight_diff": D("-0.005000"), "est_qty_diff": 2, "side_hint": "SELL", "action": "EXIT", "reason": "TARGET_ZERO", "handoff_allowed": 1}]
    cands = [{"symbol": "KO", "eligible": True, "reasons": [], "raw": {"ret20": "0.1"}, "scores": {"momentum": "100"}, "composite": D("80.5"),
              "rank": 1, "reference_price": D("60"), "avg_dollar_volume": D("9000000"), "flags": []},
             {"symbol": "AMD", "eligible": True, "reasons": [], "raw": {"ret20": "0.05"}, "scores": {"momentum": "50"}, "composite": D("70"),
              "rank": 2, "reference_price": D("101"), "avg_dollar_volume": D("50000000"), "flags": ["RH_SHARES_HELD"]},
             {"symbol": "MU", "eligible": False, "reasons": ["LOW_LIQUIDITY", "INSUFFICIENT_HISTORY"], "raw": {"ret20": None},
              "scores": None, "composite": None, "rank": None, "reference_price": D("20"), "avg_dollar_volume": D("1000"), "flags": []}]
    cands = [c for c in cands if c["symbol"] in symbols]
    turnover, ccw, tcw = D("0.849000"), D("0.894000"), D("0.050000")
    run = {"config_id": None, "config_hash": None, "run_at": "2026-10-05T21:00:00+00:00", "data_session": "2026-10-02",
           "universe_source": "WATCHLIST", "universe_ref": None, "universe_json": list(symbols), "universe_hash": "a" * 64,
           "portfolio_source": "ROBINHOOD_READ_ONLY", "portfolio_snapshot_at": "2026-10-05T20:50:00+00:00", "snapshot_status": "OK",
           "snapshot_cash": D("8940.00"), "positions_json": [{"symbol": "AMD", "quantity": "10"}, {"symbol": "MU", "quantity": "2.5"}],
           "source_meta_json": {"market_value": "12345.67", "buying_power": "999999"}, "source_mismatch_note": "Proposal based on Robinhood holdings.",
           "reference_equity": D("10000.00"), "current_cash_weight": ccw, "target_cash_weight": tcw, "input_hash": "b" * 64,
           "proposal_hash": S.proposal_hash(status, turnover, ccw, tcw, targets, items), "status": status, "status_detail": None,
           "n_universe": len(cands), "n_eligible": sum(1 for c in cands if c["eligible"]), "n_selected": len(targets),
           "turnover": turnover, "market_data_requests": 1, "completed_at": "2026-10-05T21:00:03+00:00", **over}
    return run, cands, targets, items


def insert(st, status="VALID", **over):
    c = cfg(st)
    run, cands, targets, items = run_rows(status, **over)
    run.update(config_id=c["config_id"], config_hash=c["config_hash"])
    return st.insert_run(run, cands, targets, items), run, cands, targets, items


def raw(st):
    conn = sqlite3.connect(str(st.path))
    conn.row_factory = sqlite3.Row
    return conn


# ==================================================================================================================================
# configs
# ==================================================================================================================================

def test_1_create_config_is_canonical_versioned_and_tables_appear_on_first_write(st):
    assert not st.exists() and st.configs() == [] and st.config("0" * 32) is None
    c = cfg(st)
    assert st.exists() and c["created"] is True and c["version"] == 1 and c["benchmark"] == "SPY" and re.fullmatch(r"[0-9a-f]{32}", c["config_id"])
    assert c["config"]["excluded_symbols"] == ["AMD", "KO"]                                    # upper-cased, de-duplicated, sorted
    assert c["config"]["weights"]["drawdown"] == "0.000000" and c["config"]["cash_buffer_pct"] == "0.050000"
    assert c["config"]["min_price"] == "5.0000" and c["config"]["benchmark"] == "SPY"            # defaults applied
    assert c["config_hash"] == S.sha256_hex(c["config_json"]) and re.fullmatch(r"[0-9a-f]{64}", c["config_hash"])
    assert st.config(c["config_id"])["config_json"] == c["config_json"] and st.config_by_hash(c["config_hash"])["config_id"] == c["config_id"]
    with sqlite3.connect(str(st.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0                            # never touched


def test_2_same_name_creates_version_2_not_a_mutation(st):
    c1 = cfg(st)
    c2 = cfg(st, portfolio_size=8, exit_rank=12)
    assert (c1["version"], c2["version"]) == (1, 2) and c1["config_id"] != c2["config_id"] and c1["config_hash"] != c2["config_hash"]
    assert [x["version"] for x in st.configs("base")] == [1, 2] and st.config(c1["config_id"])["config_json"] == c1["config_json"]
    other = cfg(st, name="other", portfolio_size=5, exit_rank=9)
    assert other["version"] == 1 and [(x["name"], x["version"]) for x in st.configs()] == [("base", 1), ("base", 2), ("other", 1)]
    again = cfg(st)                                                                              # identical content: returned, not re-created
    assert again["created"] is False and again["config_id"] == c1["config_id"] and len(st.configs("base")) == 2


def test_3_4_config_update_and_delete_fail(st):
    c = cfg(st)
    with raw(st) as conn:
        for sql in ("UPDATE portfolio_rotation_configs SET name = 'x'", "UPDATE portfolio_rotation_configs SET config_json = '{}'",
                    "UPDATE portfolio_rotation_configs SET benchmark = 'QQQ'", "DELETE FROM portfolio_rotation_configs"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
    assert st.config(c["config_id"])["config_json"] == c["config_json"]


def test_invalid_configs_are_never_persisted(st):
    for bad, code in (({"portfolio_size": 10, "min_position_weight": "0.10"}, "INVALID_WEIGHT_BOUNDS"),
                      ({"weights": {**R.DEFAULT_WEIGHTS, "momentum": "0.31"}}, "INVALID_WEIGHTS"),
                      ({"exit_rank": 9}, "INVALID_EXIT_RANK"), ({"benchmark": "QQQ"}, "INVALID_CONFIG"),
                      ({"extra": 1}, "INVALID_CONFIG"), ({"excluded_symbols": ["bad!"]}, "INVALID_CONFIG"),
                      ({"min_price": "0"}, "INVALID_CONFIG"), ({"max_snapshot_age_min": 0}, "INVALID_CONFIG"),
                      ({"cash_buffer_pct": 0.05}, "INVALID_CONFIG")):
        with pytest.raises((S.StoreError, R.ConfigError)) as e:
            cfg(st, **bad)
        assert e.value.code == code, bad
    assert not st.exists()                                                                       # nothing was ever written


# ==================================================================================================================================
# runs and children
# ==================================================================================================================================

def test_5_6_completed_run_update_and_delete_fail(st):
    rid, run, *_ = insert(st)
    with raw(st) as conn:
        for sql in ("UPDATE portfolio_rotation_runs SET status = 'INPUT_ERROR'", "UPDATE portfolio_rotation_runs SET turnover = '0'",
                    "UPDATE portfolio_rotation_runs SET source_meta_json = '{}'", "DELETE FROM portfolio_rotation_runs"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
    assert st.run(rid)["status"] == "VALID" and st.run(rid)["proposal_hash"] == run["proposal_hash"]


def test_7_8_9_children_update_and_delete_fail(st):
    rid, *_ = insert(st)
    with raw(st) as conn:
        for table, col in (("portfolio_rotation_candidates", "rank"), ("portfolio_rotation_targets", "target_weight"),
                           ("portfolio_rebalance_items", "action")):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"UPDATE {table} SET {col} = NULL")
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"DELETE FROM {table}")
    assert len(st.candidates(rid)) == 3 and len(st.targets(rid)) == 2 and len(st.items(rid)) == 3


def test_10_failed_runtime_run_is_persisted(st):
    c = cfg(st)
    for status, detail in (("DATA_STALE", "snapshot older than 30 minutes"), ("INPUT_ERROR", "no reference price for MU"),
                           ("NO_ELIGIBLE_CANDIDATES", None), ("INSUFFICIENT_CANDIDATES", None), ("TURNOVER_LIMIT_EXCEEDED", "0.849 > 0.5")):
        ph = S.proposal_hash(status, None, None, None, [], [])
        run = {"config_id": c["config_id"], "config_hash": c["config_hash"], "run_at": "2026-10-05T21:00:00+00:00", "data_session": None,
               "universe_source": "CUSTOM", "universe_ref": None, "universe_json": ["AMD"], "universe_hash": "c" * 64,
               "portfolio_source": "ALPACA_PAPER_VIEW", "portfolio_snapshot_at": None, "snapshot_status": None, "snapshot_cash": None,
               "positions_json": None, "source_meta_json": None, "source_mismatch_note": None, "reference_equity": None,
               "current_cash_weight": None, "target_cash_weight": None, "input_hash": "d" * 64, "proposal_hash": ph, "status": status,
               "status_detail": detail, "n_universe": 0, "n_eligible": 0, "n_selected": 0, "turnover": None, "market_data_requests": 0,
               "completed_at": "2026-10-05T21:00:01+00:00"}
        rid = st.insert_run(run, [], [], [])
        got = st.run(rid)
        assert got["status"] == status and got["status_detail"] == detail and st.candidates(rid) == [] and st.items(rid) == []
        assert st.verify_run(rid) == {"run_id": rid, "ok": True, "problems": [], "n_candidates": 0, "n_targets": 0, "n_items": 0}
    assert len(st.runs()) == 5


def test_11_malformed_rows_are_rejected_by_check_constraints(st):
    c = cfg(st)
    base, cands, targets, items = run_rows()
    base.update(config_id=c["config_id"], config_hash=c["config_hash"])
    for over in ({"input_hash": "zz"}, {"universe_hash": "x" * 64}, {"n_universe": -1}, {"n_eligible": "two"}, {"status": "MAYBE"},
                 {"snapshot_cash": "-5"}, {"turnover": "abc"}, {"data_session": "2026/10/02"}, {"universe_source": "EVERYTHING"},
                 {"snapshot_status": "FRESH"}, {"positions_json": "not json"}, {"completed_at": None}, {"market_data_requests": 1.5}):
        with pytest.raises((sqlite3.DatabaseError, S.StoreError)):
            st.insert_run({**base, **over}, cands, targets, items)
    bad_children = (([{**cands[0], "symbol": "amd"}] + cands[1:], targets, items),            # lowercase symbol
                    ([{**cands[0], "eligible": False}] + cands[1:], targets, items),           # ineligible yet ranked
                    (cands, [{**targets[0], "reason": "BECAUSE"}] + targets[1:], items),
                    (cands, [{**targets[0], "est_target_qty": -1}] + targets[1:], items),
                    (cands, targets, [{**items[0], "est_qty_diff": 2.5}] + items[1:]),
                    (cands, targets, [{**items[0], "current_qty": "-1"}] + items[1:]),
                    (cands, targets, items + [{**items[0]}]),                                  # duplicate symbol
                    (cands, targets + [{**targets[0], "symbol": "ZZZ"}], items))              # target without candidate
    for cs, ts, its in bad_children:
        with pytest.raises((sqlite3.DatabaseError, S.StoreError)):
            st.insert_run(dict(base), cs, ts, its)
    assert st.runs() == [] and st.exists()                                                      # nothing persisted, tables exist


def test_12_13_14_15_invalid_enumerations_rejected(st):
    c = cfg(st)
    base, cands, targets, items = run_rows()
    base.update(config_id=c["config_id"], config_hash=c["config_hash"])
    with pytest.raises(sqlite3.DatabaseError):
        st.insert_run({**base, "portfolio_source": "MANUAL"}, cands, targets, items)             # 12 invalid portfolio_source
    with raw(st) as conn:                                                                        # 13 benchmark is SPY only
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO portfolio_rotation_configs (config_id, name, version, config_json, config_hash, benchmark, created_at) "
                         "VALUES (?, 'x', 1, '{}', ?, 'QQQ', 'now')", ("1" * 32, "2" * 64))
    with pytest.raises(sqlite3.DatabaseError):
        st.insert_run(dict(base), cands, targets, [{**items[0], "side_hint": "SHORT"}] + items[1:])      # 14
    with pytest.raises(sqlite3.DatabaseError):
        st.insert_run(dict(base), cands, targets, [{**items[0], "action": "SUBMIT"}] + items[1:])        # 15
    assert st.runs() == []
    assert set(M.ACTIONS) == set(R.ACTIONS) and M.PORTFOLIO_SOURCES == ("ALPACA_PAPER_VIEW", "ROBINHOOD_READ_ONLY", "LOCAL_SIMULATOR")


def test_16_fractional_current_qty_persists_exactly(st):
    rid, *_ = insert(st)
    mu = next(i for i in st.items(rid) if i["symbol"] == "MU")
    assert mu["current_qty"] == "2.5" and Decimal(mu["current_qty"]) == D("2.5") and mu["est_qty_diff"] == 2
    assert isinstance(mu["est_qty_diff"], int) and mu["weight_diff"] == "-0.005000" and mu["side_hint"] == "SELL"
    with raw(st) as conn:
        assert tuple(conn.execute("SELECT typeof(current_qty), typeof(est_qty_diff) FROM portfolio_rebalance_items WHERE symbol='MU'").fetchone()) == ("text", "integer")


def test_17_complete_run_insert_is_atomic_and_children_round_trip(st):
    rid, run, cands, targets, items = insert(st)
    got = st.run(rid)
    assert got["status"] == "VALID" and got["portfolio_source"] == "ROBINHOOD_READ_ONLY" and got["benchmark"] == "SPY"
    assert json.loads(got["positions_json"]) == [{"symbol": "AMD", "quantity": "10"}, {"symbol": "MU", "quantity": "2.5"}]
    assert json.loads(got["source_meta_json"]) == {"buying_power": "999999", "market_value": "12345.67"}   # informational only
    assert got["reference_equity"] == "10000.00" and got["snapshot_cash"] == "8940.00" and got["turnover"] == "0.849000"
    assert [t["symbol"] for t in st.targets(rid)] == ["KO", "AMD"] and st.targets(rid)[1]["flags_json"] == '["RH_SHARES_HELD"]'
    assert [c["symbol"] for c in st.candidates(rid)] == ["KO", "AMD", "MU"]                   # eligible by rank, then ineligible
    assert json.loads(st.candidates(rid)[2]["reasons_json"]) == ["LOW_LIQUIDITY", "INSUFFICIENT_HISTORY"]
    assert st.verify_run(rid)["ok"] is True and len(st.runs()) == 1


def test_18_forced_child_insert_failure_rolls_back_the_entire_run(st, monkeypatch):
    c = cfg(st)
    run, cands, targets, items = run_rows()
    run.update(config_id=c["config_id"], config_hash=c["config_hash"])
    real = S.RotationStore._item_row

    def boom(r):
        if r["symbol"] == "MU":
            raise RuntimeError("simulated failure while writing the last child")
        return real(r)
    monkeypatch.setattr(S.RotationStore, "_item_row", staticmethod(boom))
    with pytest.raises(RuntimeError):
        st.insert_run(run, cands, targets, items)
    with raw(st) as conn:
        assert [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in M.TABLES] == [1, 0, 0, 0, 0]   # config only
    monkeypatch.undo()
    rid = st.insert_run(run, cands, targets, items)                                              # the same rows then succeed
    assert len(st.items(rid)) == 3


def test_19_canonical_config_json_and_hash_are_stable_across_key_ordering(st):
    a = S.normalise_config(CONFIG)
    shuffled = {k: CONFIG[k] for k in reversed(list(CONFIG))}
    shuffled["weights"] = {k: CONFIG["weights"][k] for k in reversed(list(CONFIG["weights"]))}
    shuffled["excluded_symbols"] = ["amd", "KO"]
    b = S.normalise_config(shuffled)
    assert S.canonical_json(a) == S.canonical_json(b) and S.config_hash(a) == S.config_hash(b)
    assert S.canonical_json({"b": D("1.50"), "a": [D("2E+1")]}) == '{"a":["20"],"b":"1.50"}'      # Decimals as plain strings
    assert S.dec_str(D("0.000001")) == "0.000001" and S.dec_str(None) is None
    with pytest.raises(S.StoreError):
        S.dec_str(0.1)                                                                           # floats are refused
    c1 = cfg(st)
    assert cfg(st, name="other", **{"excluded_symbols": ["amd", "KO"]})["config_hash"] == c1["config_hash"]   # same content → same hash


def test_20_children_are_returned_in_deterministic_order_and_hash_is_order_independent(st):
    rid, run, cands, targets, items = insert(st)
    assert [c["symbol"] for c in st.candidates(rid)] == ["KO", "AMD", "MU"]
    assert [t["rank"] for t in st.targets(rid)] == [1, 2] and [i["symbol"] for i in st.items(rid)] == ["AMD", "KO", "MU"]
    rid2, *_ = insert(st)
    assert [i["symbol"] for i in st.items(rid2)] == ["AMD", "KO", "MU"]
    rev = S.proposal_hash("VALID", run["turnover"], run["current_cash_weight"], run["target_cash_weight"], list(reversed(targets)), list(reversed(items)))
    assert rev == run["proposal_hash"]                                                           # symbol-sorted inside the hash
    stored = S.proposal_hash(st.run(rid)["status"], st.run(rid)["turnover"], st.run(rid)["current_cash_weight"],
                             st.run(rid)["target_cash_weight"], st.targets(rid), st.items(rid))
    assert stored == run["proposal_hash"]                                                        # recomputable from stored rows


def test_integrity_check_reports_but_never_repairs(st):
    rid, run, cands, targets, items = insert(st)
    assert st.verify_run(rid)["ok"]
    c = cfg(st)
    bad_hash = {**run, "proposal_hash": "e" * 64, "config_id": c["config_id"], "config_hash": c["config_hash"], "n_selected": 1}
    rid2 = st.insert_run(bad_hash, cands, targets, items)
    v = st.verify_run(rid2)
    assert v["ok"] is False and v["problems"] == ["TARGET_COUNT", "PROPOSAL_HASH"]
    assert st.run(rid2)["proposal_hash"] == "e" * 64                                            # reported, not rewritten
    with pytest.raises(S.StoreError):
        st.verify_run("0" * 32)


def test_migration_is_additive_idempotent_and_exact(tmp_path):
    db = tmp_path / "m.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE other (x)")
        c.execute("PRAGMA user_version = 7")
        for _ in range(3):
            M.run_rotation_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 7
        objs = sorted(c.execute("SELECT type, name FROM sqlite_master WHERE name != 'other'"))
    assert [n for t, n in objs if t == "table"] == sorted(M.TABLES)
    assert [n for t, n in objs if t == "trigger"] == sorted(f"{p}_{k}" for p in ("prc", "prr", "prcand", "prt", "pri") for k in ("no_update", "no_delete"))
    assert {n for t, n in objs if t == "index" and not n.startswith("sqlite_autoindex")} == {"idx_prr_config", "idx_prr_run_at", "idx_pri_run"}
    assert sorted(p.name for p in (ROOT / "database").glob("*rotation*")) == ["rotation_migrations.py"]


def test_store_is_offline_and_order_free():
    for f in ("database/rotation_migrations.py", "rotation/store.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"^\s*(from|import)\s+(requests|socket|urllib|http|alpaca|anthropic|agents|portfolio|rh_gateway|"
                             r"backtest|data\.|api|threading|subprocess|random)\b", code, re.M), f
        assert not re.search(r"requests\.|urllib|socket|/v2/|TradingClient|place_?order|submit_?order|os\.environ|getenv|\.env|"
                             r"load_bars|fetch_daily_bars|get_provider|provider_factory|get_positions|get_portfolio|eval\(|exec\(|"
                             r"__import__|importlib|float\(|DELETE FROM|UPDATE portfolio", code), f
    svc = (ROOT / "rotation" / "store.py").read_text(encoding="utf-8")
    assert svc.count("from paper import alpaca_order_rules as RU") == 1 and "RU.SYMBOL_RE" in svc and svc.count("RU.") == 1
