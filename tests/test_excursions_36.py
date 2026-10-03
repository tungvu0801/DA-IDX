"""Stage 3.6: forward MFE / MAE of reference cycles — observed going forward only (never backfilled), Stage 3.2
conventions (entry session's full range, held sessions' full ranges, the exit session's OPEN only, clamps), Stage 3.3
price basis, append-only evidence, legacy cycles untouched, Evidence integration. No network, no AI, no broker."""
import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import config
import fw_fixtures as FL
from backtest import runs as R
from comparison import view as V
from database.excursion_migrations import run_excursion_migrations
from database.forward_migrations import run_forward_migrations
from fit import readonly as RO
from forward import excursions as XC
from forward import journal as J
from forward.store import ForwardStore
from strategy import spec as S

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
QUIET = {"vol": 0.002}
UP3 = {"logic": "ALL", "conditions": [{"feature": "stock.change_1d_pct", "op": ">", "value": 3}]}
NEVER = {"logic": "ALL", "conditions": [{"feature": "stock.close", "op": "<", "value": 0}]}


def hold(n):
    return {"logic": "ANY", "conditions": [], "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 40},
            "target": {"method": "PCT_ABOVE_ENTRY", "pct": 60}, "max_holding_days": n}


def fspec(entry=None, exit_=None, symbols=("AMD",), name="Excursion test"):
    s = X.spec(symbols=symbols, entry=entry or UP3, exit_=exit_ or hold(2),
               risk={"max_position_pct": 10, "max_open_positions": 50}, name=name)
    n, err = S.validate(s)
    assert not err, err
    return n


def jump(m, sym, d, pct=5.0):
    pc = m.rows[sym][m.days[m.days.index(d) - 1]][5]
    m.set_bar(sym, d, pc, pc * (1 + pct / 100) * 1.001, pc * 0.999, pc * (1 + pct / 100))


def market(bars, sym="AMD", signal="2026-09-28", extra=("SPY",)):
    """ENTER at the `signal` close (a +5 % day), then explicit bars {date: (open, high, low, close)}."""
    m = FL.Market((sym,) + tuple(extra), **QUIET)
    jump(m, sym, D(signal))
    for d, (o, h, low, c) in bars.items():
        m.set_bar(sym, D(d), o, h, low, c)
    return m


def run(lab, days, spec=None, created="2026-09-25"):
    jid = lab.journal(spec or fspec(), FL.ny(D(created), 12))["journal_id"]
    for d in days:
        lab.record(jid, FL.at(D(d)))
    return jid


def xrows(lab, jid):
    return lab.fs.excursions(jid)


def cycles(lab, jid):
    return J.journal_view(lab.fs, jid)["excursions"]["cycles"]


def raw(lab):
    c = sqlite3.connect(lab.path)
    c.execute("PRAGMA foreign_keys = ON")
    return c


class LegacyLab(FL.FLab):
    """A database from before Stage 3.6: the Stage 3.3 journal tables only (no excursion tables), used through a store
    that does not migrate — exactly what earlier captures stored. `upgrade()` then opens the normal store, which runs the
    additive Stage 3.6 migration (as the first open after the update would)."""

    def __init__(self, m):
        X.Lab.__init__(self)
        with sqlite3.connect(self.path) as c:
            run_forward_migrations(c)
        self.fs = ForwardStore.__new__(ForwardStore)
        self.fs.db_path = Path(self.path)
        self.market, self.research, self.events = m, FL.ResearchDB(), FL.Events()

    def upgrade(self):
        self.fs = ForwardStore(self.path)


# ---- 34 / 35 / 36 / 37 / 38: semantics (Stage 3.2 conventions) --------------------------------------------------------------

def test_34_entry_session_full_range_counts():
    lab = FL.FLab(market({"2026-09-29": (100, 106, 97, 101)}))
    jid = run(lab, ("2026-09-28", "2026-09-29"))
    (r,) = xrows(lab, jid)
    assert (r["observation_type"], r["session_date"], r["entry_reference_price"]) == ("ENTRY_SESSION", "2026-09-29", 100.0)
    assert r["cumulative_mfe_pct"] == pytest.approx(6.0) and r["cumulative_mae_pct"] == pytest.approx(-3.0)
    assert (r["tracking_status"], r["status"], r["price_basis"]) == ("TRACKING", "OBSERVED", "THIS_CAPTURE")


