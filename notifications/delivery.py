"""
notifications/delivery.py — Stage 4.3 OPT-IN DAILY BRIEF DESKTOP DELIVERY (local Windows notification only).

When a NEW completed session appears in stored data, deliver ONE concise notification carrying the counts of that
session's Stage 4.2 Daily Brief (brief.daily.build — consumed, never changed) — at most once per session and channel.

  * OFF by default. Enabling records the latest stored brief session as the BASELINE (no notification for it or any
    earlier session); later sessions may notify. Re-enabling after a pause baselines again — no historical replay.
  * Runs as an isolated "after_cycle" extension of the Stage 3.7 scheduler (same thread, check time and lease table):
    forward capture -> saved-scan checks -> the cycle's summary stored -> Daily Brief read -> delivery, so the brief
    reflects the completed cycle (including a stored PARTIAL_FAILURE). Enabled delivery alone keeps the scheduler
    running; it only reads stored data.
  * Only the LATEST stored session is considered (no backfill). notify_only_if_activity (default ON) skips a brief whose
    Stage 4.2 activity counts are all zero (stored as SKIPPED_NO_ACTIVITY).
  * At most once: a PENDING row claims (brief_session, channel) under UNIQUE + the "daily_brief_delivery" lease before
    the OS call, then becomes DELIVERED / FAILED / UNSUPPORTED_PLATFORM. A restart, a second check or a second server
    never re-sends; an interrupted PENDING row is never re-sent either. No automatic retry.
  * The message is deterministic text from fixed templates (no AI, no ranking, no instruction); the browser can never
    supply notification text. "Send test notification" uses fixed server text and is recorded as kind TEST.

No market data, scanner, Claude or broker call; only the delivery tables are written.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from brief import daily as DB
from database.delivery_migrations import run_delivery_migrations
from fit import current as FC
from fit import readonly as RO
from fit import scanner as SC
from notifications import windows as W

ENGINE_VERSION = "4.3.0"
CHANNEL = "WINDOWS_DESKTOP"
LEASE = "daily_brief_delivery"
TITLE = "Stock Agent · Daily Strategy Brief"
FOOTER = "Open Daily Brief for details."
TEST_TITLE = "Stock Agent test notification"
TEST_BODY = "Desktop notifications are working.\nTEST — this is not a Daily Brief."
PREVIEW_MAX, PREVIEW_LINE_MAX = 2, 90
TEST_MIN_INTERVAL_S = 3.0
ADAPTER = W                              # the OS adapter (tests and the browser harness replace it with a fake)
NOTE = ("Local Windows desktop notification only — nothing is sent anywhere else. One notification per new stored "
        "completed session, with the counts of that session's Daily Brief. No AI, no market data, no ranking, no orders.")
SERVER_NOTE = ("Delivered only while this Stock Agent server is running and the computer is awake (after the automatic "
               "check of the Stage 3.7 scheduler). Sessions missed while it was off are never sent later.")
NEXT_TEXT = "After a new stored completed session."
_last_test = [0.0]


def _now(now: Optional[datetime] = None) -> str:
    return FC._utc(now).isoformat(timespec="seconds")


def sha(o) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ================================================================================================================
# storage (the two Stage 4.3 tables only)
# ================================================================================================================

class DeliveryStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    @contextmanager
    def _connect(self, readonly: bool = False):
        if readonly:
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            run_delivery_migrations(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000;")
        try:
            yield conn
        finally:
            conn.close()

    def _read(self, sql: str, args=()) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            with self._connect(readonly=True) as conn:
                return [dict(r) for r in conn.execute(sql, args)]
        except sqlite3.OperationalError:              # never enabled: no tables yet
            return []

    def settings(self) -> dict:
        rows = {r["key"]: r for r in self._read("SELECT key, value, updated_at FROM daily_brief_delivery_settings")}
        return {"enabled": (rows.get("enabled") or {}).get("value") == "true",
                "notify_only_if_activity": (rows.get("notify_only_if_activity") or {}).get("value", "true") == "true",
                "updated_at": max((r["updated_at"] for r in rows.values()), default=None)}

    def save_settings(self, now: Optional[datetime] = None, **values) -> None:
        stamp = _now(now)
        with self._connect() as conn:
            for k, v in values.items():
                conn.execute("INSERT INTO daily_brief_delivery_settings (key, value, updated_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                             (k, "true" if v else "false", stamp))
            conn.commit()

    def history(self, limit: int = 20) -> List[dict]:
        return self._read("SELECT * FROM daily_brief_deliveries ORDER BY rowid DESC LIMIT ?", (int(limit),))

    def last_session(self) -> Optional[dict]:
        rows = self._read("SELECT * FROM daily_brief_deliveries WHERE kind = 'SESSION' AND channel = ? "
                          "ORDER BY brief_session DESC LIMIT 1", (CHANNEL,))
        return rows[0] if rows else None

    def insert(self, row: dict) -> None:
        with self._connect() as conn:
            conn.execute(f"INSERT INTO daily_brief_deliveries ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                         list(row.values()))
            conn.commit()

    def finalize(self, delivery_id: str, status: str, error_code: Optional[str], now: Optional[datetime] = None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE daily_brief_deliveries SET status = ?, delivered_at = ?, error_code = ? WHERE delivery_id = ? "
                         "AND status = 'PENDING'", (status, _now(now) if status == "DELIVERED" else None, error_code, delivery_id))
            conn.commit()


# ================================================================================================================
# the deterministic notification text (Stage 4.2 counts; fixed templates; Windows limits)
# ================================================================================================================

def _n(k: int, one: str, many: Optional[str] = None) -> str:
    return f"{k} {one if k == 1 else (many or one + 's')}"


def _day(S: str) -> str:
    d = date.fromisoformat(S)
    return f"{d.strftime('%b')} {d.day} close"


def activity(counts: dict) -> bool:
    """Stage 4.2 counts only: a RULES MET change event, a forward CAPTURED / MISSED session, a completed reference cycle
    or a data / continuity issue. (A saved scan checked without a change is not activity.)"""
    return any(counts.get(k, 0) for k in ("rule_change_events", "forward_captures", "forward_sessions_missed",
                                           "completed_reference_cycles", "data_issues"))


def _changes(brief: dict) -> List[str]:
    """One line per symbol-level RULES MET change, in the brief's order (strategy A–Z, then the stored symbol order)."""
    out = []
    for c in brief.get("changes") or []:
        for x in c["newly_rules_met"]:
            out.append(f"{x['symbol']} newly meets the saved entry rules ({c['label']}).")
        for x in c["no_longer_rules_met"]:
            out.append(f"{x['symbol']} is no longer in RULES MET ({c['label']}); now {SC.STATUS_LABEL.get(x['status'], x['status'])}.")
    return [line if len(line) <= PREVIEW_LINE_MAX else line[:PREVIEW_LINE_MAX - 1] + "…" for line in out]


