"""Stage 3.1: Strategy Lab — declarative strategy specs, feature registry, immutable versions (no backtest, no orders)."""
import copy
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import tm
from strategy import evaluate as E
from strategy import features as F
from strategy import spec as S
from strategy.examples import pullback_example
from strategy.store import StrategyError, StrategyStore

ROOT = Path(__file__).resolve().parents[1]


def good(**over):
    s = pullback_example()
    s["universe"]["symbols"] = ["AMD", "MU", "NVDA"]
    s.update(over)
    return s


def codes(spec):
    return [e["code"] for e in S.validate(copy.deepcopy(spec))[1]]


def cond(fid, op, value=None):
    c = {"feature": fid, "op": op}
    if value is not None:
        c["value"] = value
    return c


def with_entry(*conds, logic="ALL"):
    return good(entry={"logic": logic, "conditions": list(conds)})


# ---- 43: schema -----------------------------------------------------------------------------------------------

def test_valid_strategy_normalises_and_is_backtest_ready():
    rep = S.report(good())
    assert rep["valid"] and rep["errors"] == []
    assert rep["readiness"]["status"] == S.READY and rep["spec"]["universe"]["symbols"] == ["AMD", "MU", "NVDA"]
    assert rep["spec"]["feature_registry_fingerprint"] == F.fingerprint() and len(rep["spec_hash"]) == 64


@pytest.mark.parametrize("bad, code", [
    (cond("stock.not_a_feature", "==", "X"), "UNKNOWN_FEATURE"),
    (cond("stock.rsi_14", "contains", 30), "INVALID_OPERATOR"),        # wrong operator for a number
    (cond("stock.trend", ">", "UPTREND"), "INVALID_OPERATOR"),         # wrong operator for a choice
    (cond("stock.rsi_14", "<", "thirty"), "INVALID_VALUE"),             # wrong value type
    (cond("stock.rsi_14", "<", True), "INVALID_VALUE"),                 # booleans are not numbers
    (cond("stock.rsi_14", "<", 130), "OUT_OF_BOUNDS"),                  # numeric bounds
    (cond("stock.trend", "==", "SIDEWAYS"), "INVALID_ENUM"),            # invalid enum
    (cond("stock.trend", "in", ["UPTREND", "NOPE"]), "INVALID_ENUM"),
    (cond("stock.rsi_14", "between", [60, 40]), "INVALID_VALUE"),       # low above high
    (cond("stock.extended", "is_true", 1), "INVALID_VALUE"),            # booleans take no value
])
def test_invalid_conditions_return_structured_errors(bad, code):
    _, errors = S.validate(with_entry(bad))
    assert code in [e["code"] for e in errors]
    e = next(e for e in errors if e["code"] == code)
    assert e["path"].startswith("entry.conditions[0]") and e["message"]


def test_wrong_operator_message_is_specific():
    e = S.validate(with_entry(cond("stock.rsi_14", "contains", 30)))[1][0]
    assert e["feature"] == "stock.rsi_14" and 'Operator "contains" is not valid for a numeric feature' in e["message"]


def test_empty_entry_and_empty_exit_are_rejected():
    assert "EMPTY_ENTRY" in codes(good(entry={"logic": "ALL", "conditions": []}))
    assert "EMPTY_EXIT" in codes(good(exit={"logic": "ANY", "conditions": [], "invalidation": None, "target": None,
                                            "max_holding_days": None}))
    assert "EMPTY_EXIT" not in codes(good(exit={"logic": "ANY", "conditions": [], "max_holding_days": 10}))   # enough


@pytest.mark.parametrize("field, value, code", [("timeframe", "1H", "INVALID_TIMEFRAME"), ("timeframe", "5m", "INVALID_TIMEFRAME"),
                                                ("direction", "SHORT", "INVALID_DIRECTION"), ("direction", "LONG_SHORT", "INVALID_DIRECTION"),
                                                ("schema_version", 2, "SCHEMA_VERSION"), ("name", "  ", "REQUIRED")])
