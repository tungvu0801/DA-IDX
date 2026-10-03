"""
forward/automation.py — Stage 3.7 OPT-IN automatic after-close forward capture (local, capture only).

When the user turns it on, a small local scheduler inside the Stock Agent server performs the EXISTING Stage 3.3 / 3.6
workflow — the same `forward.journal.preflight` + `forward.journal.record` calls the "Check" and "Record latest
completed close" buttons make — for every ACTIVE or CONTINUITY_BLOCKED journal, once per check. There is no second
capture implementation: evidence, MISSED sessions, continuity blocks and MFE / MAE come from the ordinary capture.

Rules kept exactly:
  * completed sessions only — Stage 3.3 treats a session as complete once its New York date has ended
    (backtest.bars.last_complete_session_date = New York date - 1 day). A check at 4:15 PM ET could never capture that
    day's close, so the default check time is 00:15 ET: the first safe moment after the date ends. The rule itself is
    NOT changed here; any time can be configured and the existing check still decides.
  * no backfill / no catch-up loop — each check captures only the latest completed session; sessions missed while the
    server was off become MISSED through the normal capture, never reconstructed.
  * once per session — the journal's own lock, ALREADY_RECORDED and the database's unique constraints are authoritative;
    a lease in app_leases keeps two scheduler loops from running a check at the same time.
  * off by default; runs only while this server process is running and the computer is awake (no cloud, no OS task).
  * no trading of any kind, no order object, no broker, no Claude, no Strategy Fit, no alerts, no scanning.

Stage 4.1: the same scheduler (thread, check time, retries, lease table) also runs registered after-close EXTENSIONS —
opt-in local checks owned by other modules (saved Strategy Scanner checks). This module never imports them: an extension
registers itself with `name`, `wanted()` and `run(now, trigger, owner)`. The forward capture runs first (only when it is
turned on); each extension then runs isolated (a journal failure never stops it) and reports in its own summary section.
The scheduler thread runs when forward capture is on OR an extension wants scheduled checks.

Stage 4.3: an extension may declare phase = "after_cycle" (Daily Brief desktop delivery). It runs after the cycle —
forward capture, every regular extension, and the stored last-check summary — so it reads the completed cycle's stored
data; its result is then added to that summary. With no after-cycle extension wanted, a check is unchanged.

Scheduling uses America/New_York wall-clock times (zoneinfo, so DST is handled); the thread sleeps between checks
(waking at most every 15 minutes to re-read the wall clock, which also covers sleep / resume) and never polls market data.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from backtest.bars import NY
from database.automation_migrations import run_automation_migrations

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)           # a few concise AUTO_CAPTURE lines per day stay visible (no secrets, short ids)

DEFAULT_TIME_ET = "00:15"
RETRY_MINUTES = 15
MAX_RETRIES = 4                      # at most 4 retries of the SAME check (~1 hour), then wait for the next scheduled check
MAX_SLEEP_S = 900                    # re-read the wall clock at least every 15 minutes (no market data is touched)
STARTUP_DELAY_S = 30                 # let the server finish starting before a startup check
LEASE_NAME = "forward_auto_capture"
LEASE_TTL_MIN = 30
K_ENABLED, K_TIME, K_LAST = ("forward_auto_capture_enabled", "forward_auto_capture_time_et",
                             "forward_auto_capture_last_check")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
RETRYABLE = {"DATA_UNAVAILABLE", "UNEXPECTED_ERROR", "DATA_INTEGRITY_ERROR"}
COMPLETED_SESSION_RULE = ("A session counts as a completed close only after its New York date has ended (Stage 3.3 "
                          "rule, unchanged). The check at 00:15 ET records the close of the session that just ended.")
SERVER_NOTE = ("Runs only while this Stock Agent server is running and the computer is awake. Nothing runs in the cloud or "
               "while the computer is off or asleep; a session that is not recorded becomes MISSED and is never filled in "
               "later.")
WORKER_NOTE = "Automatic capture expects a single server process (one uvicorn worker)."
NOTE = "Automatic capture records observations only. No orders are ever placed."


def _utc(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def valid_time(s) -> bool:
    return isinstance(s, str) and TIME_RE.match(s) is not None


# ================================================================================================================
# schedule (America/New_York wall clock; zoneinfo handles DST — never a fixed UTC offset)
# ================================================================================================================

def _at(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, dtime(h, m), tzinfo=NY).astimezone(timezone.utc)


def next_scheduled(now: datetime, hhmm: str) -> datetime:
    """The first scheduled instant strictly after `now` (UTC)."""
    now = _utc(now)
    d = now.astimezone(NY).date()
    for k in range(0, 3):
        t = _at(d + timedelta(days=k), hhmm)
        if t > now:
            return t
    raise AssertionError("unreachable")


def previous_scheduled(now: datetime, hhmm: str) -> datetime:
    """The latest scheduled instant at or before `now` (UTC)."""
    now = _utc(now)
    d = now.astimezone(NY).date()
    for k in range(0, 3):
        t = _at(d - timedelta(days=k), hhmm)
        if t <= now:
            return t
    raise AssertionError("unreachable")


# ================================================================================================================
# settings + lease (additive tables; reading never migrates — an untouched database means OFF)
# ================================================================================================================

class AutomationStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self, readonly: bool = False):
        if readonly:
            conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000;")
        try:
            yield conn
        finally:
            conn.close()

    def settings(self) -> dict:
        rows = {}
        try:
            with self._connect(readonly=True) as conn:
                rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings")}
        except sqlite3.OperationalError:              # no settings yet (never enabled): the default is OFF
            rows = {}
        t = rows.get(K_TIME)
        try:
            last = json.loads(rows[K_LAST]) if rows.get(K_LAST) else None
        except ValueError:
            last = None
        return {"enabled": rows.get(K_ENABLED) == "true", "capture_time_et": t if valid_time(t) else DEFAULT_TIME_ET,
                "last_check": last}

    def save(self, enabled: Optional[bool] = None, capture_time_et: Optional[str] = None,
             last_check: Optional[dict] = None, now: Optional[datetime] = None) -> None:
        stamp = _iso(_utc(now))
        vals = {}
        if enabled is not None:
            vals[K_ENABLED] = "true" if enabled else "false"
        if capture_time_et is not None:
            if not valid_time(capture_time_et):
                raise ValueError("capture_time_et must be HH:MM (24-hour, New York time)")
            vals[K_TIME] = capture_time_et
        if last_check is not None:
            vals[K_LAST] = json.dumps(last_check, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            run_automation_migrations(conn)
            for k, v in vals.items():
                conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                             "SET value = excluded.value, updated_at = excluded.updated_at", (k, v, stamp))
            conn.commit()

    def acquire(self, owner: str, now: Optional[datetime] = None, ttl_min: int = LEASE_TTL_MIN, name: str = LEASE_NAME) -> bool:
        now = _utc(now)
        with self._connect() as conn:
            run_automation_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT owner, expires_at FROM app_leases WHERE name = ?", (name,)).fetchone()
            if r is not None and r["owner"] != owner and r["expires_at"] > _iso(now):
                conn.rollback()
                return False
            conn.execute("INSERT INTO app_leases (name, owner, acquired_at, expires_at) VALUES (?,?,?,?) ON CONFLICT(name) "
                         "DO UPDATE SET owner = excluded.owner, acquired_at = excluded.acquired_at, expires_at = "
                         "excluded.expires_at", (name, owner, _iso(now), _iso(now + timedelta(minutes=ttl_min))))
            conn.commit()
            return True

    def release(self, owner: str, name: str = LEASE_NAME) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_leases WHERE name = ? AND owner = ?", (name, owner))
            conn.commit()


def default_store() -> AutomationStore:
    from database.database import get_db
    return AutomationStore(Path(get_db().db_path))


# ================================================================================================================
# one check: the SAME preflight + record the buttons use, for every active journal (errors isolated per journal)
# ================================================================================================================

def _journal_result(fs, bs, j: dict, now: datetime, kw: dict) -> dict:
    from forward import journal as J
    from forward.store import ForwardError
    jid = j["journal_id"]
    base = {"journal": jid[:8], "strategy": (j.get("config") or {}).get("strategy", {}).get("name"),
            "version": j["version_number"], "journal_status": j["status"], "session": None, "code": None,
            "missed_recorded": []}
    pf_kw = {k: kw[k] for k in ("fetch_fn", "client", "research_db") if k in kw}
    try:
        pf = J.preflight(fs, bs, jid, now=now, **pf_kw)
        st = pf["status"]
        latest = (pf.get("session") or {}).get("latest_completed")
        if st in ("READY", "CAN_RECORD_WITH_MISSING_INPUT"):
            rec = J.record(fs, bs, jid, now=now, **kw)
            sess = rec["session"]["session"]["session_date"]
            if rec["status"] == "RECORDED":
                return {**base, "result": "CAPTURED", "session": sess,
                        "missed_recorded": list(rec["session"]["session"]["summary"].get("missed_detected") or [])}
            return {**base, "result": "ALREADY_RECORDED", "session": sess}
        if st == "ALREADY_RECORDED":
            return {**base, "result": "ALREADY_RECORDED", "session": latest}
        if st in ("NO_ELIGIBLE_SESSION", "SESSION_NOT_COMPLETE"):
            return {**base, "result": "NO_NEW_SESSION", "code": st}
        code = ((pf.get("errors") or [{}])[0]).get("code") or st
        return {**base, "result": "ERROR", "code": code, "session": latest}
    except ForwardError as exc:
        if exc.code in ("NO_ELIGIBLE_SESSION", "SESSION_NOT_COMPLETE"):
            return {**base, "result": "NO_NEW_SESSION", "code": exc.code}
        return {**base, "result": "ERROR", "code": exc.code}
    except Exception as exc:  # noqa: BLE001 - one journal never stops the others, and never the server
        return {**base, "result": "ERROR", "code": "UNEXPECTED_ERROR", "detail": type(exc).__name__}


def run_check(now: Optional[datetime] = None, trigger: str = "SCHEDULED", fs=None, bs=None,
              store: Optional[AutomationStore] = None, owner: Optional[str] = None, **capture_kw) -> dict:
    """One automatic check. Captures at most the latest completed session per active journal (nothing else)."""
    from backtest.store import get_backtest_store
    from forward.store import get_forward_store
    now = _utc(now)
    store = store or default_store()
    owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    summary = {"at": _iso(now), "trigger": trigger, "result": None, "latest_session": None, "journals": [],
               "counts": {"captured": 0, "already_recorded": 0, "no_new_session": 0, "errors": 0}, "retryable": False}
    logger.info("AUTO_CAPTURE check started (%s)", trigger.lower())
    if not store.acquire(owner, now):
        summary["result"] = "LEASE_HELD"
        logger.info("AUTO_CAPTURE another check is running; skipped")
        return summary
    try:
        fs = fs or get_forward_store()
        bs = bs or get_backtest_store()
        journals = [j for j in fs.list_journals() if j["status"] in ("ACTIVE", "CONTINUITY_BLOCKED")]
        if not journals:
            summary["result"] = "NO_ACTIVE_JOURNALS"
            logger.info("AUTO_CAPTURE no active forward journals")
            return summary
        for j in sorted(journals, key=lambda j: (j["created_at"], j["journal_id"])):
            r = _journal_result(fs, bs, j, now, capture_kw)
            summary["journals"].append(r)
            key = {"CAPTURED": "captured", "ALREADY_RECORDED": "already_recorded", "NO_NEW_SESSION": "no_new_session"}.get(
                r["result"], "errors")
            summary["counts"][key] += 1
            if r["result"] == "ALREADY_RECORDED":
                logger.info("AUTO_CAPTURE journal %s already recorded", r["journal"])
            elif r["result"] == "ERROR":
                logger.warning("AUTO_CAPTURE failed: %s (journal %s)", r["code"], r["journal"])
        sessions = sorted({r["session"] for r in summary["journals"] if r["session"]})
        summary["latest_session"] = sessions[-1] if sessions else None
        c = summary["counts"]
        summary["retryable"] = any(r["result"] == "ERROR" and r["code"] in RETRYABLE for r in summary["journals"])
        summary["result"] = ("PARTIAL_FAILURE" if c["errors"] and (c["captured"] or c["already_recorded"] or c["no_new_session"])
                             else "ERROR" if c["errors"] else "CAPTURED" if c["captured"]
                             else "ALREADY_RECORDED" if c["already_recorded"] else "NO_NEW_SESSION")
        if c["captured"]:
            logger.info("AUTO_CAPTURE captured %d journal(s) for %s", c["captured"], summary["latest_session"])
        elif summary["result"] == "NO_NEW_SESSION":
            logger.info("AUTO_CAPTURE no eligible session")
        return summary
    finally:
        try:
            store.release(owner)
        except sqlite3.Error:
            logger.warning("AUTO_CAPTURE could not release its lease (it expires by itself)")


# ================================================================================================================
# after-close extensions (Stage 4.1) — registered by other modules; each runs isolated after the forward capture
# ================================================================================================================

_extensions: List = []


def register_extension(ext) -> None:
    """ext: an object with .name, .wanted() -> bool and .run(now, trigger, owner) -> dict (and optionally .status())."""
    if all(e.name != ext.name for e in _extensions):
        _extensions.append(ext)


def extensions_wanted() -> bool:
    for e in _extensions:
        try:
            if e.wanted():
                return True
        except Exception:  # noqa: BLE001 - an unreadable extension state never starts or crashes the scheduler
            continue
    return False


def run_extensions(now: datetime, trigger: str, owner: str, phase: str = "cycle") -> Dict[str, dict]:
    out = {}
    for e in _extensions:
        if getattr(e, "phase", "cycle") != phase:
            continue
        try:
            if e.wanted():
                out[e.name] = e.run(now, trigger, owner)
        except Exception as exc:  # noqa: BLE001 - one extension's failure never affects the capture or the others
            out[e.name] = {"result": "ERROR", "error": type(exc).__name__, "retryable": True}
            logger.warning("AFTER_CLOSE %s failed: %s", e.name, type(exc).__name__)
    return out


def _forward_off(now: datetime, trigger: str) -> dict:
    return {"at": _iso(now), "trigger": trigger, "result": "FORWARD_CAPTURE_OFF", "latest_session": None, "journals": [],
            "counts": {"captured": 0, "already_recorded": 0, "no_new_session": 0, "errors": 0}, "retryable": False}


# ================================================================================================================
# the scheduler: one per process, one thread, sleeps between checks
# ================================================================================================================

class Scheduler:
    def __init__(self, store_fn: Callable[[], AutomationStore] = default_store,
                 check_fn: Callable[..., dict] = run_check, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 startup_delay_s: float = STARTUP_DELAY_S):
        self.store_fn, self.check_fn, self.clock, self.startup_delay_s = store_fn, check_fn, clock, startup_delay_s
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()            # one check at a time in this process
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.next_at: Optional[datetime] = None
        self.retries = 0
        self.last_error: Optional[str] = None

    # ---- decision (pure given the clock; the thread just calls step) ----------------------------------------------------
    def _plan(self, now: datetime, s: dict) -> datetime:
        """First check of this scheduler: if the latest scheduled slot has not been checked yet (e.g. the server was off
        at 00:15), check the latest completed session once now — otherwise wait for the next slot. Never a backfill."""
        slot = previous_scheduled(now, s["capture_time_et"])
        last = (s.get("last_check") or {}).get("at")
        if last is None or datetime.fromisoformat(last) < slot:
            return now
        return next_scheduled(now, s["capture_time_et"])

    def step(self, now: Optional[datetime] = None, trigger: Optional[str] = None) -> Optional[dict]:
        now = _utc(now or self.clock())
        store = self.store_fn()
        s = store.settings()
        if not (s["enabled"] or extensions_wanted()):
            self.next_at, self.retries = None, 0
            return None
        if self.next_at is None:
            self.next_at = self._plan(now, s)
        if now < self.next_at:
            return None
        return self._check(now, store, s, trigger or ("RETRY" if self.retries else "SCHEDULED"))

    def _check(self, now: datetime, store: AutomationStore, s: dict, trigger: str) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"at": _iso(now), "trigger": trigger, "result": "CHECK_IN_PROGRESS"}
        try:
            try:
                summary = (self.check_fn(now=now, trigger=trigger, store=store, owner=self.owner) if s["enabled"]
                           else _forward_off(now, trigger))
            except Exception as exc:  # noqa: BLE001 - never crash the server; report and try again later
                summary = {"at": _iso(now), "trigger": trigger, "result": "ERROR", "journals": [], "retryable": True,
                           "counts": {"captured": 0, "already_recorded": 0, "no_new_session": 0, "errors": 1},
                           "error": type(exc).__name__}
                logger.warning("AUTO_CAPTURE failed: %s", type(exc).__name__)
            ext = run_extensions(now, trigger, self.owner)          # isolated: runs whatever the capture returned
            if ext:
                summary["extensions"] = ext
                summary["retryable"] = bool(summary.get("retryable")) or any(x.get("retryable") for x in ext.values())
        finally:
            self._lock.release()
        if summary.get("retryable") and self.retries < MAX_RETRIES:
            self.retries += 1
            self.next_at = now + timedelta(minutes=RETRY_MINUTES)
        else:
            self.retries = 0
            self.next_at = next_scheduled(now, s["capture_time_et"])
        summary["next_check_at"] = _iso(self.next_at)
        self.last_error = None if summary.get("result") not in ("ERROR", "PARTIAL_FAILURE") else summary.get("result")
        if summary.get("result") not in ("LEASE_HELD", "CHECK_IN_PROGRESS"):
            try:
                store.save(last_check=summary, now=now)
            except sqlite3.Error:
                logger.warning("AUTO_CAPTURE could not save its status")
            post = run_extensions(now, trigger, self.owner, phase="after_cycle")   # Stage 4.3: reads the stored cycle
            if post:
                summary.setdefault("extensions", {}).update(post)
                try:
                    store.save(last_check=summary, now=now)
                except sqlite3.Error:
                    logger.warning("AUTO_CAPTURE could not save its status")
        return summary

    def check_now(self) -> dict:
        """'Check automation now': the same check, immediately (completed-session rules unchanged; not a backfill)."""
        store = self.store_fn()
        s = store.settings()
        now = _utc(self.clock())
        out = self._check(now, store, s, "MANUAL_CHECK")
        self._wake.set()
        return out

    # ---- thread ---------------------------------------------------------------------------------------------------------
    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - the scheduler reports and continues at the next check
                self.last_error = type(exc).__name__
                logger.warning("AUTO_CAPTURE scheduler error: %s", type(exc).__name__)
            wait = MAX_SLEEP_S
            if self.next_at is not None:
                wait = max(1.0, min(MAX_SLEEP_S, (self.next_at - _utc(self.clock())).total_seconds()))
            self._wake.wait(wait)
            self._wake.clear()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._state_lock:
            if self.running():
                return False                     # exactly one scheduler thread per process
            self._stop.clear()
            self._wake.clear()
            self.next_at, self.retries = None, 0
            self._thread = threading.Thread(target=self._loop, name="forward-auto-capture", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 30.0) -> bool:
        with self._state_lock:
            t = self._thread
            self._stop.set()
            self._wake.set()
        if t is not None:
            t.join(timeout)
        self.next_at = None
        return not (t is not None and t.is_alive())

    def wake(self) -> None:
        self.next_at = None                      # settings changed: plan again
        self._wake.set()

    def status(self) -> dict:
        s = self.store_fn().settings()
        active = s["enabled"] or extensions_wanted()
        nxt = self.next_at
        if active and nxt is None:
            nxt = self._plan(_utc(self.clock()), s)
        return {"enabled": s["enabled"], "capture_time_et": s["capture_time_et"], "default_time_et": DEFAULT_TIME_ET,
                "scheduler_running": self.running(), "next_check_at": _iso(nxt) if active else None,
                "extensions": {e.name: (e.status() if hasattr(e, "status") else {}) for e in _extensions},
                "last_check": s["last_check"], "last_error": self.last_error, "retry_pending": bool(self.retries),
                "completed_session_rule": COMPLETED_SESSION_RULE, "server_note": SERVER_NOTE, "worker_note": WORKER_NOTE,
                "scope": "Automatic capture is enabled for active forward journals (archived journals are never captured).",
                "note": NOTE}


_scheduler: Optional[Scheduler] = None
_singleton = threading.Lock()


def get_scheduler() -> Scheduler:
    global _scheduler
    with _singleton:
        if _scheduler is None:
            _scheduler = Scheduler()
        return _scheduler


def start_if_enabled() -> bool:
    """Server startup: start the scheduler only when the user turned automatic capture on, or an opt-in extension (e.g.
    a saved scan with alerts on) wants scheduled checks. Default: OFF."""
    sch = get_scheduler()
    if sch.store_fn().settings()["enabled"] or extensions_wanted():
        return sch.start()
    return False


def reconcile() -> dict:
    """An opt-in changed elsewhere (e.g. saved-scan alerts): run the scheduler exactly when something needs it."""
    sch = get_scheduler()
    if sch.store_fn().settings()["enabled"] or extensions_wanted():
        if not sch.start():
            sch.wake()
    else:
        sch.stop()
    return sch.status()


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.stop()


def configure(enabled: bool, capture_time_et: Optional[str] = None) -> dict:
    sch = get_scheduler()
    sch.store_fn().save(enabled=enabled, capture_time_et=capture_time_et)
    if enabled or extensions_wanted():
        if not sch.start():
            sch.wake()
    else:
        sch.stop()
    return sch.status()
