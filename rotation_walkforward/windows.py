"""
rotation_walkforward/windows.py — deterministic train / test windows on the SPY session calendar (DESIGN_49 §2).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Sequence

from rotation.store import canonical_json, sha256_hex


class WindowError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def _sessions_between(sessions: Sequence[date], start: date, end: date) -> List[date]:
    return [s for s in sessions if start <= s <= end]


def build_windows(sessions: Sequence[date], start: date, end: date, train_months: int, test_months: int, step_months: int,
                  min_train_sessions: int, min_test_sessions: int) -> List[dict]:
    """Window k: train [start + k·step, +train_months − 1 day], test [train_end + 1 day, +test_months − 1 day]; only windows
    whose test_end <= end exist. Fails closed on too few sessions in any window; never shortens a window silently."""
    out: List[dict] = []
    k = 0
    while True:
        train_start = add_months(start, k * step_months)
        train_end = add_months(train_start, train_months) - timedelta(days=1)
        test_start = train_end + timedelta(days=1)
        test_end = add_months(test_start, test_months) - timedelta(days=1)
        if test_end > end:
            break
        tr, te = _sessions_between(sessions, train_start, train_end), _sessions_between(sessions, test_start, test_end)
        if len(tr) < min_train_sessions:
            raise WindowError("INSUFFICIENT_TRAIN_DATA", f"window {k}: {len(tr)} train sessions between {train_start.isoformat()} and "
                                                         f"{train_end.isoformat()} (minimum {min_train_sessions}).")
        if len(te) < min_test_sessions:
            raise WindowError("INSUFFICIENT_TEST_DATA", f"window {k}: {len(te)} test sessions between {test_start.isoformat()} and "
                                                        f"{test_end.isoformat()} (minimum {min_test_sessions}).")
        w = {"window_index": k, "train_start": train_start.isoformat(), "train_end": train_end.isoformat(), "test_start": test_start.isoformat(),
             "test_end": test_end.isoformat(), "train_first_session": tr[0].isoformat(), "train_last_session": tr[-1].isoformat(),
             "test_first_session": te[0].isoformat(), "test_last_session": te[-1].isoformat(), "n_train_sessions": len(tr), "n_test_sessions": len(te)}
        w["window_hash"] = sha256_hex(canonical_json({k2: v for k2, v in w.items() if k2 != "window_hash"}))
        out.append(w)
        k += 1
        if k > 1000:
            raise WindowError("TOO_MANY_WINDOWS", "more than 1000 windows")
    if not out:
        raise WindowError("NO_WINDOWS", f"No complete train + test window fits between {start.isoformat()} and {end.isoformat()}.")
    return out


def tests_overlap(windows: Sequence[dict]) -> bool:
    for a, b in zip(windows, windows[1:]):
        if date.fromisoformat(b["test_start"]) <= date.fromisoformat(a["test_end"]):
            return True
    return False