def test_top_level_fields(field, value, code):
    assert code in codes(good(**{field: value}))


def test_duplicate_symbols_and_normalisation():
    s = good()
    s["universe"]["symbols"] = [" amd", "Mu", "nvda "]
    assert S.validate(s)[0]["universe"]["symbols"] == ["AMD", "MU", "NVDA"]
    s["universe"]["symbols"] = ["AMD", "amd"]
    assert "DUPLICATE_SYMBOL" in codes(s)
    s["universe"]["symbols"] = ["AMD", "BAD SYMBOL", "12AB"]
    assert codes(s).count("INVALID_SYMBOL") == 2
    s["universe"] = {"type": "WATCHLIST"}                               # "whatever is in my watchlist later"
    assert "INVALID_UNIVERSE" in codes(s)


def test_logic_all_and_any_and_nesting_limit():
    a = S.validate(with_entry(cond("stock.trend", "==", "UPTREND"), cond("stock.extended", "is_false"), logic="ALL"))[0]
    o = S.validate(with_entry(cond("stock.trend", "==", "UPTREND"), cond("stock.extended", "is_false"), logic="ANY"))[0]
    assert a["entry"]["logic"] == "ALL" and o["entry"]["logic"] == "ANY"
    assert "INVALID_LOGIC" in codes(with_entry(cond("stock.trend", "==", "UPTREND"), logic="XOR"))
    one_level = {"logic": "ANY", "conditions": [cond("stock.rsi_14", "<", 30)]}
    assert codes(with_entry(cond("stock.trend", "==", "UPTREND"), one_level)) == []        # depth 2 allowed
    two_levels = {"logic": "ANY", "conditions": [{"logic": "ALL", "conditions": [cond("stock.rsi_14", "<", 30)]}]}
    assert "NESTING_TOO_DEEP" in codes(with_entry(cond("stock.trend", "==", "UPTREND"), two_levels))


def test_unknown_fields_are_rejected_everywhere():
    assert "UNKNOWN_FIELD" in codes(good(python="__import__('os')"))
    assert "UNKNOWN_FIELD" in codes(with_entry({**cond("stock.rsi_14", "<", 30), "code": "print(1)"}))
    s = good()
    s["risk"]["leverage"] = 2
    assert "UNKNOWN_FIELD" in codes(s)


def test_readiness_backtest_ready_forward_only_and_unsupported():
    assert S.report(good())["readiness"]["status"] == S.READY
    fwd = S.report(with_entry(cond("stock.trend", "==", "UPTREND"), cond("research.catalyst", "==", "POSITIVE")))["readiness"]
    assert fwd["status"] == S.FORWARD_ONLY and fwd["label"] == "FORWARD TEST ONLY"
    why = {r["feature"]: r for r in fwd["reasons"]}
    assert why["stock.trend"]["state"] == "HISTORICAL" and why["research.catalyst"]["state"] == "FORWARD_ONLY"
    assert "cannot be reconstructed" in why["research.catalyst"]["why"]
    uns = S.report(with_entry(cond("portfolio.position_weight_pct", "<", 20)))["readiness"]
    assert uns["status"] == S.UNSUPPORTED
    ev = S.report(with_entry(cond("event.risk_level", "!=", "HIGH")))["readiness"]["status"]
    assert ev == S.FORWARD_ONLY                                          # events are never assumed point-in-time


def test_registry_declares_historical_support_conservatively():
    for f in F.FEATURES:
        assert f.feature_id.split(".")[0].upper() == f.scope
        assert set(f.allowed_operators) == set(F.OPS_BY_TYPE[f.data_type]) and f.point_in_time and f.source
        if f.scope in ("STOCK", "SECTOR"):
            assert f.historical_support and f.forward_support
        if f.scope in ("RESEARCH", "EVENT"):
            assert not f.historical_support and f.forward_support
        if f.scope == "PORTFOLIO":
            assert not f.historical_support and not f.forward_support
    assert not F.get("market.major_event_within_24h").historical_support       # event-derived market input
    assert F.get("market.environment").historical_support


