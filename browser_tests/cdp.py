"""
browser_tests/cdp.py — a minimal synchronous Chrome DevTools Protocol client for headless Microsoft Edge.

The browser is launched with a FIXED argument list (no shell, no user input), its own temporary profile and a local
debugging port; close() always terminates it (kill after a grace period). Events are buffered while commands run:
JS exceptions (incl. unhandled promise rejections), console errors and HTTP responses (for 5xx detection).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

EDGE_CANDIDATES = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                   r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")


def find_edge() -> str:
    env = os.environ.get("BROWSER_HARNESS_EDGE")
    for p in ((env,) if env else ()) + EDGE_CANDIDATES:
        if p and Path(p).is_file():
            return p
    raise RuntimeError("Microsoft Edge was not found (set BROWSER_HARNESS_EDGE to msedge.exe)")


class Browser:
    def __init__(self, port: int, profile: Path, width: int = 1920, height: int = 1080):
        self.port, self.profile, self.proc, self.ws = port, profile, None, None
        self.width, self.height = width, height
        self.n, self.events = 0, []

    # ---- lifecycle ----------------------------------------------------------------------------------------------------
    def launch(self, timeout: float = 30.0):
        args = [find_edge(), "--headless=new", "--disable-gpu", f"--remote-debugging-port={self.port}", "--hide-scrollbars",
                f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-component-update", "--disable-sync", "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # noqa: S603 - fixed args
        from websockets.sync.client import connect
        end = time.time() + timeout
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=2) as r:
                    page = next(t for t in json.load(r) if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001 - the browser is still starting
                if time.time() > end:
                    raise RuntimeError("headless Edge did not start")
                time.sleep(0.3)
        # unbounded queue + no keep-alive: event bursts while the harness waits must never stall or drop the connection
        self.ws = connect(page["webSocketDebuggerUrl"], max_size=2 ** 27, max_queue=None, open_timeout=10, ping_interval=None)
        for d in ("Page", "Runtime", "Network", "Log"):
            self.send(f"{d}.enable")
        self.viewport(self.width, self.height)

    def close(self):
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:  # noqa: BLE001 - closing regardless
            pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)

    # ---- protocol -----------------------------------------------------------------------------------------------------
    def send(self, method: str, params: Optional[dict] = None, timeout: float = 120.0) -> dict:
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while True:
            msg = json.loads(self.ws.recv(timeout=max(0.1, end - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError(f"JS error in harness expression: {d.get('exception', {}).get('description') or d.get('text')}")
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 30.0, what: str = "") -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return True
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for {what or expr}")

    def viewport(self, width: int, height: int):
        self.width, self.height = width, height
        self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

    def navigate(self, url: str):
        self.send("Page.navigate", {"url": url})

    def screenshot(self, path: Path, selector: Optional[str] = None, max_height: int = 6000):
        p = {"format": "png"}
        if selector:
            r = self.js("(() => { const e = document.querySelector(" + json.dumps(selector) + "); if (!e) return null; "
                        "e.scrollIntoView({block: 'start'}); const b = e.getBoundingClientRect(); "
                        "return [b.left + scrollX, b.top + scrollY, b.width, b.height]; })()")
            if r and r[2] > 0 and r[3] > 0:
                p.update({"captureBeyondViewport": True,
                          "clip": {"x": r[0], "y": r[1], "width": r[2], "height": min(r[3], max_height), "scale": 1}})
        res = self.send("Page.captureScreenshot", p)
        path.write_bytes(base64.b64decode(res["data"]))

    # ---- event summaries ----------------------------------------------------------------------------------------------
    def drain(self):
        """Read any buffered events without sending a real command."""
        self.js("0")

    def js_errors(self) -> list:
        out = []
        for e in self.events:
            if e.get("method") == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append((d.get("exception") or {}).get("description") or d.get("text"))
        return out

    def console_errors(self) -> list:
        return [" ".join(str(a.get("value", a.get("description", ""))) for a in e["params"].get("args", []))
                for e in self.events if e.get("method") == "Runtime.consoleAPICalled" and e["params"].get("type") == "error"]

    def http_errors(self) -> list:
        return [(e["params"]["response"]["status"], e["params"]["response"]["url"]) for e in self.events
                if e.get("method") == "Network.responseReceived" and e["params"]["response"]["status"] >= 500]
