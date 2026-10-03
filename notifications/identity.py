"""
notifications/identity.py — Stage 4.4 local notification identity for the Daily Brief desktop notification.

One static, code-owned AppUserModelID and display name. Windows shows a desktop application's notifications under the
display name registered for its AppUserModelID; an unpackaged app registers it per user with exactly ONE registry value
(approved for Stage 4.4):

    HKEY_CURRENT_USER\\Software\\Classes\\AppUserModelId\\StockAgent.Local    DisplayName (REG_SZ) = "Stock Agent"

It is written when desktop notifications are turned ON and deleted when they are turned OFF (and only this exact key:
nothing else is read or written). Per user, no administrator rights, nothing system-wide, no installer, no shortcut,
no service. The identity never depends on a strategy name, user name, machine name or database path.
"""
from __future__ import annotations

import re
import sys
from typing import Optional

APP_ID = "StockAgent.Local"
DISPLAY_NAME = "Stock Agent"
KEY = r"Software\Classes\AppUserModelId" + "\\" + APP_ID
VALUE = "DisplayName"
_ID_RE = re.compile(r"^[A-Za-z0-9]+(\.[A-Za-z0-9]+){1,3}$")
assert _ID_RE.match(APP_ID) and len(APP_ID) <= 128 and KEY.endswith("\\" + APP_ID)


class _WinReg:
    """The real per-user registry backend (only this module's one key)."""

    def get(self) -> Optional[str]:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_READ) as k:
                v, t = winreg.QueryValueEx(k, VALUE)
                return v if t == winreg.REG_SZ else None
        except OSError:
            return None

    def put(self) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, DISPLAY_NAME)

    def remove(self) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, VALUE)
        except OSError:
            pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)                 # only our (empty) key
        except OSError:
            pass


BACKEND = _WinReg()                      # tests and the browser harness replace it with an in-memory fake


def registered() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return BACKEND.get() == DISPLAY_NAME
    except Exception:  # noqa: BLE001 - an unreadable identity simply means "not branded"
        return False


def register() -> dict:
    """Desktop notifications turned ON: ensure the per-user display-name registration (idempotent)."""
    if sys.platform != "win32":
        return {"registered": False, "error_code": "NOT_WINDOWS"}
    try:
        if not registered():
            BACKEND.put()
        return {"registered": registered(), "error_code": None}
    except Exception as exc:  # noqa: BLE001 - branding is optional; notifications still work as "Python"
        return {"registered": False, "error_code": type(exc).__name__}


def unregister() -> dict:
    """Desktop notifications turned OFF: remove the registration (idempotent)."""
    if sys.platform != "win32":
        return {"registered": False, "error_code": None}
    try:
        BACKEND.remove()
        return {"registered": registered(), "error_code": None}
    except Exception as exc:  # noqa: BLE001
        return {"registered": registered(), "error_code": type(exc).__name__}