def test_registry_reuses_the_existing_calculations():
    from insights.labels import is_extended, momentum_label, price_location, trend_label, volume_label
    m = tm("MU", momentum_5d_pct=12.0, rsi=74.0)
    v = F.stock_values(m)
    assert v["stock.trend"] == trend_label(m.trend) and v["stock.momentum"] == momentum_label(m.momentum_score)
    assert v["stock.volume_level"] == volume_label(m.relative_volume) and v["stock.extended"] is is_extended(12.0, 74.0) is True
    assert v["stock.price_location"] == price_location(m.price, m.support, m.resistance) and v["stock.rsi_14"] == m.rsi
    src = (ROOT / "strategy" / "features.py").read_text(encoding="utf-8")
    body = src[src.index("def stock_values"):]
    assert not re.search(r"ewm\(|rolling\(|np\.|\.mean\(\)|\.std\(\)", body)   # no indicator maths re-implemented
    from test_market_insights import build as build_market
    mk = build_market()
    mv = F.market_values(mk)
    assert mv["market.environment"] == mk["environment"] and mv["market.breadth"] == mk["breadth_label"]


def test_registry_fingerprint_pins_thresholds(monkeypatch):
    before = F.fingerprint()
    assert before == F.fingerprint()                                       # stable
    monkeypatch.setattr(config, "RISK_EXTENDED_MOMENTUM_PCT", 12.5)
    import importlib
    changed = importlib.reload(F).fingerprint()
    monkeypatch.undo()
    importlib.reload(F)
    assert changed != before                                               # a threshold change is detected
    s = good()
    s["feature_registry_fingerprint"] = "0" * 64
    assert "REGISTRY_MISMATCH" in codes(s)


def test_operator_semantics_are_defined_once():
    snap = {"stock.rsi_14": 30.0, "stock.trend": "UPTREND", "stock.extended": False, "stock.momentum": None}
    ok = lambda c: E.condition_met(c, snap)[0]
    assert ok(cond("stock.rsi_14", "between", [30, 40])) and ok(cond("stock.rsi_14", "<=", 30))     # inclusive
    assert not ok(cond("stock.rsi_14", "<", 30)) and ok(cond("stock.rsi_14", "==", 30))
    assert ok(cond("stock.trend", "in", ["UPTREND", "MIXED"])) and not ok(cond("stock.trend", "not_in", ["UPTREND"]))
    assert ok(cond("stock.extended", "is_false")) and not ok(cond("stock.extended", "is_true"))
    met, trace = E.condition_met(cond("stock.momentum", "!=", "WEAK"), snap)
    assert met is False and trace["result"] == "UNAVAILABLE"                 # missing data never triggers
    g = {"logic": "ANY", "conditions": [cond("stock.rsi_14", ">", 50), cond("stock.trend", "==", "UPTREND")]}
    assert E.group_met(g, snap)[0] and not E.group_met({**g, "logic": "ALL"}, snap)[0]


def test_plain_language_summary_is_deterministic():
    t = S.report(good())["summary"]
    assert t == S.report(good())["summary"]
    assert "Enter when all of these 4 conditions are true: stock trend is Up, price location is Near support" in t
    assert "after 10 trading days" in t and "support level that existed at entry" in t and "next session's open" in t


# ---- 44: versioning ----------------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return StrategyStore(tmp_path / "s.db")


def test_versions_are_append_only(store):
    s = store.create(good(), notes="first")
    sid = s["strategy_id"]
    assert s["current_version"] == 1 and s["versions"][0]["version_number"] == 1
    v1 = copy.deepcopy(s["versions"][0])
    edited = good()
    edited["exit"]["max_holding_days"] = 15
    s2 = store.add_version(sid, edited, based_on_version=1)
    assert [v["version_number"] for v in s2["versions"]] == [1, 2]
    assert s2["versions"][0] == v1                                          # v1 unchanged
    assert s2["versions"][1]["spec_hash"] != v1["spec_hash"]                # changed rule -> changed hash
    with pytest.raises(StrategyError) as e:
        store.add_version(sid, edited)                                      # identical -> refused
    assert e.value.code == "NO_CHANGE"
    with pytest.raises(StrategyError) as e:
        store.add_version(sid, good(), based_on_version=1)                  # stale base -> refused
    assert e.value.code == "VERSION_CONFLICT"
    s3 = store.add_version(sid, good(name="Renamed pullback"))
    assert [v["version_number"] for v in s3["versions"]] == [1, 2, 3] and s3["name"] == "Renamed pullback"
    assert s3["versions"][2]["rules_hash"] == v1["rules_hash"] != s3["versions"][1]["rules_hash"]   # name-only change


