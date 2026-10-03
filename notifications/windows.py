"""
notifications/windows.py — Stage 4.3 LOCAL Windows desktop notification adapter; Stage 4.4 click-to-open + identity.

    send(title, body) -> {"status": "DELIVERED" | "FAILED" | "UNSUPPORTED_PLATFORM", "error_code": str | None}
    capability() -> platform, click / branding support and state, app id, display name, activation URL
    shutdown()   -> remove the icon, destroy the window, stop the message loop (server shutdown / exit)

Standard library only: ctypes calling the native Win32 Shell_NotifyIconW API (Windows 10 / 11 show it as a toast) and
webbrowser for the click. No new dependency, no process, no shell, no network.

Lifecycle (Stage 4.4): ONE adapter per process, started on the first notification — one daemon thread ("stock-agent-
notify") owning one hidden message-only window and running its message loop. The notification-area icon is added when a
notification is shown and kept (Windows keeps a toast in the notification center only while its icon exists); shutdown()
removes the icon, destroys the window, ends the loop, joins the thread and unregisters the window class. Sequential
notifications reuse the same window and icon.

Click: clicking a toast makes Windows send NIN_BALLOONUSERCLICK to the window; the adapter then opens exactly ONE fixed
URL — the local Daily Brief (http://<FASTAPI_HOST>:<FASTAPI_PORT>/#daily-brief, loopback only) — after validating its
scheme, host, port, path, query and fragment. Notification text is never parsed for links; the target cannot come from
the browser, a strategy name or the database. A click writes nothing.

Identity (Stage 4.4): when the per-user AppUserModelID registration exists (notifications/identity.py, written only while
desktop notifications are ON), the process uses the AppUserModelID "StockAgent.Local" before its window is created, so
Windows can show the display name "Stock Agent" instead of "Python". Without it, the process keeps its default identity.
"""
from __future__ import annotations

import atexit
import sys
import threading
import webbrowser
from typing import Dict, List, Optional

import config
from notifications import identity as ID

TITLE_MAX, BODY_MAX = 63, 255
TIP = "Stock Agent notifications"
CLASS_NAME = "StockAgentNotifyWindow"
THREAD_NAME = "stock-agent-notify"
FRAGMENT = "daily-brief"
LOOPBACK = ("127.0.0.1", "localhost")
OPENER = webbrowser.open                 # the only way a click leaves the adapter (tests / the harness replace it)
_icons: List[tuple] = []                 # (hwnd, uid) of the one live icon — for exit cleanup and tests
_lock = threading.RLock()
_st: Dict[str, object] = {"thread": None, "hwnd": None, "ready": None, "error": None, "branded": False, "class": False}
_stats = {"clicks": 0, "opened": 0, "rejected": 0, "starts": 0, "stops": 0}
_api = None

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NOTIFYICON_VERSION_4, HWND_MESSAGE = 0x1, 4, -3
WM_CLOSE, WM_DESTROY, WM_APP, WM_USER = 0x0010, 0x0002, 0x8000, 0x0400
CALLBACK_MSG = WM_APP + 1
NIN_BALLOONUSERCLICK = WM_USER + 5


# ================================================================================================================
# the activation target (fixed, local, validated)
# ================================================================================================================

def activation_url() -> str:
    return f"http://{config.FASTAPI_HOST}:{int(config.FASTAPI_PORT)}/#{FRAGMENT}"


def allowed_urls() -> tuple:
    """The complete allow-list: the local Daily Brief on the configured port, on a loopback host — nothing else."""
    try:
        port = int(config.FASTAPI_PORT)
    except (TypeError, ValueError):
        return ()
    if not 0 < port < 65536:
        return ()
    return tuple(f"http://{h}:{port}/#{FRAGMENT}" for h in LOOPBACK)


def valid_activation(url) -> bool:
    """Exact match against allowed_urls(): scheme http, host 127.0.0.1 or localhost, the configured port, path "/", no
    query, fragment daily-brief. No parsing, no prefix match, no user-controlled part."""
    return isinstance(url, str) and url in allowed_urls()


def on_click() -> bool:
    """A toast was clicked: open the local Daily Brief (never anything else). Returns whether a page was opened."""
    _stats["clicks"] += 1
    url = activation_url()
    if not valid_activation(url):
        _stats["rejected"] += 1
        return False
    try:
        OPENER(url)
    except Exception:  # noqa: BLE001 - a browser problem never affects the adapter
        return False
    _stats["opened"] += 1
    return True


