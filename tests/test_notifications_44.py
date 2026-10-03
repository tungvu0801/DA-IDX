"""Stage 4.4 — clickable + branded Windows Daily Brief notification (notifications/windows.py, identity.py). Clicks are
simulated through the adapter's own message dispatch with a recording opener; the identity uses an in-memory registry
(conftest); the only real Win32 use here is starting / stopping the hidden message-only window (no icon, no toast)."""
import hashlib
import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import fw_fixtures as FL
from brief import daily as DB
from fit import readonly as RO
from forward import automation as A
from notifications import delivery as D
from notifications import identity as ID
from notifications import windows as W
from test_brief_delivery_43 import Fake, _brief, deliver, enable, lab_at, rows, store
from test_saved_scans_41 import S1, S2

ROOT = Path(__file__).resolve().parents[1]
CLICK = (W.CALLBACK_MSG, 0, W.NIN_BALLOONUSERCLICK | (1 << 16))       # NOTIFYICON_VERSION_4: LOWORD event, HIWORD icon id


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(W, "OPENER", urls.append)
    monkeypatch.setattr(config, "FASTAPI_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "FASTAPI_PORT", 8000)
    monkeypatch.setattr(D, "ADAPTER", Fake())
    monkeypatch.setattr(A, "reconcile", lambda: {})
    monkeypatch.setattr(D, "_last_test", [0.0])
    for k in ("clicks", "opened", "rejected"):
        monkeypatch.setitem(W._stats, k, 0)
    return urls


def digest(path):
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    h = hashlib.sha256()
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        for r in c.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
            h.update(repr(r).encode())
    c.close()
    return h.hexdigest()


# ---- 44 / 45 / 46 / 47: the click opens only the fixed local Daily Brief ----------------------------------------------------------

def test_44_a_click_opens_the_validated_local_daily_brief_once(opened):
    assert W.dispatch(*CLICK) == 0 and opened == ["http://127.0.0.1:8000/#daily-brief"]
    for other in (0x404, 0x403, 0x400, 0x0202, 0x0205):                   # timeout, hide, icon select, button up: nothing
        assert W.dispatch(W.CALLBACK_MSG, 0, other) == 0
    assert W.dispatch(0x0113, 0, 0) is None and len(opened) == 1          # unrelated messages go to Windows
    assert W._stats["clicks"] == 1 and W._stats["opened"] == 1                 # only a balloon click counts


def test_45_notification_text_never_becomes_a_link(opened):
    b = _brief([{"label": "https://evil.example v1", "newly_rules_met": [{"symbol": "AMD"}], "no_longer_rules_met": []}], rc=1)
    msg = D.compose(b)
    assert "https://evil.example" in msg["body"]                           # inert text in the notification
    W.dispatch(*CLICK)
    assert opened == ["http://127.0.0.1:8000/#daily-brief"]


@pytest.mark.parametrize("target", ["https://example.com", "http://127.0.0.1:8000/#portfolio", "https://127.0.0.1:8000/#daily-brief",
                                    "http://127.0.0.1:8001/#daily-brief", "http://127.0.0.1:8000@evil.example/#daily-brief",
                                    "http://127.0.0.1:8000/orders#daily-brief", "http://127.0.0.1:8000/#daily-brief?session=x",
                                    "file:///C:/Windows/System32/cmd.exe", "javascript:alert(1)", "", None])
def test_46_an_invalid_activation_target_is_rejected(monkeypatch, opened, target):
    monkeypatch.setattr(W, "activation_url", lambda: target)
    assert W.on_click() is False and opened == [] and W._stats["rejected"] == 1


def test_46b_a_non_loopback_server_address_disables_click(monkeypatch, opened):
    monkeypatch.setattr(config, "FASTAPI_HOST", "0.0.0.0")
    assert W.capability()["click_supported"] is False and W.capability()["activation_url"] is None
    W.dispatch(*CLICK)
    assert opened == []


def test_47_the_configured_port_is_used_and_nothing_is_scanned(monkeypatch, opened):
    monkeypatch.setattr(config, "FASTAPI_PORT", 8123)
    assert W.allowed_urls() == ("http://127.0.0.1:8123/#daily-brief", "http://localhost:8123/#daily-brief")
    W.dispatch(*CLICK)
    assert opened == ["http://127.0.0.1:8123/#daily-brief"]
    monkeypatch.setattr(config, "FASTAPI_PORT", 70000)
    assert W.allowed_urls() == () and W.on_click() is False and len(opened) == 1


# ---- 48 / 49 / 50: clicks are navigation only; delivery semantics unchanged ------------------------------------------------------

def test_48_clicking_the_test_notification_opens_the_brief_and_writes_nothing(opened):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    D.send_test(path=lab.path)
    before = digest(lab.path)
    W.dispatch(*CLICK)
    assert opened == ["http://127.0.0.1:8000/#daily-brief"] and digest(lab.path) == before
    assert [(r["kind"], r["brief_session"]) for r in D.DeliveryStore(lab.path).history()] == [("TEST", None)]


def test_49_three_clicks_one_delivery_row(opened):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    assert deliver(lab, S2)["result"] == "DELIVERED"
    before = digest(lab.path)
    for _ in range(3):
        W.dispatch(*CLICK)
    assert len(opened) == 3 and set(opened) == {"http://127.0.0.1:8000/#daily-brief"}
    assert [r["status"] for r in rows(lab)] == ["BASELINE", "DELIVERED"] and digest(lab.path) == before


def test_50_restart_no_resend_click_still_opens_the_brief(opened):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    assert deliver(lab, S2)["result"] == "DELIVERED"
    W.shutdown()                                                              # the server stops ...
    assert deliver(lab, S2)["result"] == "NO_NEW_SESSION" and len(D.ADAPTER.calls) == 1     # ... and restarts
    W.dispatch(*CLICK)
    assert opened == ["http://127.0.0.1:8000/#daily-brief"]


# ---- 51 / 52: lifecycle cleanup (real hidden window, no icon, no toast); platform ---------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 message-only window lifecycle")
def test_51_start_stop_repeatedly_leaves_no_thread_window_or_icon():
    api = W._load()
    seen = []
    for _ in range(4):
        assert W.start() and W.start()                                        # idempotent: one thread, one window
        t, hwnd = W._st["thread"], W._st["hwnd"]
        assert t.is_alive() and t.name == W.THREAD_NAME and api["user32"].IsWindow(hwnd)
        assert sum(1 for x in threading.enumerate() if x.name == W.THREAD_NAME) == 1
        seen.append((t, hwnd))
        W.shutdown()
        assert not t.is_alive() and not api["user32"].IsWindow(hwnd) and W._st["thread"] is None and W._icons == []
    assert not any(x.name == W.THREAD_NAME for x in threading.enumerate()) and W._st["class"] is False
    assert W.capability()["adapter_running"] is False


def test_52_other_platforms_are_unsupported_without_crashing(monkeypatch, opened):
    monkeypatch.setattr(sys, "platform", "linux")
    assert W.start() is False and W.shutdown() is None and ID.registered() is False and ID.register()["registered"] is False
    c = W.capability()
    assert (c["platform"], c["click_supported"], c["branding"], c["branding_active"]) == ("linux", False, "UNSUPPORTED_PLATFORM", False)


# ---- 53 / 54: identity — static, code-owned; registry scope limited to one per-user value ------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="identity registration is Windows-only")
def test_53_branding_state_follows_the_per_user_registration():
    assert (ID.APP_ID, ID.DISPLAY_NAME) == ("StockAgent.Local", "Stock Agent")
    assert ID.KEY == "Software\\Classes\\AppUserModelId\\StockAgent.Local" and ID.VALUE == "DisplayName"
    c = W.capability()
    assert (c["branding"], c["branding_active"], c["source_shown"]) == ("BRANDING_DEFERRED", False, "Python")
    assert ID.register() == {"registered": True, "error_code": None} and ID.BACKEND.value == "Stock Agent"
    c = W.capability()
    assert (c["branding"], c["branding_active"], c["source_shown"], c["app_id"], c["display_name"]) == \
        ("BRANDED", True, "Stock Agent", "StockAgent.Local", "Stock Agent")
    assert ID.unregister()["registered"] is False and W.capability()["branding"] == "BRANDING_DEFERRED"


@pytest.mark.skipif(sys.platform != "win32", reason="identity registration is Windows-only")
def test_53b_notifications_on_registers_off_removes_via_the_api(monkeypatch):
    from api.server import app
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    c = TestClient(app)
    assert c.get("/api/brief-delivery").json()["identity"]["branding"] == "BRANDING_DEFERRED"
    on = c.post("/api/brief-delivery/settings", json={"enabled": True}).json()
    assert on["identity"]["branding"] == "BRANDED" and ID.BACKEND.value == "Stock Agent"
    off = c.post("/api/brief-delivery/settings", json={"enabled": False}).json()
    assert off["identity"]["branding"] == "BRANDING_DEFERRED" and ID.BACKEND.value is None
    for bad in ({"enabled": True, "app_id": "Evil.App"}, {"activation_url": "https://example.com"}, {"display_name": "x"}):
        assert c.post("/api/brief-delivery/settings", json=bad).status_code == 422


def test_54_registry_access_is_one_module_one_key_one_value():
    srcs = {f: (ROOT / f).read_text(encoding="utf-8") for f in ("notifications/windows.py", "notifications/delivery.py",
                                                               "api/routes/daily_brief_delivery.py", "api/server.py",
                                                               "database/delivery_migrations.py", "frontend/brief_delivery.js")}
    for f, s in srcs.items():
        assert not re.search(r"winreg|RegSetValue|RegCreateKey|RegDeleteKey|advapi32", s), f
    ident = (ROOT / "notifications" / "identity.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in ident.splitlines() if not line.lstrip().startswith("#"))
    assert code.count("SetValueEx(") == 1 and code.count("CreateKeyEx(") == 1 and code.count("DeleteKey(") == 1
    assert code.count("winreg.HKEY_CURRENT_USER") == 4 and "HKEY_LOCAL_MACHINE" not in code and "HKEY_CLASSES_ROOT" not in code
    assert re.fullmatch(r"[A-Za-z0-9]+(\.[A-Za-z0-9]+)+", ID.APP_ID)


def test_security_no_process_no_shell_only_the_standard_browser_opener():
    for f in ("notifications/windows.py", "notifications/identity.py", "frontend/brief_delivery.js", "api/routes/daily_brief_delivery.py"):
        s = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|subprocess|os\.system|os\.popen|os\.startfile|ShellExecute|CreateProcess|WinExec|"
                             r"powershell|cmd\.exe|TradingClient|anthropic|get_provider|place_?order|/orders|requests\.|urllib|"
                             r"http\.client|socket\.|smtp|webhook", s, re.I), f
    w = (ROOT / "notifications" / "windows.py").read_text(encoding="utf-8")
    assert w.count("webbrowser.open") == 1 and "OPENER(url)" in w and w.count("threading.Thread(") == 1
    js = (ROOT / "frontend" / "brief_delivery.js").read_text(encoding="utf-8")
    assert js.count('location.hash === "#daily-brief"') == 1 and 'setView("brief")' in js and "hashchange" not in js
    assert "setInterval" not in js and "setTimeout" not in js


# ---- server lifecycle: identity kept while ON; adapter stopped on shutdown ---------------------------------------------------------

def test_server_lifespan_registers_identity_when_on_and_stops_the_adapter(monkeypatch):
    from api.server import app
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(config, "EVENT_WARMUP_ON_STARTUP", False)
    monkeypatch.setattr(A, "start_if_enabled", lambda: False)
    stopped = []
    monkeypatch.setattr(W, "shutdown", lambda *a, **k: stopped.append(1))
    D.configure(True, path=lab.path, now=FL.at(S1))
    ID.BACKEND.remove()
    with TestClient(app):
        assert ID.registered() is (sys.platform == "win32")
    assert stopped == [1]


def test_stage_43_contract_unchanged_message_fingerprint_and_semantics():
    b = _brief([{"label": "Up v1", "newly_rules_met": [{"symbol": "AMD"}], "no_longer_rules_met": []}], rc=1, fc=1)
    m = D.compose(b)
    assert m["fingerprint"] == D.sha({"brief_session": "2026-09-30", "channel": "WINDOWS_DESKTOP", "title": m["title"], "body": m["body"]})
    assert "daily-brief" not in m["body"] and "http" not in m["body"]      # the click target is not message content