def test_35_held_sessions_accumulate():
    lab = FL.FLab(market({"2026-09-29": (100, 106, 97, 101), "2026-09-30": (101, 110, 99, 104)}, ))
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30"), spec=fspec(exit_=hold(5)))
    r = xrows(lab, jid)[-1]
    assert r["observation_type"] == "HELD_SESSION" and r["session_high_pct"] == pytest.approx(10.0)
    assert r["cumulative_mfe_pct"] == pytest.approx(10.0) and r["cumulative_mae_pct"] == pytest.approx(-3.0)
    (c,) = cycles(lab, jid)
    assert c["status"] == "TRACKING" and c["final"] is False and c["completed"] is False


def test_36_exit_day_uses_only_the_open():
    lab = FL.FLab(market({"2026-09-29": (100, 108, 96, 101), "2026-09-30": (101, 104, 98, 102),
                          "2026-10-01": (105, 130, 80, 110)}))
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    rows = xrows(lab, jid)
    assert [r["observation_type"] for r in rows] == ["ENTRY_SESSION", "HELD_SESSION", "EXIT_OPEN"]
    ex = rows[-1]
    assert (ex["observed_open"], ex["observed_high"], ex["observed_low"]) == (105.0, None, None)       # 130 / 80 never read
    assert ex["session_high_pct"] == ex["session_low_pct"] == pytest.approx(5.0)
    assert ex["cumulative_mfe_pct"] == pytest.approx(8.0) and ex["cumulative_mae_pct"] == pytest.approx(-4.0)
    assert ex["tracking_status"] == "COMPLETE"
    (c,) = cycles(lab, jid)
    assert c["final"] and c["reference_move_pct"] == pytest.approx(5.0) and c["mfe_pct"] == pytest.approx(8.0)


def test_37_exit_gap_open_extends_the_excursion():
    lab = FL.FLab(market({"2026-09-29": (100, 105, 99, 101), "2026-09-30": (101, 104, 99.5, 102),
                          "2026-10-01": (112, 115, 111, 114)}))
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    ex = xrows(lab, jid)[-1]
    assert ex["observation_type"] == "EXIT_OPEN"
    assert ex["cumulative_mfe_pct"] == pytest.approx(12.0) and ex["cumulative_mae_pct"] == pytest.approx(-1.0)
    fill = next(f for f in lab.fs.fills(jid) if f["fill_type"] == "EXIT")
    assert ex["session_high_pct"] == fill["reference_move_pct"]                  # the same arithmetic as the stored move