def dispatch(msg: int, wparam: int, lparam: int) -> Optional[int]:
    """The adapter's window messages (None = let Windows handle it). Only a balloon click has an effect."""
    if msg == CALLBACK_MSG:
        if (int(lparam) & 0xFFFF) == NIN_BALLOONUSERCLICK:
            on_click()
        return 0
    return None


# ================================================================================================================
# Win32 bindings (built once, only on Windows)
# ================================================================================================================

def _load():
    global _api
    if _api is not None:
        return _api
    import ctypes
    from ctypes import wintypes as W

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                    ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", W.BYTE * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT), ("uCallbackMessage", W.UINT),
                    ("hIcon", W.HICON), ("szTip", W.WCHAR * 128), ("dwState", W.DWORD), ("dwStateMask", W.DWORD),
                    ("szInfo", W.WCHAR * 256), ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

    for fn, args, res in ((user32.DefWindowProcW, [W.HWND, W.UINT, W.WPARAM, W.LPARAM], LRESULT),
                          (user32.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], W.ATOM),
                          (user32.UnregisterClassW, [W.LPCWSTR, W.HINSTANCE], W.BOOL),
                          (user32.CreateWindowExW, [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                                    ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID], W.HWND),
                          (user32.DestroyWindow, [W.HWND], W.BOOL), (user32.IsWindow, [W.HWND], W.BOOL),
                          (user32.PostMessageW, [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL),
                          (user32.GetMessageW, [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT], W.BOOL),
                          (user32.TranslateMessage, [ctypes.POINTER(W.MSG)], W.BOOL),
                          (user32.DispatchMessageW, [ctypes.POINTER(W.MSG)], LRESULT),
                          (user32.PostQuitMessage, [ctypes.c_int], None),
                          (user32.LoadIconW, [W.HINSTANCE, W.LPVOID], W.HICON),
                          (shell32.Shell_NotifyIconW, [W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], W.BOOL),
                          (shell32.SetCurrentProcessExplicitAppUserModelID, [W.LPCWSTR], ctypes.c_long),
                          (kernel32.GetModuleHandleW, [W.LPCWSTR], W.HMODULE)):
        fn.argtypes, fn.restype = args, res

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            r = dispatch(msg, wparam, lparam)
            if r is not None:
                return r
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - never raise into Windows
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _api = {"ctypes": ctypes, "W": W, "user32": user32, "shell32": shell32, "NID": NOTIFYICONDATAW, "WNDCLASSW": WNDCLASSW,
            "proc": WNDPROC(wndproc), "hinst": kernel32.GetModuleHandleW(None),
            "icon": user32.LoadIconW(None, ctypes.c_void_p(32516))}                  # IDI_INFORMATION
    return _api


def _nid(api, hwnd, flags: int):
    n = api["NID"]()
    n.cbSize = api["ctypes"].sizeof(n)
    n.hWnd, n.uID, n.uFlags, n.uCallbackMessage, n.hIcon = hwnd, 1, flags, CALLBACK_MSG, api["icon"]
    n.szTip = TIP
    return n


# ================================================================================================================
# the one adapter thread: window + message loop
# ================================================================================================================

def _loop(api, ready: threading.Event) -> None:
    ctypes, user32 = api["ctypes"], api["user32"]
    hwnd = user32.CreateWindowExW(0, CLASS_NAME, "Stock Agent", 0, 0, 0, 0, 0, HWND_MESSAGE, None, api["hinst"], None)
    if not hwnd:
        _st["error"] = f"CREATE_WINDOW_{ctypes.get_last_error()}"
        ready.set()
        return
    _st["hwnd"] = hwnd
    ready.set()
    msg = api["W"].MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:          # ends with WM_QUIT (or an error)
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    _st["hwnd"] = None