def compose(brief: dict) -> dict:
    """title / body / activity / fingerprint for one stored Daily Brief. The brief is read, never modified."""
    S, c = brief["brief_session"], brief["summary_counts"]
    head = [_day(S), " · ".join([_n(c["rule_change_events"], "rule-state change"), _n(c["forward_captures"], "forward capture"),
                                  _n(c["completed_reference_cycles"], "completed cycle")])]
    if c["data_issues"]:
        head.append(_n(c["data_issues"], "data / continuity issue"))
    lines = _changes(brief)
    body = ""
    for k in range(min(PREVIEW_MAX, len(lines)), -1, -1):                 # as many preview lines as fit (2, 1, 0)
        more = len(lines) - k
        parts = head + lines[:k] + ([f"+{_n(more, 'more change')}"] if more else []) + [FOOTER]
        body = "\n".join(parts)
        if len(body) <= W.BODY_MAX:
            break
    body = body[:W.BODY_MAX]
    return {"title": TITLE, "body": body, "brief_session": S, "activity": activity(c), "preview_lines": min(k, len(lines)),
            "changes_total": len(lines), "fingerprint": sha({"brief_session": S, "channel": CHANNEL, "title": TITLE, "body": body})}


# ================================================================================================================
# operations
# ================================================================================================================

def _latest_session(path: Path) -> Optional[str]:
    with DB._open(path) as conn:                                           # read-only (Stage 4.2 helper)
        sessions = DB.stored_sessions(conn)
    return sessions[-1] if sessions else None


def _row(kind: str, status: str, trigger: str, now, S: Optional[str] = None, msg: Optional[dict] = None,
         error_code: Optional[str] = None) -> dict:
    return {"delivery_id": uuid.uuid4().hex, "kind": kind, "brief_session": S, "channel": CHANNEL, "status": status,
            "trigger": trigger, "created_at": _now(now), "delivered_at": _now(now) if status == "DELIVERED" else None,
            "summary_fingerprint": (msg or {}).get("fingerprint"), "title": (msg or {}).get("title"),
            "body": (msg or {}).get("body"), "error_code": error_code}


def _reconcile() -> None:
    try:
        from forward import automation as A
        A.reconcile()
    except Exception:  # noqa: BLE001 - scheduling is best effort; the setting itself is stored
        pass


def status(*, path: Optional[Path] = None) -> dict:
    store = DeliveryStore(path)
    s = store.settings()
    hist = store.history(20)
    last = next((h for h in hist if h["kind"] == "SESSION"), None)
    return {"engine_version": ENGINE_VERSION, "enabled": s["enabled"], "notify_only_if_activity": s["notify_only_if_activity"],
            "updated_at": s["updated_at"], "channel": CHANNEL, "platform_supported": sys.platform == "win32",
            "last_delivery": last, "history": hist, "next": NEXT_TEXT if s["enabled"] else None,
            "note": NOTE, "server_note": SERVER_NOTE}