def test_hash_is_stable_and_canonical():
    a, b = S.validate(good())[0], S.validate(good())[0]
    assert S.spec_hash(a) == S.spec_hash(b)
    c = good()
    c["entry"]["conditions"][0] = {"value": "UPTREND", "op": "==", "feature": "stock.trend"}      # key order
    c["risk"] = {"max_open_positions": 5, "max_position_pct": 10.0}                                # 10.0 == 10
    assert S.spec_hash(S.validate(c)[0]) == S.spec_hash(a)
    assert S.canonical_json(a) == json.dumps(a, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert S.spec_hash(a) == hashlib.sha256(S.canonical_json(a).encode()).hexdigest()
    d = good()
    d["entry"]["conditions"][0]["value"] = "MIXED"
    assert S.spec_hash(S.validate(d)[0]) != S.spec_hash(a)


def test_saved_versions_cannot_be_overwritten_or_deleted(store):
    sid = store.create(good())["strategy_id"]
    conn = sqlite3.connect(str(store.db_path))
    for sql in ("UPDATE strategy_versions SET spec_json = '{}'", "UPDATE strategy_versions SET version_number = 9",
                "DELETE FROM strategy_versions", "DELETE FROM strategy_definitions",
                "UPDATE strategy_definitions SET name = 'x'"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable|never deleted|only archived_at"):
            conn.execute(sql)
    conn.close()
    assert store.get(sid)["versions"][0]["integrity"] == "OK"


def test_tampering_is_detected_on_read(store):
    sid = store.create(good())["strategy_id"]
    conn = sqlite3.connect(str(store.db_path))
    conn.execute("DROP TRIGGER strategy_versions_immutable_update")
    conn.execute("UPDATE strategy_versions SET spec_json = replace(spec_json, 'UPTREND', 'DOWNTREND')")
    conn.commit()
    conn.close()
    assert store.get(sid)["versions"][0]["integrity"] == "INTEGRITY_ERROR"


def test_archive_does_not_touch_versions(store):
    s = store.create(good())
    before = copy.deepcopy(s["versions"])
    a = store.set_archived(s["strategy_id"], True)
    assert a["archived_at"] and a["versions"] == before
    assert store.list() == [] and len(store.list(include_archived=True)) == 1
    assert store.set_archived(s["strategy_id"], False)["archived_at"] is None


def test_version_history_ordered(store):
    sid = store.create(good())["strategy_id"]
    for h in (11, 12, 13):
        s = good()
        s["exit"]["max_holding_days"] = h
        store.add_version(sid, s)
    assert [v["version_number"] for v in store.get(sid)["versions"]] == [1, 2, 3, 4]
    assert store.version(sid, 3)["spec"]["exit"]["max_holding_days"] == 12


# ---- 45: database migration from the current real schema ---------------------------------------------------------

def _digest(conn, tables):
    h = hashlib.sha256()
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE tbl_name IN (%s) ORDER BY name"
                                  % ",".join("?" * len(tables)), tables):
        h.update(f"{name}|{sql}".encode())
    for t in tables:
        cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{t}")'))   # WITHOUT ROWID tables too
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY {cols}'):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def test_migration_on_a_copy_of_the_real_database_is_additive():
    real = ROOT / "data" / "stock_agent.db"
    if not real.exists():
        pytest.skip("no real database in this checkout")
    tmp = Path(tempfile.mkdtemp(prefix="st31-")) / "copy.db"
    src = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    src.close()
    old = [r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'strategy_%' "
                                     "AND name != 'sqlite_sequence'")]
    assert {"research_snapshots", "research_outcomes"} <= set(old)
    before = _digest(dst, old)
    counts = {t: dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in old}
    dst.close()
    st = StrategyStore(tmp)                                                  # migration + real use
    sid = st.create(good())["strategy_id"]
    st.add_version(sid, good(name="v2 name"))
    StrategyStore(tmp)                                                       # idempotent
    conn = sqlite3.connect(str(tmp))
    assert _digest(conn, old) == before                                      # old schema + every old row unchanged
    assert {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in old} == counts
    cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_versions)")}
    assert {"version_id", "strategy_id", "version_number", "schema_version", "spec_json", "spec_hash", "rules_hash",
            "readiness", "created_at", "feature_registry_fingerprint"} <= cols
    fks = conn.execute("PRAGMA foreign_key_list(strategy_versions)").fetchall()
    assert any(f[2] == "strategy_definitions" for f in fks)
    trig = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"strategy_versions_immutable_update", "strategy_versions_immutable_delete"} <= trig
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0            # Stage 1–2.8 migration seam untouched
    conn.close()