def start() -> bool:
    """Start the adapter (idempotent): identity, window class, the message-loop thread and its hidden window."""
    if sys.platform != "win32":
        return False
    with _lock:
        t = _st["thread"]
        if t is not None and t.is_alive() and _st["hwnd"]:
            return True
        api = _load()
        if not _st["branded"] and ID.registered():                       # before this process creates its window
            _st["branded"] = api["shell32"].SetCurrentProcessExplicitAppUserModelID(ID.APP_ID) == 0
        if not _st["class"]:
            wc = api["WNDCLASSW"](lpfnWndProc=api["proc"], hInstance=api["hinst"], lpszClassName=CLASS_NAME)
            if not api["user32"].RegisterClassW(api["ctypes"].byref(wc)) and api["ctypes"].get_last_error() != 1410:
                raise OSError(api["ctypes"].get_last_error(), "RegisterClassW failed")
            _st["class"] = True
        ready = threading.Event()
        _st.update(error=None, hwnd=None, ready=ready)
        t = threading.Thread(target=_loop, args=(api, ready), name=THREAD_NAME, daemon=True)
        _st["thread"] = t
        t.start()
        ready.wait(5.0)
        if _st["error"] or not _st["hwnd"]:
            raise OSError(0, str(_st["error"] or "the adapter window did not start"))
        _stats["starts"] += 1
        return True


def shutdown(timeout: float = 5.0) -> None:
    """Remove the icon, destroy the window, stop the message loop, join the thread, unregister the class."""
    if sys.platform != "win32" or _api is None:
        return
    with _lock:
        api, hwnd, t = _api, _st["hwnd"], _st["thread"]
        for h, _uid in list(_icons):
            try:
                api["shell32"].Shell_NotifyIconW(NIM_DELETE, api["ctypes"].byref(_nid(api, h, 0)))
            except Exception:  # noqa: BLE001
                pass
        _icons.clear()
        if hwnd:
            api["user32"].PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if t is not None:
            t.join(timeout)
        if _st["class"] and (t is None or not t.is_alive()):
            api["user32"].UnregisterClassW(CLASS_NAME, api["hinst"])
            _st["class"] = False
        _st.update(thread=None, hwnd=None, ready=None)
        _stats["stops"] += 1


def refresh_identity() -> None:
    """The identity registration changed: the next notification restarts the adapter under the current identity."""
    shutdown()


def send(title: str, body: str) -> Dict[str, Optional[str]]:
    """Show one local desktop notification (clicking it opens the local Daily Brief). Never raises; never retries."""
    if sys.platform != "win32":
        return {"status": "UNSUPPORTED_PLATFORM", "error_code": "NOT_WINDOWS"}
    try:
        with _lock:
            start()
            api, hwnd = _api, _st["hwnd"]
            by = api["ctypes"].byref
            if not _icons:
                n = _nid(api, hwnd, NIF_MESSAGE | NIF_ICON | NIF_TIP)
                if not api["shell32"].Shell_NotifyIconW(NIM_ADD, by(n)):
                    return {"status": "FAILED", "error_code": f"SHELL_NOTIFY_ADD_{api['ctypes'].get_last_error()}"}
                n.uVersion = NOTIFYICON_VERSION_4
                api["shell32"].Shell_NotifyIconW(NIM_SETVERSION, by(n))
                _icons.append((hwnd, 1))
            n = _nid(api, hwnd, NIF_INFO | NIF_ICON | NIF_TIP | NIF_MESSAGE)
            n.szInfoTitle = str(title)[:TITLE_MAX]
            n.szInfo = str(body)[:BODY_MAX]
            n.dwInfoFlags = NIIF_INFO
            if not api["shell32"].Shell_NotifyIconW(NIM_MODIFY, by(n)):
                _icons.clear()                                            # e.g. Explorer restarted: add it again next time
                return {"status": "FAILED", "error_code": f"SHELL_NOTIFY_{api['ctypes'].get_last_error()}"}
            return {"status": "DELIVERED", "error_code": None}
    except Exception as exc:  # noqa: BLE001 - delivery is best effort and must never affect anything else
        return {"status": "FAILED", "error_code": type(exc).__name__}


def capability() -> dict:
    windows = sys.platform == "win32"
    reg = ID.registered() if windows else False
    url = activation_url()
    return {"platform": "Windows" if windows else sys.platform, "click_supported": windows and valid_activation(url),
            "activation_url": url if valid_activation(url) else None, "branding_supported": windows,
            "branding_active": bool(windows and reg), "app_id": ID.APP_ID, "display_name": ID.DISPLAY_NAME,
            "identity_registered": reg, "source_shown": ID.DISPLAY_NAME if (windows and reg) else "Python",
            "branding": "BRANDED" if (windows and reg) else ("BRANDING_DEFERRED" if windows else "UNSUPPORTED_PLATFORM"),
            "adapter_running": bool(_st["thread"] is not None and _st["thread"].is_alive())}


atexit.register(shutdown)
