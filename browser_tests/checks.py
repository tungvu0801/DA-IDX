"""
browser_tests/checks.py — the harness's assertion and result layer (pure: no browser, no server; unit-tested).

A run FAILS (exit code 1) if any check failed, any flow raised, any external call was blocked, or the page produced an
unexpected JS error, unhandled promise rejection, console error or HTTP 5xx. Expected error scenarios must be listed
explicitly (none are needed today: a fake provider failure is an HTTP 200 "Explanation unavailable" answer).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

EXPECTED_HTTP_ERRORS: tuple = ()          # (status, url substring) pairs a flow may legitimately produce
EXPECTED_JS_ERRORS: tuple = ()            # substrings of expected JS exception texts


class ExternalCallBlocked(RuntimeError):
    """Raised as soon as the guard recorded a real external call: the run stops immediately."""


@dataclass
class Check:
    flow: str
    name: str
    ok: bool
    detail: Optional[str] = None


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)          # flow exceptions
    js_errors: List[str] = field(default_factory=list)
    console_errors: List[str] = field(default_factory=list)
    http_errors: List[list] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    screenshots: List[str] = field(default_factory=list)
    flow: str = ""
    started: float = field(default_factory=time.time)

    def check(self, name: str, ok, detail=None) -> bool:
        self.checks.append(Check(self.flow, name, bool(ok), None if detail is None else str(detail)[:600]))
        return bool(ok)

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got {got!r}, want {want!r}")

    def error(self, flow: str, exc: BaseException):
        self.errors.append({"flow": flow, "error": f"{type(exc).__name__}: {exc}"[:800]})

    def absorb_page(self, js_errors, console_errors, http_errors):
        self.js_errors = [e for e in js_errors if not any(x in (e or "") for x in EXPECTED_JS_ERRORS)]
        self.console_errors = list(console_errors)
        self.http_errors = [list(h) for h in http_errors if not any(h[0] == s and u in h[1] for s, u in EXPECTED_HTTP_ERRORS)]

    @property
    def failed_checks(self) -> List[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return not (self.failed_checks or self.errors or self.js_errors or self.console_errors or self.http_errors
                    or self.violations) and bool(self.checks)

    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def summary(self) -> str:
        lines = [f"browser harness: {'PASS' if self.ok else 'FAIL'} — {len(self.checks)} checks, "
                 f"{len(self.failed_checks)} failed, {len(self.errors)} flow errors, {len(self.js_errors)} JS errors, "
                 f"{len(self.console_errors)} console errors, {len(self.http_errors)} HTTP 5xx, "
                 f"{len(self.violations)} blocked external calls · {time.time() - self.started:.1f} s"]
        lines += [f"  FAILED [{c.flow}] {c.name}: {c.detail}" for c in self.failed_checks]
        lines += [f"  ERROR  [{e['flow']}] {e['error']}" for e in self.errors]
        lines += [f"  JS     {e}" for e in self.js_errors] + [f"  CONSOLE {e}" for e in self.console_errors]
        lines += [f"  HTTP   {s} {u}" for s, u in self.http_errors] + [f"  BLOCKED {v}" for v in self.violations]
        if not self.checks:
            lines.append("  no checks ran")
        return "\n".join(lines)

    def write(self, path: Path):
        d = asdict(self)
        d["ok"], d["exit_code"], d["runtime_s"] = self.ok, self.exit_code(), round(time.time() - self.started, 1)
        path.write_text(json.dumps(d, indent=1), encoding="utf-8")