def test_38_clamps_mfe_never_below_zero_mae_never_above_zero():
    lab = FL.FLab(market({"2026-09-29": (100, 100, 95, 96), "2026-09-30": (96, 99, 94, 95), "2026-10-01": (97, 99, 90, 98)}))
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    ex = xrows(lab, jid)[-1]
    assert ex["cumulative_mfe_pct"] == 0.0 and ex["cumulative_mae_pct"] == pytest.approx(-6.0)
    up = FL.FLab(market({"2026-09-29": (100, 110, 100, 108), "2026-09-30": (108, 112, 104, 111), "2026-10-01": (111, 113, 90, 99)}))
    j2 = run(up, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    assert xrows(up, j2)[-1]["cumulative_mae_pct"] == 0.0 and xrows(up, j2)[-1]["cumulative_mfe_pct"] == pytest.approx(12.0)
    for lb, j in ((lab, jid), (up, j2)):
        assert all(r["cumulative_mfe_pct"] >= 0 and r["cumulative_mae_pct"] <= 0 for r in xrows(lb, j))
    with raw(lab) as c, pytest.raises(sqlite3.IntegrityError):                  # the database refuses a wrong sign
        c.execute("UPDATE forward_test_excursions SET cumulative_mfe_pct = -1")


# ---- 39 / 40 / 46 / 3: legacy cycles, new cycles, mixed journals, NO BACKFILL ---------------------------------------------------

def _two_cycle_market():
    """Every bar explicit. Cycle 1: ENTER Sep 28, entry 100 (Sep 29), exit open 105 (Oct 1). Cycle 2: ENTER Oct 5, entry
    108 (Oct 6), held Oct 7, exit open 112 (Oct 8). No other session moves more than 3 %, so no other cycle exists."""
    m = market({"2026-09-29": (100, 106, 97, 101), "2026-09-30": (101, 104, 98, 102), "2026-10-01": (105, 108, 101, 102.5),
                "2026-10-02": (102.5, 103, 101.5, 102.8), "2026-10-05": (102.8, 108, 102.7, 107.94),
                "2026-10-06": (108, 116.1, 105.84, 110), "2026-10-07": (110, 112, 107, 111), "2026-10-08": (112, 113, 111, 112.5)})
    return m


def test_39_legacy_cycle_is_never_tracked_or_backfilled():
    lab = LegacyLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))          # a complete cycle BEFORE Stage 3.6
    lab.upgrade()
    assert lab.fs.excursions(jid) == [] and lab.fs.excursion_tracking(jid) is None
    (c,) = cycles(lab, jid)
    assert c["status"] == "LEGACY_NOT_TRACKED" and c["mfe_pct"] is None and c["mae_pct"] is None and c["completed"]
    v = V.view(lab.fs.journal(jid)["strategy_version_id"], path=lab.path)["forward"]
    assert v["mfe_mae"] == {"tracked": False, "text": "Not tracked in Stage 3.3"}          # the Stage 3.5 view, unchanged
    assert "excursion_metrics" not in v and not any("excursion" in c for c in v["cycles"])
    lab.record(jid, FL.at(D("2026-10-02")))                                              # first capture with Stage 3.6
    assert lab.fs.excursion_tracking(jid)["activated_in_session"] == "2026-10-02" and lab.fs.excursions(jid) == []
    with raw(lab) as conn:                                                               # the DATABASE refuses a backfill
        with pytest.raises(sqlite3.IntegrityError, match="no backfill|latest|legacy"):
            conn.execute("INSERT INTO forward_test_excursions (journal_id, symbol, cycle_no, session_date, observation_type, "
                         "status, tracking_status, entry_session_date, entry_reference_price, basis_entry_open, "
                         "session_high_pct, session_low_pct, cumulative_mfe_pct, cumulative_mae_pct, dataset_id, "
                         "engine_version, captured_at) VALUES (?, 'AMD', 1, '2026-09-29', 'ENTRY_SESSION', 'OBSERVED', "
                         "'TRACKING', '2026-09-29', 100, 100, 6, -3, 6, -3, 'x', '3.6.0', 'x')", (jid,))
        with pytest.raises(sqlite3.IntegrityError, match="no backfill|legacy|tracked"):
            conn.execute("INSERT INTO forward_test_excursions (journal_id, symbol, cycle_no, session_date, observation_type, "
                         "status, tracking_status, entry_session_date, entry_reference_price, cumulative_mfe_pct, "
                         "cumulative_mae_pct, engine_version, captured_at) VALUES (?, 'AMD', 1, '2026-10-02', 'HELD_SESSION', "
                         "'EXCURSION_DATA_UNAVAILABLE', 'TRACKING', '2026-09-29', 100, 0, 0, '3.6.0', 'x')", (jid,))


def test_40_new_cycle_is_tracked_append_only_to_its_final_value():
    lab = FL.FLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30"))
    before = xrows(lab, jid)
    assert [r["observation_type"] for r in before] == ["ENTRY_SESSION", "HELD_SESSION"]
    lab.record(jid, FL.at(D("2026-10-01")))
    after = xrows(lab, jid)
    assert after[:2] == before and after[2]["observation_type"] == "EXIT_OPEN"            # appended; nothing rewritten
    (c,) = cycles(lab, jid)
    assert (c["status"], c["final"]) == ("COMPLETE", True)
    assert (c["mfe_pct"], c["mae_pct"]) == (after[-1]["cumulative_mfe_pct"], after[-1]["cumulative_mae_pct"])
    with raw(lab) as conn:
        for sql in ("UPDATE forward_test_excursions SET cumulative_mae_pct = 0", "DELETE FROM forward_test_excursions",
                    "UPDATE forward_test_excursion_tracking SET activated_in_session = '2026-01-01'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(sql)


def test_46_mixed_legacy_and_tracked_cycles_average_only_the_tracked():
    lab = LegacyLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))            # cycle 1: legacy
    lab.upgrade()
    for d in ("2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"):      # cycle 2: tracked
        lab.record(jid, FL.at(D(d)))
    cs = {c["cycle_no"]: c for c in cycles(lab, jid)}
    assert cs[1]["status"] == "LEGACY_NOT_TRACKED" and cs[2]["status"] == "COMPLETE"
    m = J.journal_view(lab.fs, jid)["excursions"]["metrics"]
    assert (m["completed_cycles"], m["tracked_completed_cycles"], m["legacy_untracked_completed_cycles"]) == (2, 1, 1)
    assert m["average_mfe_pct"] == cs[2]["mfe_pct"] and m["average_mae_pct"] == cs[2]["mae_pct"]     # never averaged as 0
    assert cs[2]["mfe_pct"] == pytest.approx(7.5) and cs[2]["mae_pct"] == pytest.approx(-2.0)
    f = V.view(lab.fs.journal(jid)["strategy_version_id"], path=lab.path)["forward"]
    assert f["excursion_metrics"]["tracked_completed_cycles"] == 1 and f["completed_cycles"] == 2
    assert f["excursion_metrics"]["legacy_note"] == "1 earlier completed cycle was not MFE / MAE tracked."
    assert f["mfe_mae"]["text"] == "MFE / MAE sample: 1 tracked cycle of 2 completed cycles"
    assert {c["cycle_no"]: c["excursion"]["status"] for c in f["cycles"]} == {1: "LEGACY_NOT_TRACKED", 2: "COMPLETE"}