def configure(enabled: Optional[bool] = None, notify_only_if_activity: Optional[bool] = None, *,
              path: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    path = Path(path) if path else RO.db_path()
    store = DeliveryStore(path)
    was = store.settings()["enabled"]
    vals = {k: v for k, v in (("enabled", enabled), ("notify_only_if_activity", notify_only_if_activity)) if v is not None}
    if vals:
        store.save_settings(now, **vals)
    if enabled and not was:                                                # first enable / re-enable: BASELINE, no replay
        S = _latest_session(path)
        last = store.last_session()
        if S and not (last and last["brief_session"] >= S):
            try:
                store.insert(_row("SESSION", "BASELINE", "ENABLE", now, S))
            except sqlite3.IntegrityError:
                pass
    if enabled is not None and enabled != was:
        _reconcile()
    return status(path=path)


def send_test(*, path: Optional[Path] = None, now: Optional[datetime] = None, adapter=None) -> dict:
    """Fixed server-side text, kind TEST, no brief session. 0 market / AI / broker calls."""
    if time.monotonic() - _last_test[0] < TEST_MIN_INTERVAL_S:
        raise DeliveryError("TOO_SOON", "A test notification was just sent. Wait a few seconds.", 429)
    _last_test[0] = time.monotonic()
    r = _send(adapter, TEST_TITLE, TEST_BODY)
    msg = {"title": TEST_TITLE, "body": TEST_BODY, "fingerprint": sha({"kind": "TEST", "title": TEST_TITLE, "body": TEST_BODY})}
    DeliveryStore(path).insert(_row("TEST", r["status"], "TEST", now, None, msg, r.get("error_code")))
    return {"result": r["status"], "error_code": r.get("error_code"), "title": TEST_TITLE, "body": TEST_BODY}


def _send(adapter, title: str, body: str) -> dict:
    try:
        r = (adapter or ADAPTER).send(title, body)
        st = r.get("status") if isinstance(r, dict) else None
        return r if st in ("DELIVERED", "FAILED", "UNSUPPORTED_PLATFORM") else {"status": "FAILED", "error_code": "BAD_ADAPTER_RESULT"}
    except Exception as exc:  # noqa: BLE001 - the OS call is isolated: a failure is recorded, nothing else is affected
        return {"status": "FAILED", "error_code": type(exc).__name__}


def deliver(now: datetime, trigger: str, owner: str, *, path: Optional[Path] = None, adapter=None) -> dict:
    """The scheduler extension body: the latest stored brief session, at most once, never a backfill."""
    from forward import automation as A
    path = Path(path) if path else RO.db_path()
    store = DeliveryStore(path)
    out = {"result": None, "brief_session": None, "retryable": False}
    s = store.settings()
    if not s["enabled"]:
        return {**out, "result": "DISABLED"}
    auto = A.AutomationStore(path)
    if not auto.acquire(owner, now, name=LEASE):
        return {**out, "result": "LEASE_HELD"}
    try:
        S = _latest_session(path)
        out["brief_session"] = S
        if S is None:
            return {**out, "result": "NO_STORED_BRIEF"}
        last = store.last_session()
        if last and last["brief_session"] >= S:
            return {**out, "result": "NO_NEW_SESSION", "last_status": last["status"]}
        msg = compose(DB.build(S, path=path))
        trig = trigger if trigger in ("SCHEDULED", "RETRY", "MANUAL_CHECK") else "SCHEDULED"
        if s["notify_only_if_activity"] and not msg["activity"]:
            store.insert(_row("SESSION", "SKIPPED_NO_ACTIVITY", trig, now, S, msg))
            return {**out, "result": "SKIPPED_NO_ACTIVITY"}
        claim = _row("SESSION", "PENDING", trig, now, S, msg)
        try:
            store.insert(claim)                                            # UNIQUE (brief_session, channel): the claim
        except sqlite3.IntegrityError:
            return {**out, "result": "ALREADY_HANDLED"}
        r = _send(adapter, msg["title"], msg["body"])
        store.finalize(claim["delivery_id"], r["status"], r.get("error_code"), now)
        return {**out, "result": r["status"], "error_code": r.get("error_code"), "fingerprint": msg["fingerprint"]}
    finally:
        try:
            auto.release(owner, name=LEASE)
        except sqlite3.Error:
            pass


# ================================================================================================================
# the Stage 3.7 scheduler extension (registered after the saved-scan checks)
# ================================================================================================================

class DailyBriefDelivery:
    name = "daily_brief_delivery"
    phase = "after_cycle"                  # runs after forward capture, the saved-scan checks and the stored summary

    def __init__(self, path_fn=None, adapter=None):
        self.path_fn = path_fn or RO.db_path
        self.adapter = adapter

    def wanted(self) -> bool:
        return DeliveryStore(self.path_fn()).settings()["enabled"]

    def status(self) -> dict:
        store = DeliveryStore(self.path_fn())
        last = store.last_session()
        return {"enabled": store.settings()["enabled"],
                "last_delivery": {k: last[k] for k in ("brief_session", "status")} if last else None}

    def run(self, now: datetime, trigger: str, owner: str) -> dict:
        return deliver(now, trigger, owner, path=self.path_fn(), adapter=self.adapter)


EXTENSION = DailyBriefDelivery()


def register() -> None:
    from fit import saved_scans as SS
    from forward import automation as A
    SS.register()                          # the saved-scan checks always run first in each scheduler check
    A.register_extension(EXTENSION)