def test_existing_migration_module_is_unchanged():
    src = (ROOT / "database" / "migrations.py").read_text(encoding="utf-8")
    assert "strategy" not in src.lower()                                     # Stage 3 lives in its own module


# ---- 46: security --------------------------------------------------------------------------------------------------

STRATEGY_FILES = ["strategy/features.py", "strategy/spec.py", "strategy/evaluate.py", "strategy/store.py",
                  "strategy/examples.py", "strategy/contracts.py", "api/routes/strategies.py",
                  "database/strategy_migrations.py", "frontend/strategy_lab.js"]


def test_no_code_execution_or_broker_mutation_in_strategy_code():
    for f in STRATEGY_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"setTimeout\(|setInterval\(|pickle|marshal", text), f
        assert not re.search(r"TradingClient|place_?order|submit_?order|cancel_?order|webhook|/orders|alpaca\.trading|"
                             r"get_provider\(|anthropic", text, re.I), f


def test_strategy_fields_are_data_never_code():
    evil = good(name="$(rm -rf /); __import__('os').system('x')", description="<script>alert(1)</script>")
    rep = S.report(evil)
    assert rep["valid"] and rep["spec"]["name"].startswith("$(rm")               # stored as plain text only
    js = (ROOT / "frontend" / "strategy_lab.js").read_text(encoding="utf-8")
    assert "esc(d.name)" in js and "esc(s.name)" in js and "innerHTML = `" in js and "JSON.parse(JSON.stringify" in js


def test_api_has_no_backtest_paper_or_execution_endpoint():
    from api.server import app
    paths = app.openapi()["paths"]
    mine = {p: set(ops) for p, ops in paths.items() if p.startswith("/api/strategies")}
    assert mine == {"/api/strategies/features": {"get"}, "/api/strategies/validate": {"post"},
                    "/api/strategies/shortcuts/watchlist": {"get"}, "/api/strategies/shortcuts/holdings": {"get"},
                    "/api/strategies": {"get", "post"}, "/api/strategies/{strategy_id}": {"get"},
                    "/api/strategies/{strategy_id}/versions": {"post"}, "/api/strategies/{strategy_id}/versions/{number}": {"get"},
                    "/api/strategies/{strategy_id}/compare": {"get"}, "/api/strategies/{strategy_id}/archive": {"post"}}
    from paper_endpoints import paper_exempt
    for p, ops in paths.items():
        # Stage 3.2 adds historical backtests under /api/backtests only (exact set in test_backtest_32); still no paper,
        # execution or broker path anywhere — except Stage 4.5's exact LOCAL simulated paper-portfolio paths
        if paper_exempt(p, ops):
            continue
        assert not re.search(r"paper|execute|broker", p), p
        assert not re.search(r"backtest", p) or p.startswith("/api/backtests"), p
        if "order" in p:
            assert set(ops) == {"get"}, p                       # the existing read-only order history only