def test_46b_four_legacy_and_three_tracked_completed_cycles():
    m = FL.Market(("AMD", "SPY"), **QUIET)
    signals = ["2026-09-22", "2026-09-24", "2026-09-28", "2026-09-30", "2026-10-02", "2026-10-06", "2026-10-08"]
    for d in signals:
        jump(m, "AMD", D(d))
    lab = LegacyLab(m)
    sp = fspec(exit_=hold(1))                                     # enter at a close, fill next open, exit signal same day
    jid = lab.journal(sp, FL.ny(D("2026-09-18"), 12))["journal_id"]
    days = [d for d in m.days if D("2026-09-21") <= d <= D("2026-10-13")]
    for d in days:
        if d == D("2026-10-02"):
            lab.upgrade()                                         # the Stage 3.6 update happens between two captures
        lab.record(jid, FL.at(d))
    mt = J.journal_view(lab.fs, jid)["excursions"]["metrics"]
    cs = cycles(lab, jid)
    tracked = [c for c in cs if c["status"] == "COMPLETE"]
    assert (mt["completed_cycles"], mt["tracked_completed_cycles"], mt["legacy_untracked_completed_cycles"]) == (7, 3, 4)
    assert mt["average_mfe_pct"] == pytest.approx(sum(c["mfe_pct"] for c in tracked) / 3)
    ev = V.view(lab.fs.journal(jid)["strategy_version_id"], path=lab.path)
    assert ev["forward"]["mfe_mae"]["text"] == "MFE / MAE sample: 3 tracked cycles of 7 completed cycles"
    assert ev["forward"]["excursion_metrics"]["legacy_note"] == "4 earlier completed cycles were not MFE / MAE tracked."


# ---- 41 / 45 / 15 / 28: continuity gaps and open cycles ------------------------------------------------------------------------

def test_41_missed_session_makes_tracking_incomplete_without_reconstruction():
    m = market({"2026-09-29": (100, 106, 97, 101), "2026-09-30": (101, 140, 60, 102), "2026-10-01": (102, 104, 100, 103)})
    lab = FL.FLab(m)
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-10-01"), spec=fspec(exit_=hold(5)))    # Sep 30 missed while OPEN
    o = lab.obs(jid, D("2026-10-01"))["AMD"]
    assert (o["state_before"], o["state_after"], o["reason_code"]) == ("OPEN", "CONTINUITY_BLOCKED", "FORWARD_CONTINUITY_GAP")
    assert lab.fs.journal(jid)["status"] == "CONTINUITY_BLOCKED"                            # Stage 3.3 behaviour unchanged
    rows = xrows(lab, jid)
    assert [(r["session_date"], r["observation_type"]) for r in rows] == [("2026-09-29", "ENTRY_SESSION"), ("2026-10-01", "CONTINUITY_GAP")]
    assert rows[-1]["tracking_status"] == "INCOMPLETE_DUE_TO_CONTINUITY_GAP" and rows[-1]["cumulative_mfe_pct"] == pytest.approx(6.0)
    assert not any(r["session_date"] == "2026-09-30" for r in rows)                           # 140 / 60 never read
    lab.record(jid, FL.at(D("2026-10-02")))
    assert len(xrows(lab, jid)) == 2                                                          # nothing after the gap
    (c,) = cycles(lab, jid)
    assert c["status"] == "INCOMPLETE_DUE_TO_CONTINUITY_GAP" and c["final"] is False
    assert J.journal_view(lab.fs, jid)["excursions"]["metrics"]["tracked_completed_cycles"] == 0
    f = V.view(lab.fs.journal(jid)["strategy_version_id"], path=lab.path)["forward"]
    assert f["cycles"][0]["excursion"]["status"] == "INCOMPLETE_DUE_TO_CONTINUITY_GAP"
    assert f["excursion_metrics"]["average_mfe_pct"] is None


