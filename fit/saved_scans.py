"""
fit/saved_scans.py — Stage 4.1 SAVED STRATEGY SCANS + RULES-MET CHANGE ALERTS (local, informational, opt-in).

"Did the RULES MET set of this exact saved strategy scan change after the latest completed session?"

  * a saved scan = one exact immutable strategy version + a list source (SAVED_UNIVERSE, WATCHLIST, or a normalised
    CUSTOM list). Its identity never changes; name, the alert switch and archiving are the only administrative edits.
    HOLDINGS scans are deferred: broker availability must never become a scheduler dependency.
  * a check runs the ONE Stage 4.0 scanner (fit.scanner.scan) — no second evaluator — and stores ONE immutable compact
    snapshot per saved scan and decision session (the resolved symbols, each symbol's status, the strategy hashes and a
    fingerprint). The same session checked again is a no-op.
  * the first snapshot is the baseline (no alert). Afterwards an alert EVENT is stored only when the RULES MET set changed
    among the symbols scanned in BOTH snapshots: newly_rules_met / no_longer_rules_met (with the latest status, e.g.
    INCOMPLETE DATA or STALE DATA — never implying the rules became false). Condition-count changes and watchlist
    membership changes are not rule alerts; list changes are recorded separately on the snapshot.
  * automatic checks run inside the Stage 3.7 scheduler as an after-close extension (same thread, check time and lease
    table), only for saved scans with alerts ON and not archived. A server that was off does not backfill: a check only
    ever uses the latest completed session. Manual checks run the same path; both are protected by an in-process lock,
    one SQLite write transaction and UNIQUE constraints (one snapshot / one event per scan and session).

Nothing here calls Claude, a broker, or places anything. Alerts are in-app only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from database.saved_scan_migrations import run_saved_scan_migrations
from fit import current as FC
from fit import readonly as RO
from fit import scanner as SC

ENGINE_VERSION = "4.1.0"
SOURCES = {"SAVED_UNIVERSE": "Saved universe", "WATCHLIST": "Watchlist", "CUSTOM": "Custom list"}
LEASE = "saved_scan_checks"
NOTE = ("Saved-scan alerts describe changes in which stocks meet a saved strategy version's entry rules at the latest "
        "completed close. They are informational rule-state changes — not trade signals, advice, rankings or predictions — "
        "and they are shown only inside this app.")
SERVER_NOTE = ("Automatic checks run only while this Stock Agent server is running (the Stage 3.7 scheduler, same check "
               "time). Sessions that were not checked are never reconstructed later.")


class SavedScanError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def _now(now: Optional[datetime] = None) -> str:
    return FC._utc(now).isoformat(timespec="seconds")          # the same clock as the scan itself


def canonical(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(o) -> str:
    return hashlib.sha256(canonical(o).encode("utf-8")).hexdigest()


def label_of(scan: dict) -> str:
    return f"{scan['strategy_name']} v{scan['version_number']}"


# ================================================================================================================
# storage
# ================================================================================================================

class SavedScanStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    @contextmanager
    def _connect(self, readonly: bool = False):
        if readonly:
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000;")
        try:
            yield conn
        finally:
            conn.close()

    def _read(self, sql: str, args=()) -> List[dict]:
        try:
            with self._connect(readonly=True) as conn:
                return [dict(r) for r in conn.execute(sql, args)]
        except sqlite3.OperationalError:              # never saved anything: no tables yet
            return []

    # ---- saved scans ----------------------------------------------------------------------------------------------------
    def insert_scan(self, row: dict) -> None:
        with self._connect() as conn:
            run_saved_scan_migrations(conn)
            conn.execute(f"INSERT INTO saved_strategy_scans ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
            conn.commit()

    def scans(self, include_archived: bool = True) -> List[dict]:
        return self._read("SELECT * FROM saved_strategy_scans" + ("" if include_archived else " WHERE archived_at IS NULL")
                          + " ORDER BY archived_at IS NOT NULL, created_at, saved_scan_id")

    def scan(self, saved_scan_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM saved_strategy_scans WHERE saved_scan_id = ?", (saved_scan_id,))
        return rows[0] if rows else None

    def active_identity(self, identity_hash: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM saved_strategy_scans WHERE identity_hash = ? AND archived_at IS NULL", (identity_hash,))
        return rows[0] if rows else None

    def update_admin(self, saved_scan_id: str, now: Optional[datetime] = None, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("name", "alerts_enabled", "archived_at") and v is not None}
        if not allowed:
            return
        with self._connect() as conn:
            run_saved_scan_migrations(conn)
            sets = ", ".join(f"{k} = ?" for k in allowed)
            conn.execute(f"UPDATE saved_strategy_scans SET {sets}, updated_at = ? WHERE saved_scan_id = ?",
                         [*allowed.values(), _now(now), saved_scan_id])
            conn.commit()

    def wanted(self) -> bool:
        return bool(self._read("SELECT 1 FROM saved_strategy_scans WHERE alerts_enabled = 1 AND archived_at IS NULL LIMIT 1"))

    # ---- snapshots + events -----------------------------------------------------------------------------------------------
    def snapshots(self, saved_scan_id: str, limit: int = 20) -> List[dict]:
        return [_snap_out(r) for r in self._read("SELECT * FROM saved_scan_snapshots WHERE saved_scan_id = ? "
                                                 "ORDER BY decision_session DESC LIMIT ?", (saved_scan_id, limit))]

    def latest(self, saved_scan_id: str) -> Optional[dict]:
        s = self.snapshots(saved_scan_id, 1)
        return s[0] if s else None

    def events(self, saved_scan_id: Optional[str] = None, unread_only: bool = False, limit: int = 50,
               before: Optional[str] = None) -> List[dict]:
        where, args = [], []
        if saved_scan_id:
            where.append("e.saved_scan_id = ?")
            args.append(saved_scan_id)
        if unread_only:
            where.append("e.read_at IS NULL")
        if before and "|" in before:
            sess, aid = before.split("|", 1)
            where.append("(e.decision_session < ? OR (e.decision_session = ? AND e.alert_id < ?))")
            args += [sess, sess, aid]
        sql = ("SELECT e.*, s.name AS scan_name, s.strategy_version_id, s.list_source, s.archived_at AS scan_archived_at "
               "FROM saved_scan_alert_events e JOIN saved_strategy_scans s ON s.saved_scan_id = e.saved_scan_id "
               f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY e.decision_session DESC, e.alert_id DESC LIMIT ?")
        return [_event_out(r) for r in self._read(sql, [*args, limit])]

    def unread(self) -> int:
        r = self._read("SELECT count(*) AS n FROM saved_scan_alert_events WHERE read_at IS NULL")
        return r[0]["n"] if r else 0

    def mark_read(self, alert_id: Optional[str] = None, now: Optional[datetime] = None) -> int:
        with self._connect() as conn:
            run_saved_scan_migrations(conn)
            if alert_id:
                cur = conn.execute("UPDATE saved_scan_alert_events SET read_at = ? WHERE alert_id = ? AND read_at IS NULL",
                                   (_now(now), alert_id))
            else:
                cur = conn.execute("UPDATE saved_scan_alert_events SET read_at = ? WHERE read_at IS NULL", (_now(now),))
            conn.commit()
            return cur.rowcount

    def record(self, scan: dict, result: dict, trigger: str, now: Optional[datetime] = None) -> dict:
        """Store the snapshot of one check and, when RULES MET changed since the previous snapshot, ONE alert event —
        atomically (BEGIN IMMEDIATE + UNIQUE constraints). The same session again is a no-op."""
        sid, session = scan["saved_scan_id"], result["decision_session"]
        rows = result["results"]
        status = {r["symbol"]: r["fit_status"] for r in rows}
        resolved = list(result["requested_symbols"])
        st = result["strategy"]
        fp = sha({"strategy_version_id": st["strategy_version_id"], "spec_hash": st["spec_hash"], "rules_hash": st["rules_hash"],
                  "registry": st["feature_registry_fingerprint"], "resolved_symbols": resolved, "decision_session": session,
                  "status": status})
        with self._connect() as conn:
            run_saved_scan_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            same = conn.execute("SELECT * FROM saved_scan_snapshots WHERE saved_scan_id = ? AND decision_session = ?",
                                (sid, session)).fetchone()
            if same is not None:
                conn.rollback()
                return {"result": "ALREADY_CHECKED", "snapshot": _snap_out(dict(same)), "alert": None}
            newer = conn.execute("SELECT decision_session FROM saved_scan_snapshots WHERE saved_scan_id = ? AND "
                                 "decision_session > ? LIMIT 1", (sid, session)).fetchone()
            if newer is not None:
                conn.rollback()
                return {"result": "NEWER_SNAPSHOT_EXISTS", "snapshot": None, "alert": None}
            prev = conn.execute("SELECT * FROM saved_scan_snapshots WHERE saved_scan_id = ? AND decision_session < ? "
                                "ORDER BY decision_session DESC LIMIT 1", (sid, session)).fetchone()
            prev = _snap_out(dict(prev)) if prev is not None else None
            pstat = (prev or {}).get("status_map") or {}
            added = sorted(set(resolved) - set(pstat)) if prev else []
            removed = sorted(set(pstat) - set(resolved)) if prev else []
            both = sorted(set(resolved) & set(pstat))
            newly = [{"symbol": s, "previous_status": pstat[s], "status": status[s]} for s in both
                     if status[s] == FC.RULES_MET and pstat[s] != FC.RULES_MET]
            gone = [{"symbol": s, "previous_status": pstat[s], "status": status[s]} for s in both
                    if pstat[s] == FC.RULES_MET and status[s] != FC.RULES_MET]
            snap = {"snapshot_id": uuid.uuid4().hex, "saved_scan_id": sid, "decision_session": session,
                    "evaluated_at": result["evaluated_at"], "trigger": trigger, "strategy_version_id": st["strategy_version_id"],
                    "spec_hash": st["spec_hash"], "rules_hash": st["rules_hash"], "registry_fingerprint": st["feature_registry_fingerprint"],
                    "scanner_engine": result["engine_version"], "resolved_symbols_json": canonical(resolved),
                    "status_map_json": canonical(status), "groups_json": canonical({g["group"]: g["symbols"] for g in result["groups"]}),
                    "conditions_json": canonical({r["symbol"]: [r["conditions_met"], r["conditions_total"]] for r in rows
                                                  if r["conditions_total"]}),
                    "list_changes_json": canonical({"added": [{"symbol": s, "status": status[s]} for s in added],
                                                    "removed": [{"symbol": s, "previous_status": pstat[s]} for s in removed]}),
                    "context_timing": result.get("context_timing"), "previous_snapshot_id": prev["snapshot_id"] if prev else None,
                    "is_baseline": 0 if prev else 1, "snapshot_fingerprint": fp}
            conn.execute(f"INSERT INTO saved_scan_snapshots ({', '.join(snap)}) VALUES ({', '.join('?' * len(snap))})", list(snap.values()))
            alert = None
            if prev and (newly or gone):
                alert = {"alert_id": uuid.uuid4().hex, "saved_scan_id": sid, "snapshot_id": snap["snapshot_id"],
                         "previous_snapshot_id": prev["snapshot_id"], "decision_session": session,
                         "previous_session": prev["decision_session"], "newly_rules_met_json": canonical(newly),
                         "no_longer_rules_met_json": canonical(gone),
                         "alert_fingerprint": sha({"saved_scan_id": sid, "previous": prev["snapshot_fingerprint"], "current": fp,
                                                   "decision_session": session, "newly": newly, "no_longer": gone}),
                         "created_at": _now(now), "read_at": None}
                conn.execute(f"INSERT INTO saved_scan_alert_events ({', '.join(alert)}) VALUES ({', '.join('?' * len(alert))})",
                             list(alert.values()))
            conn.commit()
        out = "BASELINE" if not prev else "ALERT_CREATED" if alert else "NO_CHANGE"
        return {"result": out, "snapshot": _snap_out(snap), "alert": _event_out({**alert, "scan_name": scan["name"]}) if alert else None}


def _loads(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _snap_out(r: dict) -> dict:
    out = {k: v for k, v in r.items() if not k.endswith("_json")}
    out.update(resolved_symbols=_loads(r.get("resolved_symbols_json"), []), status_map=_loads(r.get("status_map_json"), {}),
               groups=_loads(r.get("groups_json"), {}), conditions=_loads(r.get("conditions_json"), {}),
               list_changes=_loads(r.get("list_changes_json"), {"added": [], "removed": []}))
    out["rules_met"] = sorted(s for s, st in out["status_map"].items() if st == FC.RULES_MET)
    out["is_baseline"] = bool(out.get("is_baseline"))
    return out


def _event_out(r: dict) -> dict:
    out = {k: v for k, v in r.items() if not k.endswith("_json")}
    out.update(newly_rules_met=_loads(r.get("newly_rules_met_json"), []), no_longer_rules_met=_loads(r.get("no_longer_rules_met_json"), []))
    return out


# ================================================================================================================
# alert wording (informational rule-state changes only)
# ================================================================================================================

_LEFT = {FC.INCOMPLETE_DATA: "the latest scan has incomplete data, so the rule result could not be decided",
         FC.STALE_DATA: "the latest scan has stale data (no bar for the decision session), so the rules were not evaluated",
         FC.DATA_UNAVAILABLE: "market data was unavailable for it in the latest scan"}


def alert_texts(event: dict, label: str) -> List[str]:
    out = [f"{x['symbol']} newly meets the saved entry rules for {label} (RULES MET)." for x in event["newly_rules_met"]]
    for x in event["no_longer_rules_met"]:
        if x["status"] == FC.RULES_NOT_MET:
            out.append(f"{x['symbol']} no longer meets the saved entry rules for {label} (now RULES NOT MET).")
        else:
            why = _LEFT.get(x["status"], f"latest status: {SC.STATUS_LABEL.get(x['status'], x['status'])}")
            out.append(f"{x['symbol']} is no longer in RULES MET for {label}; {why}.")
    return out


# ================================================================================================================
# operations
# ================================================================================================================

def _versions(path: Path) -> Dict[str, dict]:
    return {v["strategy_version_id"]: v for v in RO.saved_versions(path, include_old=True)}


def _enrich(scan: dict, versions: Dict[str, dict]) -> dict:
    v = versions.get(scan["strategy_version_id"]) or {}
    return {**{k: v2 for k, v2 in scan.items() if k != "custom_symbols_json"},
            "custom_symbols": _loads(scan.get("custom_symbols_json"), None), "alerts_enabled": bool(scan["alerts_enabled"]),
            "strategy_name": v.get("strategy_name", "(archived strategy)"), "version_number": v.get("version_number"),
            "is_current": v.get("is_current"), "source_label": SOURCES[scan["list_source"]], "available": bool(v)}


def create(strategy_version_id: str, source: str, name: Optional[str] = None, alerts_enabled: bool = False,
           symbols: Optional[List[str]] = None, *, path: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    path = Path(path) if path else RO.db_path()
    if source == "HOLDINGS":
        raise SavedScanError("HOLDINGS_DEFERRED", "Saved HOLDINGS scans are not available yet: the scheduler must never "
                             "depend on the broker connection. Save the watchlist or a custom list instead.")
    if source not in SOURCES:
        raise SavedScanError("INVALID_SOURCE", f"Unknown list source {source!r}.")
    try:
        v = SC.find_version(strategy_version_id, path)
        ref, spec, _ = SC.checked_version(v, RO.ReadOnlyBacktestStore(path))
    except SC.ScanError as exc:
        raise SavedScanError(exc.code, exc.message, exc.status) from exc
    custom = None
    if source == "CUSTOM":
        syms, bad = SC.normalise_symbols(symbols or [], split=True)
        if bad:
            raise SavedScanError("INVALID_SYMBOL", f"Not a valid ticker: {', '.join(map(str, bad[:10]))}. Nothing was saved.")
        if not syms:
            raise SavedScanError("EMPTY_LIST", "Enter at least one ticker symbol.")
        if len(syms) > SC.MAX_SYMBOLS:
            raise SavedScanError("TOO_MANY_SYMBOLS", f"{len(syms)} symbols; the limit is {SC.MAX_SYMBOLS}. Nothing was saved.")
        custom = syms
    elif symbols:
        raise SavedScanError("SYMBOLS_NOT_ACCEPTED", "Symbols are only accepted for a CUSTOM list.")
    identity = sha({"strategy_version_id": strategy_version_id, "list_source": source, "custom_symbols": custom})
    dup = SavedScanStore(path).active_identity(identity)
    if dup is not None:
        raise SavedScanError("ALREADY_SAVED", f"This exact scan is already saved as “{dup['name']}”.", 409,
                             saved_scan_id=dup["saved_scan_id"])
    label = f"{v['strategy_name']} v{v['version_number']} · {SOURCES[source]}"
    nm = " ".join(str(name or label).split())[:80] or label
    stamp = _now(now)
    row = {"saved_scan_id": uuid.uuid4().hex, "strategy_id": v["strategy_id"], "strategy_version_id": strategy_version_id,
           "list_source": source, "custom_symbols_json": canonical(custom) if custom is not None else None,
           "identity_hash": identity, "name": nm, "alerts_enabled": int(bool(alerts_enabled)), "created_at": stamp,
           "updated_at": stamp, "archived_at": None}
    SavedScanStore(path).insert_scan(row)
    if alerts_enabled:
        _reconcile()
    return get(row["saved_scan_id"], path=path)


def get(saved_scan_id: str, *, path: Optional[Path] = None, history: int = 10) -> dict:
    store = SavedScanStore(path)
    scan = store.scan(saved_scan_id)
    if scan is None:
        raise SavedScanError("NOT_FOUND", "No saved scan with that id.", 404)
    out = _enrich(scan, _versions(store.path))
    snaps = store.snapshots(saved_scan_id, history)
    events = store.events(saved_scan_id, limit=history)
    out.update(latest_snapshot=snaps[0] if snaps else None, snapshots=snaps,
               events=[{**e, "texts": alert_texts(e, label_of(out))} for e in events],
               unread=sum(1 for e in events if not e["read_at"]))
    return out


def list_scans(*, path: Optional[Path] = None) -> dict:
    store = SavedScanStore(path)
    versions = _versions(store.path)
    out = []
    for scan in store.scans(include_archived=True):
        s = _enrich(scan, versions)
        latest = store.latest(scan["saved_scan_id"])
        s["latest_snapshot"] = ({k: latest[k] for k in ("snapshot_id", "decision_session", "evaluated_at", "rules_met", "groups",
                                                        "list_changes", "is_baseline", "trigger", "snapshot_fingerprint")}
                                if latest else None)
        out.append(s)
    return {"saved_scans": out, "unread_alerts": store.unread(), "note": NOTE, "server_note": SERVER_NOTE,
            "sources": SOURCES, "holdings_note": "Saved HOLDINGS scans are deferred (no broker dependency for scheduled checks)."}


def settings(saved_scan_id: str, name: Optional[str] = None, alerts_enabled: Optional[bool] = None, *,
             path: Optional[Path] = None) -> dict:
    store = SavedScanStore(path)
    scan = store.scan(saved_scan_id)
    if scan is None:
        raise SavedScanError("NOT_FOUND", "No saved scan with that id.", 404)
    if scan["archived_at"]:
        raise SavedScanError("ARCHIVED", "This saved scan is archived; its settings can no longer change.", 409)
    nm = " ".join(str(name).split())[:80] if name is not None else None
    if name is not None and not nm:
        raise SavedScanError("INVALID_NAME", "The name cannot be empty.")
    store.update_admin(saved_scan_id, name=nm, alerts_enabled=None if alerts_enabled is None else int(alerts_enabled))
    if alerts_enabled is not None:
        _reconcile()
    return get(saved_scan_id, path=store.path)


def archive(saved_scan_id: str, *, path: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    store = SavedScanStore(path)
    scan = store.scan(saved_scan_id)
    if scan is None:
        raise SavedScanError("NOT_FOUND", "No saved scan with that id.", 404)
    if not scan["archived_at"]:
        store.update_admin(saved_scan_id, archived_at=_now(now), alerts_enabled=0)
        _reconcile()
    return get(saved_scan_id, path=store.path)


_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock(saved_scan_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks[saved_scan_id]


def check(saved_scan_id: str, trigger: str = "MANUAL_CHECK", now: Optional[datetime] = None, *,
          path: Optional[Path] = None, scan_cache: Optional[dict] = None, **scan_kw) -> dict:
    """Run the Stage 4.0 scanner for one saved scan and store its snapshot (+ an alert event if RULES MET changed)."""
    store = SavedScanStore(path)
    scan = store.scan(saved_scan_id)
    if scan is None:
        raise SavedScanError("NOT_FOUND", "No saved scan with that id.", 404)
    if scan["archived_at"]:
        raise SavedScanError("ARCHIVED", "This saved scan is archived; it is no longer checked.", 409)
    info = _enrich(scan, _versions(store.path))
    base = {"saved_scan_id": saved_scan_id, "name": scan["name"], "label": label_of(info) if info["available"] else scan["name"]}
    custom = _loads(scan.get("custom_symbols_json"), None)
    key = (scan["strategy_version_id"], scan["list_source"], tuple(custom or ()))
    with _lock(saved_scan_id):
        try:
            if scan_cache is not None and key in scan_cache:
                result = scan_cache[key]                  # identical scan in this scheduler check: reused, same session
            else:
                result = SC.scan(scan["strategy_version_id"], scan["list_source"], custom, now=now, path=store.path, **scan_kw)
                if scan_cache is not None:
                    scan_cache[key] = result
        except SC.ScanError as exc:
            return {**base, "result": "ERROR", "code": exc.code, "message": exc.message, "retryable": False}
        if result["decision_session"] is None:
            return {**base, "result": "NO_SESSION", "code": result["status"], "message": result.get("message"),
                    "retryable": result["status"] == FC.DATA_UNAVAILABLE}
        out = store.record(scan, result, trigger, now)
    out = {**base, **out, "decision_session": result["decision_session"], "retryable": False}
    if out.get("alert"):
        out["alert"]["texts"] = alert_texts(out["alert"], base["label"])
    return out


# ================================================================================================================
# the Stage 3.7 scheduler extension
# ================================================================================================================

def _reconcile() -> None:
    try:
        from forward import automation as A
        A.reconcile()
    except Exception:  # noqa: BLE001 - scheduling is best-effort; the saved scan itself is stored
        pass


class SavedScanChecks:
    """After the forward capture, check every saved scan with alerts ON (not archived) for the latest completed session."""
    name = "saved_scans"

    def __init__(self, path_fn=None, scan_kw_fn=None):
        self.path_fn = path_fn or RO.db_path
        self.scan_kw_fn = scan_kw_fn or (lambda: {})

    def wanted(self) -> bool:
        return SavedScanStore(self.path_fn()).wanted()

    def status(self) -> dict:
        store = SavedScanStore(self.path_fn())
        scans = store.scans(include_archived=False)
        return {"alerts_on": sum(1 for s in scans if s["alerts_enabled"]), "paused": sum(1 for s in scans if not s["alerts_enabled"]),
                "unread_alerts": store.unread()}

    def run(self, now: datetime, trigger: str, owner: str) -> dict:
        from forward import automation as A
        path = Path(self.path_fn())
        auto = A.AutomationStore(path)
        summary = {"result": None, "counts": {"checked": 0, "baselines": 0, "alerts_created": 0, "no_change": 0,
                                              "already_checked": 0, "no_session": 0, "errors": 0}, "scans": [], "retryable": False}
        if not auto.acquire(owner, now, name=LEASE):
            summary["result"] = "LEASE_HELD"
            return summary
        try:
            store = SavedScanStore(path)
            todo = [s for s in store.scans(include_archived=False) if s["alerts_enabled"]]
            if not todo:
                summary["result"] = "NO_ENABLED_SCANS"
                return summary
            cache: dict = {}
            kw = self.scan_kw_fn()
            for s in todo:
                try:
                    r = check(s["saved_scan_id"], "RETRY" if trigger == "RETRY" else "MANUAL_CHECK" if trigger == "MANUAL_CHECK"
                              else "SCHEDULED", now, path=path, scan_cache=cache, **kw)
                except Exception as exc:  # noqa: BLE001 - one saved scan never stops the others
                    r = {"saved_scan_id": s["saved_scan_id"], "name": s["name"], "result": "ERROR", "code": type(exc).__name__,
                         "retryable": True}
                c = summary["counts"]
                c["checked"] += 1
                key = {"BASELINE": "baselines", "ALERT_CREATED": "alerts_created", "NO_CHANGE": "no_change",
                       "ALREADY_CHECKED": "already_checked", "NO_SESSION": "no_session"}.get(r["result"], "errors")
                c[key] += 1
                summary["retryable"] = summary["retryable"] or bool(r.get("retryable"))
                summary["scans"].append({k: r.get(k) for k in ("saved_scan_id", "name", "result", "code", "decision_session")})
            c = summary["counts"]
            summary["scans"] = summary["scans"][:25]
            summary["result"] = ("PARTIAL_FAILURE" if c["errors"] and c["errors"] < c["checked"] else "ERROR" if c["errors"]
                                 else "ALERTS_CREATED" if c["alerts_created"] else "CHECKED")
            return summary
        finally:
            try:
                auto.release(owner, name=LEASE)
            except sqlite3.Error:
                pass


EXTENSION = SavedScanChecks()


def register() -> None:
    from forward import automation as A
    A.register_extension(EXTENSION)