# ---- API: 0 AI / 0 market / 0 broker work, strict requests --------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from api.routes import portfolio as pr
    from api.server import app
    from insights import service
    from pf_fixtures import FakeGatewayHttp, fake_provider
    from strategy import store as st
    calls, scans = [], []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: scans.append(now))
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: scans.append(syms))
    http = FakeGatewayHttp()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(st, "_store", StrategyStore(Path(tempfile.mkdtemp(prefix="st31api-")) / "api.db"))
    monkeypatch.setattr(st, "get_store", lambda: st._store)
    import api.routes.strategies as routes
    monkeypatch.setattr(routes, "get_store", lambda: st._store)
    c = TestClient(app)
    c.calls, c.scans, c.http = calls, scans, http
    return c


def test_api_round_trip_without_ai_market_or_broker_work(api):
    f = api.get("/api/strategies/features").json()
    assert len(f["features"]) == len(F.FEATURES) and f["example"]["spec"]["universe"]["symbols"] == []
    assert api.post("/api/strategies/validate", json={"spec": f["example"]["spec"]}).json()["valid"] is False  # no symbols
    r = api.post("/api/strategies", json={"spec": good()})
    assert r.status_code == 201
    sid = r.json()["strategy_id"]
    e = good()
    e["entry"]["conditions"].append(cond("research.view", "in", ["BULLISH BIAS", "STRONG BULLISH BIAS"]))
    v2 = api.post(f"/api/strategies/{sid}/versions", json={"spec": e, "based_on_version": 1})
    assert v2.status_code == 201 and v2.json()["versions"][1]["readiness"] == "FORWARD_TEST_ONLY"
    assert api.post(f"/api/strategies/{sid}/versions", json={"spec": e}).status_code == 409
    cmp_ = api.get(f"/api/strategies/{sid}/compare?a=1&b=2").json()
    assert {c["section"] for c in cmp_["changes"]} == {"ENTRY CONDITIONS", "READINESS"}
    assert api.get("/api/strategies").json()["strategies"][0]["current_version"] == 2
    assert api.post("/api/strategies", json={"spec": with_entry(cond("stock.rsi_14", "contains", 3))}).status_code == 422
    assert api.post("/api/strategies", json={"spec": good(), "run_backtest": True}).status_code == 422   # strict body
    assert api.get("/api/strategies/../../etc").status_code == 404
    assert api.calls == [] and api.scans == [] and api.http.requests == []   # no AI, no market scan, no gateway read


def test_shortcuts_copy_symbols_read_only(api):
    w = api.get("/api/strategies/shortcuts/watchlist").json()
    from scanner.watchlist import load_watchlist
    assert w["symbols"] == [s for s in load_watchlist() if S.SYMBOL_RE.match(s)] and w["origin"] == "WATCHLIST_COPY"
    h = api.get("/api/strategies/shortcuts/holdings").json()
    assert h["available"] and h["symbols"] == sorted(h["symbols"]) and h["origin"] == "HOLDINGS_COPY"
    assert [r["path"].split("?")[0] for r in api.http.requests] == ["/positions"]      # one read-only GET
    assert all(r.get("method", "GET") == "GET" for r in api.http.requests) and api.calls == [] and api.scans == []


def test_ui_makes_no_performance_claim_and_loads_lazily():
    js = (ROOT / "frontend" / "strategy_lab.js").read_text(encoding="utf-8")
    assert not re.search(r"win rate|profit factor|expected return|probabilit|projected|profitable", js, re.I)
    assert 'tabBtn.addEventListener("click", () => { if (!loaded) load(); })' in js     # nothing on page load
    load = js[js.index("async function load()"):js.index("const tabBtn")]
    assert re.findall(r'getJSON\("([^"]+)"\)', load) == ["/api/strategies/features"] and "refreshList()" in load
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="strategy">Strategy Lab</button>' in html and 'id="tab-strategy"' in html