def test_45_open_cycle_is_visible_but_excluded_from_averages():
    lab = FL.FLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"))
    cs = {c["cycle_no"]: c for c in cycles(lab, jid)}
    assert cs[1]["status"] == "COMPLETE" and cs[2]["status"] == "TRACKING"
    assert cs[2]["mfe_pct"] == pytest.approx(7.5) and cs[2]["mae_pct"] == pytest.approx(-2.0)      # current, not final
    m = J.journal_view(lab.fs, jid)["excursions"]["metrics"]
    assert m["tracked_completed_cycles"] == 1 and m["open_tracked_cycles"] == 1 and m["average_mfe_pct"] == cs[1]["mfe_pct"]
    sess = J.session_view(lab.fs, jid, "2026-10-06")
    assert [(x["observation_type"], x["cycle_no"]) for x in sess["excursions"]] == [("ENTRY_SESSION", 2)]


def test_missing_bar_is_unavailable_not_guessed_and_matches_stage32():
    m = market({"2026-09-29": (100, 106, 97, 101), "2026-10-01": (103, 109, 101, 104), "2026-10-02": (104, 105, 103, 104)})
    m.drop("AMD", D("2026-09-30"))                                           # the symbol has no bar that session
    lab = FL.FLab(m)
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"))
    rows = xrows(lab, jid)
    gap = next(r for r in rows if r["session_date"] == "2026-09-30")
    assert (gap["status"], gap["reason_code"], gap["tracking_status"]) == ("EXCURSION_DATA_UNAVAILABLE", "NO_BAR_FOR_SESSION", "TRACKING")
    assert gap["observed_high"] is None and gap["cumulative_mfe_pct"] == pytest.approx(6.0)     # carried, nothing guessed
    (c,) = cycles(lab, jid)
    assert c["status"] == "COMPLETE" and c["sessions_without_bar"] == 1 and c["mfe_pct"] == pytest.approx(9.0)


def test_missing_price_basis_ends_tracking_as_incomplete():
    lab = FL.FLab(market({"2026-09-29": (100, 106, 97, 101), "2026-09-30": (101, 104, 98, 102)}))
    jid = run(lab, ("2026-09-28", "2026-09-29"), spec=fspec(exit_=hold(5)))
    prev = lab.obs(jid, D("2026-09-29"))["AMD"]
    last = XC.last_by_cycle(xrows(lab, jid))

    class NoEntryBar:                                                        # a capture whose data lacks the entry session
        def bar(self, d):
            return None if d == D("2026-09-29") else type("Bar", (), {"open": 101.0, "high": 104.0, "low": 98.0})()
    obs = {**prev, "state_before": "OPEN", "state_after": "OPEN"}
    (r,) = XC.rows_for("AMD", D("2026-09-30"), prev, obs, [], NoEntryBar(), {"dataset_id": "d" * 32, "content_hash": "c" * 64},
                       last, "2026-10-01T12:00:00+00:00")
    assert (r["status"], r["reason_code"], r["tracking_status"]) == ("EXCURSION_DATA_UNAVAILABLE", "PRICE_BASIS_UNAVAILABLE",
                                                                     "INCOMPLETE_DATA_UNAVAILABLE")


# ---- 42 / 43 / 44: dedup, future leak, split ---------------------------------------------------------------------------------------

