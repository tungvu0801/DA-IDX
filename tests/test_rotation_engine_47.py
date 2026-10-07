"""Stage 4.7 Phase 3 — snapshot adapters, universe resolution and the engine orchestration. Fully offline: synthetic bars
(tests/fw_fixtures.Market), the Stage 2.7C fake gateway (tests/pf_fixtures), fake Stage 4.6A view payloads and a scratch
Stage 4.5 lab. conftest blocks sockets and tripwires the Alpaca wires; the market-data provider is the fake fetch function."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import fw_fixtures as FL
import pf_fixtures as PF
import test_paper_45 as T45
from fit import current as FC
from rotation import engine as E
from rotation import rules as R
from rotation import snapshots as SN
from rotation import store as S
from rotation import universe as U

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
Dt = date.fromisoformat
SESSION = "2026-09-28"                                             # the latest completed session of every run below
NOW = FL.at(Dt(SESSION))                                           # 12:00 UTC the day after
SYMS = ("AMD", "MU", "KO", "CLS", "NVDA", "SNDK")
CONFIG = {"weights": R.DEFAULT_WEIGHTS, "portfolio_size": 3, "exit_rank": 5, "cash_buffer_pct": "0.05", "rebalance_threshold": "0.01",
          "max_turnover_per_rotation": "1.00", "max_position_weight": "0.40", "min_position_weight": "0.10", "min_price": "5.00",
          "min_avg_dollar_volume": "1000000.00", "min_history_sessions": 252}


# ==================================================================================================================================
# fixtures
# ==================================================================================================================================

@pytest.fixture
def lab():
    """A Stage 4.5 lab whose synthetic market has > 252 completed sessions for SYMS + SPY, with fixed closes at T."""
    m = FL.Market(SYMS + ("SPY",), start=date(2025, 6, 2), vol=0.01)
    for s, close in (("AMD", 100), ("MU", 50), ("KO", 60), ("CLS", 40), ("NVDA", 150), ("SNDK", 500), ("SPY", 500)):
        m.set_bar(s, Dt(SESSION), close, close * 1.01, close * 0.99, close, 2_000_000)
    lb = FL.FLab(m)
    lb.market = m
    return lb


def store_for(lab):
    return S.RotationStore(Path(lab.path))


def config_id(st, **over):
    return st.create_config("p3", {**CONFIG, **over}, NOW)["config_id"]


def snap(cash="10000.00", positions=(), source=SN.ROBINHOOD_READ_ONLY, at=None, meta=None, status=SN.OK):
    return SN.normalise(source, at or NOW.isoformat(), cash, positions, meta or {}, status)


def run(lab, st, cid, universe, snapshot, **kw):
    return E.run_rotation(st, cid, universe, snapshot.source if snapshot else kw.pop("source", SN.ROBINHOOD_READ_ONLY), snapshot, now=NOW,
                          path=Path(lab.path), cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object(),
                          conflicts_fn=kw.pop("conflicts_fn", lambda p, s: set()), **kw)


CUSTOM = {"source": "CUSTOM", "symbols": list(SYMS)}


class SpyProvider:
    """Wraps a provider and records every method called on it."""

    def __init__(self, inner):
        self._inner, self.calls = inner, []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr):
            def wrapped(*a, **k):
                self.calls.append(name)
                return attr(*a, **k)
            return wrapped
        return attr


def gateway(cash="1000.00", positions=None, *, status="OK", truncated=False, fetched_at="2026-09-29T11:55:00+00:00", extra_portfolio=None):
    positions = positions if positions is not None else [PF.position("AMD", "10.000000", "90"), PF.position("MU", "2.500000", "40")]
    pos_env = {**PF.env({"account": PF.POSITIONS["account"], "positions": positions}, status), "fetched_at": fetched_at, "truncated": truncated}
    port_env = {**PF.env({**PF.PORTFOLIO, "cash": cash, **(extra_portfolio or {})}, status), "fetched_at": fetched_at}
    http = PF.FakeGatewayHttp({"/positions": (200, pos_env), "/portfolio": (200, port_env)})
    return SpyProvider(PF.fake_provider(http)), http


def alpaca_view(cash="10000.00", positions=None, refreshed_at=None, **over):
    positions = positions if positions is not None else [{"symbol": "AMD", "qty": "10", "side": "long", "market_value": "1234.00"}]
    return {"refreshed_at": refreshed_at or NOW.isoformat(), "last_result": "CONNECTED",
            "account": {"cash": cash, "equity": "999999.00", "buying_power": "5000000.00", "portfolio_value": "999999.00",
                        "status": "ACTIVE", "account_number_masked": "••••7890"},
            "positions": positions, "sections": {n: {"status": "OK"} for n in ("account", "positions", "orders", "fills")}, **over}


# ==================================================================================================================================
# A — snapshot normalisation
# ==================================================================================================================================

def test_1_alpaca_snapshot_normalises(monkeypatch):
    s = SN.load_alpaca_snapshot(lambda: alpaca_view(positions=[{"symbol": "amd", "qty": "10", "side": "long"}, {"symbol": "KO", "qty": "0", "side": "long"}]))
    assert s.source == "ALPACA_PAPER_VIEW" and s.cash == D("10000.00") and s.positions == (("AMD", D("10")),) and s.status == "OK"
    assert s.source_meta["equity"] == "999999.00" and s.source_meta["account_number_masked"] == "••••7890"
    assert s.deterministic_inputs() == {"cash": "10000.00", "positions": [{"symbol": "AMD", "quantity": "10"}]}
    with pytest.raises(SN.SnapshotError) as e:
        SN.from_alpaca_view(alpaca_view(positions=[{"symbol": "AMD", "qty": "-3", "side": "short"}]))
    assert e.value.code == "INPUT_ERROR"                                                     # non-long → INPUT_ERROR
    with pytest.raises(SN.SnapshotError):
        SN.from_alpaca_view({"refreshed_at": None, "account": None, "positions": []})      # no refresh yet
    with pytest.raises(SN.SnapshotError):
        SN.from_alpaca_view(alpaca_view(sections={"account": {"status": "OK"}, "positions": {"status": "FAILED"}}))   # incomplete


def test_2_robinhood_snapshot_normalises():
    provider, http = gateway(positions=[PF.position("nvda", "2.000000", "100"), PF.position("NVDA", "1.000000", "110"),
                                        PF.position("MU", "2.500000", "40"), {**PF.position("KO", "4.000000", "60"), "shares_held_for_sells": "1.000000"}])
    s = SN.load_robinhood_snapshot(lambda: provider)
    assert s.source == "ROBINHOOD_READ_ONLY" and s.cash == D("1000.00") and s.status == "OK" and s.snapshot_at == "2026-09-29T11:55:00+00:00"
    assert s.positions == (("KO", D("4.000000")), ("MU", D("2.500000")), ("NVDA", D("3.000000")))        # upper, summed, fractional exact
    assert s.flags("KO") == ["RH_SHARES_HELD"] and s.flags("MU") == [] and s.source_meta["positions"]["KO"]["held_quantity"] == "1.000000"
    assert s.source_meta["equity_value"] == "995.00" and s.source_meta["gateway_status"] == {"portfolio": "OK", "positions": "OK"}


def test_3_local_simulator_snapshot_normalises(lab):
    T45.account(lab, cash="50000")
    T45.order(lab, "2026-09-24", "AMD", "BUY", 10)
    T45.order(lab, "2026-09-24", "MU", "BUY", 4)
    T45.process(lab, "2026-09-25")
    T45.order(lab, "2026-09-28", "KO", "BUY", 1)                                            # still pending at the snapshot
    s = SN.load_local_simulator_snapshot(Path(lab.path), NOW)
    assert s.source == "LOCAL_SIMULATOR" and s.snapshot_at == NOW.isoformat(timespec="seconds") and [x for x, _ in s.positions] == ["AMD", "MU"]
    assert s.quantities() == {"AMD": D(10), "MU": D(4)} and s.cash < D("50000") and s.source_meta["pending_symbols"] == ["KO"]
    with pytest.raises(SN.SnapshotError):
        SN.load_local_simulator_snapshot(Path(lab.path).with_name("empty.db"), NOW)          # no account → INPUT_ERROR


def test_4_to_10_normalisation_rules():
    s = snap(positions=[("amd", "1.5"), ("AMD", "2"), ("KO", "0"), ("MU", "-3"), ("cls", D("0.000001"))])
    assert s.positions == (("AMD", D("3.5")), ("CLS", D("0.000001")))                       # 4 summed · 5 upper · 6 fractional · 7 <= 0 removed
    for bad in ([("BAD!", "1")], [("AMD", "x")], [("TOOLONGSYM", "1")]):
        with pytest.raises(SN.SnapshotError) as e:
            snap(positions=bad)
        assert e.value.code == "INPUT_ERROR"                                                 # 8 invalid symbol / quantity
    for cash in ("-1", "nan", None, "abc"):
        with pytest.raises(SN.SnapshotError) as e:
            snap(cash=cash)
        assert e.value.code == "INPUT_ERROR"                                                 # 9 negative / invalid cash
    provider, _ = gateway(truncated=True)
    with pytest.raises(SN.SnapshotError) as e:
        SN.load_robinhood_snapshot(lambda: provider)
    assert e.value.code == "INPUT_ERROR" and "truncated" in e.value.message                  # 10 truncated
    with pytest.raises(SN.SnapshotError):
        snap(source="MANUAL")


# ==================================================================================================================================
# B — Robinhood safety
# ==================================================================================================================================

def test_11_12_13_explicit_load_uses_only_get_portfolio_and_get_positions():
    provider, http = gateway()
    SN.load_robinhood_snapshot(lambda: provider)
    assert provider.calls == ["get_portfolio", "get_positions"]                              # 11: exactly these, once each
    assert [r["path"] for r in http.requests] == ["/portfolio", "/positions"]                # 12/13: GET only, no /orders, no write
    assert all(r["params"] == {"account": "holdings"} for r in http.requests)


def test_14_the_run_itself_makes_zero_provider_calls(lab):
    provider, http = gateway()
    s = SN.load_robinhood_snapshot(lambda: provider)
    provider.calls.clear(); http.requests.clear()
    st = store_for(lab)
    out = run(lab, st, config_id(st), CUSTOM, s)
    assert out["run"]["status"] == "VALID" and provider.calls == [] and http.requests == []   # the engine received a loaded snapshot
    assert lab.market.calls and len(lab.market.calls) == 1                                   # exactly one batched market-data fetch


def test_15_gateway_stale_is_recorded_and_the_run_fails_closed(lab):
    provider, _ = gateway(status="STALE")
    s = SN.load_robinhood_snapshot(lambda: provider)
    assert s.status == "STALE" and s.source_meta["gateway_status"]["positions"] == "STALE"
    st = store_for(lab)
    out = run(lab, st, config_id(st), CUSTOM, s)
    assert out["run"]["status"] == "DATA_STALE" and out["run"]["snapshot_status"] == "STALE" and out["candidates"] == []
    assert lab.market.calls == []                                                            # fail closed before any data work


def test_16_provider_unavailable_is_reported_before_any_run():
    from portfolio.provider import UnavailablePortfolioProvider
    with pytest.raises(SN.SnapshotError) as e:
        SN.load_robinhood_snapshot(lambda: UnavailablePortfolioProvider("DISABLED"))
    assert e.value.code == "PORTFOLIO_UNAVAILABLE"


# ==================================================================================================================================
# C / D — metadata and source independence
# ==================================================================================================================================

def test_17_metadata_never_influences_the_proposal(lab):
    st = store_for(lab)
    cid = config_id(st)
    a, _ = gateway(cash="1000.00", positions=[PF.position("AMD", "10.000000", "90"), PF.position("MU", "2.500000", "40")])
    b, _ = gateway(cash="1000.00", positions=[{**PF.position("AMD", "10.000000", "999"), "shares_held_for_sells": "5.000000"},
                                              PF.position("MU", "2.500000", "1")],
                   extra_portfolio={"equity_value": "123456789.00", "total_value": "1.00", "buying_power": "0.00"})
    ra = run(lab, st, cid, CUSTOM, SN.load_robinhood_snapshot(lambda: a))["run"]
    rb = run(lab, st, cid, CUSTOM, SN.load_robinhood_snapshot(lambda: b))["run"]
    assert ra["source_meta_json"] != rb["source_meta_json"]
    for k in ("reference_equity", "current_cash_weight", "target_cash_weight", "turnover", "input_hash", "proposal_hash", "status"):
        assert ra[k] == rb[k], k
    assert ra["reference_equity"] == str(D("1000.00") + D("10") * D("100") + D("2.5") * D("50"))   # cash + qty × close at T = 2125.00
    assert [(t["symbol"], t["target_weight"]) for t in st.targets(ra["run_id"])] == [(t["symbol"], t["target_weight"]) for t in st.targets(rb["run_id"])]
    held_flags = {c["symbol"]: json.loads(c["flags_json"]) for c in st.candidates(rb["run_id"])}
    assert held_flags["AMD"] == ["RH_SHARES_HELD"] and held_flags["MU"] == []                   # display flags differ, hashes do not


def test_18_identical_cash_and_quantities_through_every_source_give_the_same_proposal(lab):
    T45.account(lab, cash="1000")                                                             # LOCAL_SIMULATOR: AMD 10 via a fill
    T45.order(lab, "2026-09-24", "AMD", "BUY", 10)
    T45.process(lab, "2026-09-25")
    local = SN.load_local_simulator_snapshot(Path(lab.path), NOW)
    cash, qty = local.cash, local.quantities()["AMD"]
    provider, _ = gateway(cash=str(cash), positions=[PF.position("AMD", f"{int(qty)}.000000", "90")])
    rh = SN.load_robinhood_snapshot(lambda: provider)
    al = SN.from_alpaca_view(alpaca_view(cash=str(cash), positions=[{"symbol": "AMD", "qty": str(qty), "side": "long"}]))
    assert local.deterministic_inputs() == rh.deterministic_inputs() == al.deterministic_inputs()
    st = store_for(lab)
    cid = config_id(st)
    runs = [run(lab, st, cid, CUSTOM, s)["run"] for s in (local, rh, al)]
    assert len({r["proposal_hash"] for r in runs}) == 1 and len({r["input_hash"] for r in runs}) == 1
    assert {r["portfolio_source"] for r in runs} == set(SN.SOURCES) and {r["status"] for r in runs} == {"VALID"}
    assert runs[1]["source_mismatch_note"] and runs[0]["source_mismatch_note"] is None and runs[2]["source_mismatch_note"] is None


# ==================================================================================================================================
# E — staleness
# ==================================================================================================================================

def test_19_20_21_missing_aged_and_fresh_snapshots(lab):
    st = store_for(lab)
    cid = config_id(st)
    missing = run(lab, st, cid, CUSTOM, None, source=SN.ALPACA_PAPER_VIEW)["run"]
    assert missing["status"] == "INPUT_ERROR" and missing["portfolio_snapshot_at"] is None and missing["n_universe"] == len(SYMS)
    aged = run(lab, st, cid, CUSTOM, snap(at=(NOW - timedelta(minutes=31)).isoformat()))["run"]
    assert aged["status"] == "DATA_STALE" and "31 minutes" in aged["status_detail"]
    fresh = run(lab, st, cid, CUSTOM, snap(at=(NOW - timedelta(minutes=29)).isoformat()))["run"]
    assert fresh["status"] == "VALID"
    assert SN.freshness(snap(at="2026-09-29T11:00:00Z"), NOW, 120) is None                   # Z suffix parses
    assert sorted(r["status"] for r in st.runs()) == ["DATA_STALE", "INPUT_ERROR", "VALID"]      # all three persisted


# ==================================================================================================================================
# F — universe resolution
# ==================================================================================================================================

def test_22_watchlist_resolution(lab):
    u = U.resolve_universe("WATCHLIST", path=Path(lab.path), watchlist_fn=lambda: ["mu", "AMD", "bad!", "AMD"])
    assert u.symbols == ("AMD", "MU") and u.warnings == ({"code": "WATCHLIST_ENTRIES_IGNORED", "count": 1},) and u.ref is None
    for fn in (lambda: [], lambda: (_ for _ in ()).throw(OSError("unreadable"))):             # empty or unreadable: never a crash
        with pytest.raises(U.UniverseError) as e:
            U.resolve_universe("WATCHLIST", path=Path(lab.path), watchlist_fn=fn)
        assert e.value.code == "EMPTY_UNIVERSE"


def test_23_28_saved_scan_resolution_uses_the_stored_snapshot_and_never_rescans(monkeypatch):
    from fit import scanner as SC
    import test_saved_scans_41 as T41
    lab41 = T41.world({T41.S2: {"AMD": 3.0}})                                                 # AMD moved > 2 %: RULES MET on S2
    sid = T41.save(lab41, "CUSTOM", symbols=["MU", "AMD"], name="p3")
    T41.check(lab41, sid, T41.S2)                                                              # the ONE stored scanner snapshot
    calls = []
    monkeypatch.setattr(SC, "scan", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("scanner re-run")))
    u = U.resolve_universe("SAVED_SCAN", sid, path=Path(lab41.path))
    assert u.source == "SAVED_SCAN" and u.ref == sid and u.symbols == ("AMD", "MU") and u.scan_session == T41.S2.isoformat()
    assert u.rules_met("AMD") is True and u.rules_met("MU") is False and calls == []            # 28: no re-scan
    assert u.universe_hash == U.universe_hash("SAVED_SCAN", sid, ["MU", "AMD"])
    with pytest.raises(U.UniverseError) as e:
        U.resolve_universe("SAVED_SCAN", "0" * 32, path=Path(lab41.path))
    assert e.value.code == "UNKNOWN_REF"
    sid2 = T41.save(lab41, "CUSTOM", symbols=["CLS"], name="unchecked")
    with pytest.raises(U.UniverseError) as e:
        U.resolve_universe("SAVED_SCAN", sid2, path=Path(lab41.path))
    assert e.value.code == "NO_SCAN_SNAPSHOT" and calls == []


def test_24_saved_universe_resolution():
    import test_saved_scans_41 as T41
    lab41 = T41.world({}, universe=("MU", "AMD", "CLS"))
    sid = T41.vid(lab41)
    u = U.resolve_universe("SAVED_UNIVERSE", sid, path=Path(lab41.path))
    assert u.source == "SAVED_UNIVERSE" and u.ref == sid and u.symbols == ("AMD", "CLS", "MU") and u.status_map == {}
    assert u.rules_met("AMD") is None                                                          # E10 does not apply
    with pytest.raises(U.UniverseError) as e:
        U.resolve_universe("SAVED_UNIVERSE", "f" * 32, path=Path(lab41.path))
    assert e.value.code == "UNKNOWN_REF"


def test_25_26_27_29_custom_duplicates_cap_and_hash(lab):
    u = U.resolve_universe("CUSTOM", symbols=["mu, AMD", "amd", " ko "], path=Path(lab.path))
    assert u.symbols == ("AMD", "KO", "MU")                                                    # 25 validated · 26 duplicates stable
    with pytest.raises(U.UniverseError) as e:
        U.resolve_universe("CUSTOM", symbols=[f"A{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(101)], path=Path(lab.path))
    assert e.value.code == "TOO_MANY_SYMBOLS" and "truncated" in e.value.message              # 27
    with pytest.raises(U.UniverseError) as e:
        U.resolve_universe("CUSTOM", symbols=["AMD", "bad!"], path=Path(lab.path))
    assert e.value.code == "INVALID_SYMBOLS"
    a = U.resolve_universe("CUSTOM", symbols=["AMD", "MU", "KO"], path=Path(lab.path))
    b = U.resolve_universe("CUSTOM", symbols=["KO", "amd", "MU", "AMD"], path=Path(lab.path))
    assert a.universe_hash == b.universe_hash and len(a.universe_hash) == 64                   # 29 shuffled → same hash
    assert U.universe_hash("WATCHLIST", None, ["AMD"]) != U.universe_hash("CUSTOM", None, ["AMD"])
    for kw, code in (({"source": "HOLDINGS"}, "INVALID_SOURCE"), ({"source": "WATCHLIST", "symbols": ["AMD"]}, "SYMBOLS_NOT_ACCEPTED"),
                     ({"source": "CUSTOM", "ref": "x", "symbols": ["AMD"]}, "REF_NOT_ACCEPTED"), ({"source": "SAVED_SCAN"}, "REF_REQUIRED")):
        with pytest.raises(U.UniverseError) as e:
            U.resolve_universe(path=Path(lab.path), **kw)
        assert e.value.code == code


# ==================================================================================================================================
# G — engine
# ==================================================================================================================================

def test_30_golden_complete_valid_rotation(lab):
    st = store_for(lab)
    cid = config_id(st)
    s = snap(cash="7875.00", positions=[("AMD", "10"), ("MU", "2.5")])                       # equity 7875 + 1000 + 125 = 9000.00
    out = run(lab, st, cid, CUSTOM, s)
    r = out["run"]
    assert r["status"] == "VALID" and r["data_session"] == SESSION and r["benchmark"] == "SPY" and r["n_universe"] == 6
    assert r["reference_equity"] == "9000.00" and r["current_cash_weight"] == "0.875000" and r["target_cash_weight"] == "0.050000"
    assert r["n_eligible"] == 6 and r["n_selected"] == 3 and r["market_data_requests"] == 1
    targets = st.targets(r["run_id"])
    ranks = [t["rank"] for t in targets]
    assert len(targets) == 3 and ranks[0] == 1 and ranks == sorted(ranks) and sum(D(t["target_weight"]) for t in targets) == D("0.950000")
    assert {"AMD", "MU"} <= {t["symbol"] for t in targets}                                    # held, ranked within exit_rank 5: retained
    assert {t["reason"] for t in targets if t["symbol"] in ("AMD", "MU") and t["rank"] > 3} <= {"RETAINED_RANK_BUFFER"}
    assert set(D(t["target_weight"]) for t in targets) <= {D("0.316666"), D("0.316668")}
    for t in targets:
        assert t["target_notional"] == str(_q2(D(t["target_weight"]) * D("9000.00"))) and t["est_target_qty"] >= 1
    items = st.items(r["run_id"])
    assert {i["symbol"] for i in items} == set(t["symbol"] for t in targets) | {"AMD", "MU"}
    assert all(i["action"] in R.ACTIONS for i in items) and any(i["handoff_allowed"] for i in items)
    cands = st.candidates(r["run_id"])
    assert [c["rank"] for c in cands] == [1, 2, 3, 4, 5, 6] and all(json.loads(c["scores_json"]) for c in cands)
    assert st.verify_run(r["run_id"])["ok"] is True
    again = run(lab, st, cid, CUSTOM, s)["run"]                                               # determinism: identical hashes
    assert (again["input_hash"], again["proposal_hash"]) == (r["input_hash"], r["proposal_hash"])


def _q2(d):
    return d.quantize(D("0.01"))


def test_31_no_eligible_candidates(lab):
    st = store_for(lab)
    cid = config_id(st, min_avg_dollar_volume="999999999999.00")                              # nothing is liquid enough
    out = run(lab, st, cid, CUSTOM, snap())
    assert out["run"]["status"] == "NO_ELIGIBLE_CANDIDATES" and out["run"]["n_eligible"] == 0 and out["targets"] == [] and out["items"] == []
    assert all("LOW_LIQUIDITY" in c["reasons"] for c in out["candidates"]) and out["run"]["reference_equity"] == "10000.00"
    assert st.verify_run(out["run"]["run_id"])["ok"]


def test_32_insufficient_candidates(lab):
    st = store_for(lab)
    cid = config_id(st, excluded_symbols=["AMD", "MU", "KO", "CLS"])                          # only 2 of 3 slots can be filled
    out = run(lab, st, cid, CUSTOM, snap())
    r = out["run"]
    assert r["status"] == "INSUFFICIENT_CANDIDATES" and r["n_eligible"] == 2 and r["n_selected"] == 2
    assert all(i["handoff_allowed"] == 0 for i in out["items"]) and D(r["target_cash_weight"]) == D(1) - 2 * D("0.316666")
    assert {c["symbol"] for c in out["candidates"] if "EXCLUDED" in c["reasons"]} == {"AMD", "MU", "KO", "CLS"}


def test_33_turnover_limit_exceeded(lab):
    st = store_for(lab)
    cid = config_id(st, max_turnover_per_rotation="0.50")
    out = run(lab, st, cid, CUSTOM, snap())                                                    # all cash → 95 % invested = 0.95 turnover
    assert out["run"]["status"] == "TURNOVER_LIMIT_EXCEEDED" and out["run"]["turnover"] == "0.950000"
    assert out["targets"] and all(i["handoff_allowed"] == 0 for i in out["items"])


def test_34_rank_buffer_through_the_engine(lab):
    st = store_for(lab)
    cid = config_id(st, portfolio_size=2, exit_rank=4, min_position_weight="0.10", max_position_weight="0.50")
    first = run(lab, st, cid, CUSTOM, snap())
    ranked = [c["symbol"] for c in first["candidates"] if c["eligible"]]
    ranked.sort(key=lambda s: next(c["rank"] for c in first["candidates"] if c["symbol"] == s))
    hold_rank4, hold_rank6 = ranked[3], ranked[5]
    out = run(lab, st, cid, CUSTOM, snap(cash="100.00", positions=[(hold_rank4, "1"), (hold_rank6, "1")]))
    by = {i["symbol"]: i for i in out["items"]}
    assert by[hold_rank4]["action"] != "EXIT" and hold_rank4 in {t["symbol"] for t in out["targets"]}            # rank 4 ≤ exit_rank: retained
    assert by[hold_rank6]["action"] == "EXIT" and by[hold_rank6]["reason"] == "RANK_ABOVE_EXIT_RANK"              # rank 6 > exit_rank: exits
    assert next(t for t in out["targets"] if t["symbol"] == hold_rank4)["reason"] == "RETAINED_RANK_BUFFER"
    assert len(out["targets"]) == 2 and out["targets"][0]["symbol"] == ranked[0]


def test_35_eligibility_rules_through_orchestration(lab, monkeypatch):
    st = store_for(lab)
    lab.market.rows["CLS"] = {d: r for d, r in lab.market.rows["CLS"].items() if d >= Dt("2026-06-01")}   # E2 short history
    lab.market.drop("KO", Dt(SESSION))                                                                           # E6 no bar at T
    for d, r in lab.market.rows["SNDK"].items():                                                                 # E4 below min price
        lab.market.rows["SNDK"][d] = (r[0], r[1], 1.0, 1.1, 0.9, 1.0, r[6])
    cid = config_id(st, excluded_symbols=["NVDA"], min_avg_dollar_volume="5000000.00")       # synthetic volume ~1.4 M shares
    out = run(lab, st, cid, CUSTOM, snap(), conflicts_fn=lambda p, s: {"MU"})
    reasons = {c["symbol"]: c["reasons"] for c in out["candidates"]}
    assert reasons["AMD"] == [] and "INSUFFICIENT_HISTORY" in reasons["CLS"] and reasons["KO"] == ["DATA_STALE"]
    assert "BELOW_MIN_PRICE" in reasons["SNDK"] and "LOW_LIQUIDITY" in reasons["SNDK"]                           # E4 + E5
    assert reasons["NVDA"] == ["EXCLUDED"] and reasons["MU"] == ["CONFLICTING_STATE"]                           # E8 + E9
    assert out["run"]["n_eligible"] == 1 and out["run"]["status"] == "INSUFFICIENT_CANDIDATES"
    lab.market.rows["ZZZZ"] = {}
    out2 = run(lab, st, cid, {"source": "CUSTOM", "symbols": ["AMD", "ZZZZ"]}, snap())
    assert {c["symbol"]: c["reasons"] for c in out2["candidates"]}["ZZZZ"] == ["NO_REFERENCE_PRICE"]            # E3: no bars at all
    # E10 through the pure engine: a SAVED_SCAN universe with stored statuses
    from backtest import bars as B
    from fit import readonly as RO
    series, _, _ = FC.load_bars(RO.ReadOnlyBacktestStore(Path(lab.path)), ["AMD", "MU", "SPY"], Dt(SESSION) - timedelta(days=420), Dt(SESSION),
                                NOW, FC.BarCache(), fetch_fn=lab.market.fetch, client=object())
    scan_u = U.ResolvedUniverse("SAVED_SCAN", "a" * 32, ("AMD", "MU"), U.universe_hash("SAVED_SCAN", "a" * 32, ["AMD", "MU"]),
                                {"AMD": FC.RULES_MET, "MU": FC.RULES_NOT_MET}, SESSION, ())
    res = E.compute_rotation(st.config(cid), scan_u, series, snap(), Dt(SESSION), now=NOW)
    assert {c["symbol"]: c["reasons"] for c in res.candidates} == {"AMD": [], "MU": ["SCANNER_NOT_MET"]} and B.last_complete_session_date(NOW) == Dt(SESSION)


def test_35b_e9_default_conflicts_read_the_existing_stores(lab):
    T45.account(lab, cash="1000")
    T45.order(lab, "2026-09-28", "KO", "BUY", 1)                                               # a pending Stage 4.5 order
    assert E.default_conflicts(Path(lab.path), "LOCAL_SIMULATOR") == {"KO"}
    assert E.default_conflicts(Path(lab.path), "ROBINHOOD_READ_ONLY") == set()                 # no Robinhood inference
    src = (ROOT / "rotation" / "engine.py").read_text(encoding="utf-8")
    assert "get_orders" not in src and "in_states(RU.UNRESOLVED)" in src


def test_36_37_benchmark_missing_or_stale(lab):
    st = store_for(lab)
    cid = config_id(st)
    lab.market.rows["SPY"] = {}
    out = run(lab, st, cid, CUSTOM, snap())
    assert out["run"]["status"] == "INPUT_ERROR" and "SPY" in out["run"]["status_detail"]        # 36
    m = FL.Market(SYMS + ("SPY",), start=date(2025, 6, 2), vol=0.01)
    m.drop("SPY", Dt(SESSION))                                                                  # SPY has no bar for T while others do
    lab.market = m
    out = run(lab, st, cid, CUSTOM, snap())
    assert out["run"]["status"] == "DATA_STALE" and "could not be confirmed" in out["run"]["status_detail"]   # 37


def test_38_held_symbol_without_reference_price(lab):
    st = store_for(lab)
    cid = config_id(st)
    out = run(lab, st, cid, CUSTOM, snap(positions=[("ORCL", "3")]))                           # held, not in the market at all
    assert out["run"]["status"] == "INPUT_ERROR" and "ORCL" in out["run"]["status_detail"] and out["run"]["reference_equity"] is None
    assert out["candidates"] == [] and out["run"]["n_universe"] == len(SYMS)


def test_39_40_reference_equity_exact_and_broker_equity_ignored(lab):
    st = store_for(lab)
    cid = config_id(st)
    view = alpaca_view(cash="1000.00", positions=[{"symbol": "AMD", "qty": "10", "side": "long", "market_value": "999999"}])
    view["account"]["equity"] = "123456789.00"
    s = SN.from_alpaca_view(view)
    out = run(lab, st, cid, CUSTOM, s)
    assert out["run"]["reference_equity"] == "2000.00"                                           # 1000 + 10 × 100 (close at T)
    items = {i["symbol"]: i for i in out["items"]}
    assert items["AMD"]["current_weight"] == "0.500000" and out["run"]["current_cash_weight"] == "0.500000"
    assert json.loads(out["run"]["source_meta_json"] if isinstance(out["run"]["source_meta_json"], str) else json.dumps(out["run"]["source_meta_json"]))["equity"] == "123456789.00"


# ==================================================================================================================================
# H / I — network and persistence
# ==================================================================================================================================

def test_41_complete_run_is_offline_except_one_market_data_fetch(lab):
    import conftest
    provider, http = gateway()
    s = SN.load_robinhood_snapshot(lambda: provider)
    st = store_for(lab)
    out = run(lab, st, config_id(st), CUSTOM, s)
    assert out["run"]["status"] == "VALID" and len(lab.market.calls) == 1 and out["run"]["market_data_requests"] == 1
    assert provider.calls == ["get_portfolio", "get_positions"] and len(http.requests) == 2    # only the explicit load
    assert conftest._ALPACA_ORDER_WIRE_HITS == []                                              # no Alpaca wire was even requested
    cache = FC.BarCache()
    cid = config_id(st)
    out2 = E.run_rotation(st, cid, CUSTOM, s.source, s, now=NOW, path=Path(lab.path), cache=cache, fetch_fn=lab.market.fetch,
                          client=object(), conflicts_fn=lambda p, q: set())
    out3 = E.run_rotation(st, cid, CUSTOM, s.source, s, now=NOW, path=Path(lab.path), cache=cache, fetch_fn=lab.market.fetch,
                          client=object(), conflicts_fn=lambda p, q: set())
    assert len(lab.market.calls) == 2 and out2["run"]["market_data_requests"] == 1 and out3["run"]["market_data_requests"] == 0   # cached: 0
    assert out2["run"]["proposal_hash"] == out3["run"]["proposal_hash"] == out["run"]["proposal_hash"]


def test_42_43_44_persistence_matches_the_engine_result(lab):
    st = store_for(lab)
    cid = config_id(st)
    out = run(lab, st, cid, CUSTOM, snap(cash="7875.00", positions=[("AMD", "10"), ("MU", "2.5")]))
    rid = out["run"]["run_id"]
    stored = st.run(rid)
    for k in ("status", "input_hash", "proposal_hash", "n_universe", "n_eligible", "n_selected", "data_session", "universe_hash"):
        assert stored[k] == out["run"][k], k
    assert stored["turnover"] == S.dec_str(out["run"]["turnover"]) and stored["reference_equity"] == S.dec_str(out["run"]["reference_equity"])
    assert [t["symbol"] for t in st.targets(rid)] == [t["symbol"] for t in out["targets"]]
    assert {(i["symbol"], i["action"], i["est_qty_diff"]) for i in st.items(rid)} == {(i["symbol"], i["action"], i["est_qty_diff"]) for i in out["items"]}
    assert len(st.candidates(rid)) == len(out["candidates"]) and st.verify_run(rid)["ok"]      # 42 / 44
    failed = run(lab, st, cid, CUSTOM, snap(at=(NOW - timedelta(hours=2)).isoformat()))["run"]
    assert st.run(failed["run_id"])["status"] == "DATA_STALE" and st.verify_run(failed["run_id"])["ok"]   # 43
    dry = run(lab, st, cid, CUSTOM, snap(), persist=False)
    assert "run_id" not in dry["run"] and len(st.runs()) == 2


def test_pre_run_input_errors_are_not_persisted(lab):
    st = store_for(lab)
    cid = config_id(st)
    with pytest.raises(E.EngineError) as e:
        run(lab, st, "0" * 32, CUSTOM, snap())
    assert e.value.code == "INVALID_CONFIG"
    with pytest.raises(E.EngineError) as e:
        E.run_rotation(st, cid, CUSTOM, SN.ALPACA_PAPER_VIEW, snap(), now=NOW, path=Path(lab.path))   # a Robinhood snapshot
    assert e.value.code == "SNAPSHOT_SOURCE_MISMATCH"
    with pytest.raises(E.EngineError) as e:
        E.run_rotation(st, cid, CUSTOM, "MANUAL", None, now=NOW, path=Path(lab.path))
    assert e.value.code == "INVALID_PORTFOLIO_SOURCE"
    with pytest.raises(U.UniverseError):
        run(lab, st, cid, {"source": "CUSTOM", "symbols": ["bad!"]}, snap())
    assert st.runs() == []


# ==================================================================================================================================
# J — static safety
# ==================================================================================================================================

def test_static_phase3_modules_stay_inside_the_approved_boundaries():
    for f in ("rotation/snapshots.py", "rotation/universe.py", "rotation/engine.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"^\s*(from|import)\s+(requests|socket|urllib|http|alpaca\b|anthropic|agents|rh_gateway|portfolio\.provider|"
                             r"portfolio\.models|portfolio\b|random|subprocess|threading)\b", code, re.M), f
        assert not re.search(r"alpaca_order_writer|alpaca_order_reads|alpaca_orders\b|TradingClient|/v2/|place_?order|submit_?order|"
                             r"cancel_order|replace_order|get_orders|get_quotes|get_tax_lots|get_realized|get_pnl|get_equity_|"
                             r"call_tool|ClientSession|os\.environ|getenv|\.env\b|eval\(|exec\(|__import__|importlib|float\(|"
                             r"latest_quote|LatestQuote|get_stock_latest|fetch_daily_bars", code), f
    snapshots = (ROOT / "rotation" / "snapshots.py").read_text(encoding="utf-8")
    assert snapshots.count("pr.provider_factory") == 1                                       # the one approved extension point
    assert snapshots.count("provider.get_portfolio()") == 1 and snapshots.count("provider.get_positions()") == 1   # the only two reads
    assert not re.search(r"provider\.(?!get_portfolio\(\)|get_positions\(\))\w+\(", snapshots)                    # no other provider call
    assert "from api.routes import portfolio as pr" in snapshots and "from paper import alpaca_view as APV" in snapshots
    assert "APV.view" in snapshots and "refresh" not in snapshots.split("def load_alpaca_snapshot")[1].split("def ")[0]
    engine = (ROOT / "rotation" / "engine.py").read_text(encoding="utf-8")
    assert "load_robinhood_snapshot" not in engine and "load_alpaca_snapshot" not in engine and "provider_factory" not in engine
    assert "FC.load_bars(" in engine and "resolve_session(" in engine and "bars_content_hash" in engine
    # the ONE approved prior-stage exemption (2026-10-06): exactly this line, for exactly this file — the regression test's
    # allowlist must not grow for any other Stage 4.7 module (the Phase 4 route included)
    iso = (ROOT / "tests" / "test_regression_isolation.py").read_text(encoding="utf-8")
    line = ('Path("rotation/snapshots.py"),  # Stage 4.7 explicit read-only Robinhood snapshot: provider_factory, '
            'get_portfolio/get_positions only')
    assert iso.count(line) == 1 and iso.count("rotation/") == 1 and "portfolio_rotation" not in iso
    importers = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "rotation").glob("*.py")
                       if re.search(r"^\s*(from portfolio\b|import portfolio\b|from api\.routes import .*portfolio)",
                                    p.read_text(encoding="utf-8"), re.M))
    assert importers == ["rotation/snapshots.py"]                                           # no other Stage 4.7 module reaches it