def test_42_recording_the_same_session_twice_adds_nothing():
    lab = FL.FLab(market({"2026-09-29": (100, 106, 97, 101)}))
    jid = run(lab, ("2026-09-28", "2026-09-29"))
    before = xrows(lab, jid)
    assert lab.record(jid, FL.at(D("2026-09-29")))["status"] == "ALREADY_RECORDED"
    assert xrows(lab, jid) == before
    r = before[0]
    with raw(lab) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute(f"INSERT INTO forward_test_excursions ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))


def _cached_future_lab(mutate):
    """Bars through Dec 31 are already in the (newest) cached dataset; captures must still read only through T."""
    m = market({"2026-09-29": (100, 106, 97, 101), "2026-09-30": (101, 110, 99, 104), "2026-10-01": (104, 108, 100, 106)})
    lab = FL.FLab(m)
    if mutate:
        m.mutate_after(D("2026-09-30"))
    for s in ("AMD", "SPY"):
        rows = [m.rows[s][d] for d in sorted(m.rows[s])]
        lab.cache(s, rows, requested_start="2025-11-01", requested_end="2026-12-31", fetched_at="2026-12-31T00:00:00+00:00")
    return lab


def test_43_bars_after_T_never_change_stored_excursions():
    keep = ("session_date", "observation_type", "observed_open", "observed_high", "observed_low", "session_high_pct",
            "session_low_pct", "cumulative_mfe_pct", "cumulative_mae_pct", "tracking_status")
    out = []
    for mutate in (False, True):
        lab = _cached_future_lab(mutate)
        jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30"), spec=fspec(exit_=hold(5)))
        out.append([{k: r[k] for k in keep} for r in xrows(lab, jid)])
    assert out[0] == out[1] and len(out[0]) == 2 and out[0][-1]["cumulative_mfe_pct"] == pytest.approx(10.0)


def test_44_split_during_an_open_cycle_creates_no_fake_excursion():
    def lab_with(split):
        m = market({"2026-09-29": (100, 106, 97, 101), "2026-09-30": (101, 104, 98, 102), "2026-10-01": (102, 107, 99, 103),
                    "2026-10-02": (103, 105, 101, 104)})
        if split:
            m.split = ("AMD", D("2026-09-30"), 2.0)                          # 2-for-1 effective at the Sep 30 open
        lb = FL.FLab(m)
        j = run(lb, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"), spec=fspec(exit_=hold(3)))
        return lb, j
    a, ja = lab_with(False)
    b, jb = lab_with(True)
    ra, rb = xrows(a, ja), xrows(b, jb)
    assert rb[0]["entry_reference_price"] == pytest.approx(200.0)            # captured before the split: raw prices
    assert [r["observation_type"] for r in rb] == ["ENTRY_SESSION", "HELD_SESSION", "HELD_SESSION", "EXIT_OPEN"]
    for x, y in zip(ra, rb):
        for k in ("session_high_pct", "session_low_pct", "cumulative_mfe_pct", "cumulative_mae_pct"):
            assert x[k] == pytest.approx(y[k], abs=1e-9), k                  # identical to the no-split journal
    assert rb[1]["basis_entry_open"] == pytest.approx(100.0) and rb[-1]["cumulative_mae_pct"] > -5    # no fake -50 %
    ex = next(f for f in b.fs.fills(jb) if f["fill_type"] == "EXIT")
    assert rb[-1]["session_high_pct"] == pytest.approx(ex["reference_move_pct"])


# ---- 47: additive migration on copies ---------------------------------------------------------------------------------------------

def _digest(conn, tables):
    out = {}
    for t in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        h = hashlib.sha256((conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0] or "").encode())
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY {", ".join(chr(34) + c + chr(34) for c in cols)}'):
            h.update(repr(row).encode())
        out[t] = h.hexdigest()
    return out


def _migrate(path):
    conn = sqlite3.connect(str(path))
    before = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    d0, uv0 = _digest(conn, before), conn.execute("PRAGMA user_version").fetchone()[0]
    trig0 = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('trigger', 'index')")}
    run_excursion_migrations(conn)
    schema1 = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
    run_excursion_migrations(conn)                                                            # idempotent
    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()) == schema1
    after = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    assert _digest(conn, before) == d0 and conn.execute("PRAGMA user_version").fetchone()[0] == uv0
    trig1 = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('trigger', 'index')")}
    assert all(trig1[k] == v for k, v in trig0.items())
    conn.close()
    return set(before), set(after)


def test_47_migration_on_a_copy_of_the_real_database_is_additive_and_idempotent():
    real = ROOT / "data" / "stock_agent.db"
    if not real.exists():
        pytest.skip("no real database")
    tmp = Path(tempfile.mkdtemp(prefix="ex36mig-")) / "copy.db"
    src, dst = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True), sqlite3.connect(str(tmp))
    src.backup(dst)
    src.close()
    dst.close()
    before, after = _migrate(tmp)
    assert {"forward_test_excursions", "forward_test_excursion_tracking"} <= after - before
    assert {"research_snapshots", "research_outcomes", "strategy_versions"} <= before


def test_47_migration_on_a_stage33_journal_database_keeps_every_row():
    lab = LegacyLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    rows0 = lab.fs.raw_rows(jid)
    before, after = _migrate(lab.path)
    assert after - before == {"forward_test_excursions", "forward_test_excursion_tracking"}
    assert ForwardStore(lab.path).raw_rows(jid) == rows0


# ---- API, Evidence, read-only views ---------------------------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from datetime import timezone
    from api.routes import forward_tests as routes
    from api.routes import portfolio as pr
    from api.server import app
    from pf_fixtures import FakeGatewayHttp, fake_provider
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    lab = FL.FLab(market({"2026-09-29": (100, 108, 96, 101), "2026-09-30": (101, 104, 98, 102),
                          "2026-10-01": (105, 130, 80, 110)}))
    monkeypatch.setattr(routes, "get_forward_store", lambda: lab.fs)
    monkeypatch.setattr(routes, "get_backtest_store", lambda: lab.store)
    monkeypatch.setattr("data.market_data.fetch_daily_bars", lab.market.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    clock = [FL.ny(D("2026-09-25"), 12)]
    monkeypatch.setattr(J, "_now", lambda now=None: (now or clock[0]).astimezone(timezone.utc))
    c = TestClient(app)
    c.lab, c.calls, c.http, c.clock = lab, calls, http, clock
    return c


def test_api_adds_excursions_to_the_existing_journal_endpoints(api):
    lab = api.lab
    sid = lab.save(fspec())["strategy_id"]
    jid = api.post("/api/forward-tests", json={"strategy_id": sid, "version_number": 1}).json()["journal"]["journal_id"]
    for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"):
        api.clock[0] = FL.at(D(d))
        lab.market.now = api.clock[0]
        r = api.post(f"/api/forward-tests/{jid}/record").json()
        assert r["status"] == "RECORDED" and "excursions" in r["journal"] and "excursions" in r["session"]
    v = api.get(f"/api/forward-tests/{jid}").json()
    x = v["excursions"]
    assert x["tracking"]["activated_in_session"] == "2026-09-28" and x["cycles"][0]["status"] == "COMPLETE"
    assert x["metrics"]["tracked_completed_cycles"] == 1 and x["cycles"][0]["mfe_pct"] == pytest.approx(8.0)
    s = api.get(f"/api/forward-tests/{jid}/sessions/2026-10-01").json()
    assert [r["observation_type"] for r in s["excursions"]] == ["EXIT_OPEN"]
    from api.server import app
    fw = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/forward-tests")}
    assert not any(re.search(r"(?i)mfe|mae|excursion", p) for p in fw)                     # no new endpoint / button
    assert api.calls == [] and api.http.requests == []


def test_evidence_side_by_side_mfe_mae_only_with_a_tracked_sample(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    m = FL.Market(("AMD", "MU", "NVDA", "SPY"), start=date(2024, 11, 1), vol=0.012)
    lab = FL.FLab(m)
    for s in ("AMD", "MU", "NVDA", "SPY"):
        rows = [m.rows[s][d] for d in sorted(m.rows[s]) if d <= D("2026-09-25")]
        lab.cache(s, rows, requested_start="2024-11-01", requested_end="2026-09-25")
    sp = fspec(entry={"logic": "ANY", "conditions": [{"feature": "stock.trend", "op": "==", "value": "UPTREND"},
                                                     {"feature": "stock.rsi_14", "op": "<", "value": 45}]},
               exit_=hold(2), symbols=("AMD", "MU", "NVDA"), name="Side")
    sid = lab.save(sp)["strategy_id"]
    R.start_run(lab.store, X.body(sid, start="2026-01-05", end="2026-09-25"), now=X.NOW)
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-18"), 12))["journal_id"]
    for d in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"):
        lab.record(jid, FL.at(D(d)))
    v = V.view(lab.fs.journal(jid)["strategy_version_id"], path=lab.path)
    xm = v["forward"]["excursion_metrics"]
    assert xm["tracked_completed_cycles"] >= 1 and xm["tracked_completed_cycles"] == xm["completed_cycles"]
    rows = {r["key"]: r for r in v["comparison"]["compatible_metrics"]}
    h = v["historical"]["metrics"]
    for key, hk, fk in (("mfe", "average_mfe_pct", "average_mfe_pct"), ("mae", "average_mae_pct", "average_mae_pct")):
        r = rows[key]
        assert r["forward"] == xm[fk] and r["historical"] == h[hk] and r["difference_unit"] == "pp"
        assert r["difference"] == V.difference(xm[fk], h[hk]) and r["forward_count"]["tracked"] == xm["tracked_completed_cycles"]
    done = [c for c in v["forward"]["cycles"] if c["status"] == "COMPLETED"]
    assert xm["average_mfe_pct"] == pytest.approx(sum(c["excursion"]["mfe_pct"] for c in done) / len(done))
    assert not re.search(r"(?i)better|worse|safer|riskier|improv|degrad", json.dumps(v["comparison"]))


def test_views_only_read_stored_rows(monkeypatch):
    lab = FL.FLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    before = (J.journal_view(lab.fs, jid), J.session_view(lab.fs, jid, "2026-10-01"))
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("recomputed"))  # noqa: E731
    monkeypatch.setattr(XC, "rows_for", boom)
    monkeypatch.setattr("data.market_data.fetch_daily_bars", boom)
    assert (J.journal_view(lab.fs, jid), J.session_view(lab.fs, jid, "2026-10-01")) == before
    vid = lab.fs.journal(jid)["strategy_version_id"]
    V.view(vid, path=lab.path)
    ro = RO.ReadOnlyForwardStore(lab.path)
    assert ro.excursions(jid) == lab.fs.excursions(jid) and ro.excursion_tracking(jid) == lab.fs.excursion_tracking(jid)


# ---- 52: security; UI hooks + neutral language --------------------------------------------------------------------------------------

NEW_FILES = ["forward/excursions.py", "database/excursion_migrations.py"]


def test_52_no_code_execution_broker_ai_or_scheduler():
    for f in NEW_FILES + ["forward/journal.py", "forward/store.py", "comparison/view.py", "frontend/forward_journal.js",
                          "frontend/evidence.js"]:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"\bpickle\b|marshal|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|webhook|"
                             r"anthropic|get_provider\(|from agents|import agents|from portfolio|import portfolio|rh_gateway|"
                             r"robinhood|setInterval|setTimeout|apscheduler|BackgroundScheduler|add_job|crontab|"
                             r"threading\.Timer", text, re.I), f
    x = (ROOT / "forward" / "excursions.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b|sqlite3|fetch_daily_bars|market_data|shares|dollar_|pnl",
                         x.split('"""', 2)[2])


def test_ui_shows_tracked_legacy_and_incomplete_states_without_new_buttons():
    js = (ROOT / "frontend" / "forward_journal.js").read_text(encoding="utf-8")
    ev = (ROOT / "frontend" / "evidence.js").read_text(encoding="utf-8")
    for s in ("FORWARD JOURNAL STATE", "Current tracked MFE", "Current tracked MAE", "COMPLETE SO FAR",
              "Not tracked for this legacy forward cycle", "MFE / MAE tracking incomplete due to continuity gap",
              "REFERENCE CYCLES · MFE / MAE", "MFE / MAE sample", "not final while the cycle is open", "drawCycles();"):
        assert s in js, s
    for s in ("MFE / MAE sample", "Average MFE", "Average MAE", "not tracked (legacy cycle)", "incomplete (continuity gap)"):
        assert s in ev, s
    assert not re.search(r"(?i)record mfe|update mae|data-fj=\"(mfe|mae|excursion)", js)            # no new manual action
    assert "setInterval" not in js and "setTimeout" not in js
    for text in (js, ev, (ROOT / "forward" / "excursions.py").read_text(encoding="utf-8")):
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("//", "#")))
        code = re.sub(r"does not combine them into one score|no combined score|not a rating", "", code)   # Stage 3.5 disclaimers
        assert not re.search(r"(?i)\bbetter\b|\bworse\b|\bsafer\b|\briskier\b|profitable|\bscore\b|confidence|probabilit", code)
