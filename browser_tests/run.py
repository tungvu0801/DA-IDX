"""
browser_tests/run.py — ONE command for the repeatable browser regression harness.

    cd stock-agent
    .venv\\Scripts\\python.exe -m browser_tests.run [--out DIR] [--only flow1,flow2] [--inject-failure]

Fresh temporary state every run (scratch database, Edge profile); the real FastAPI app with fake market data, a fake
Claude provider and a fake broker; fixed clock; headless Edge at 1920x1080 and 1400x900. Screenshots, results.json and
harness.log go to --out (default browser_tests/artifacts/; only files the harness itself writes there are replaced).
Exit code 0 = pass, 1 = any failure.
Server, browser and threads are always stopped (try/finally), even when a flow fails.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def parse(argv):
    p = argparse.ArgumentParser(prog="python -m browser_tests.run", description="Stock Agent browser regression harness")
    p.add_argument("--out", default=str(HERE / "artifacts"), help="artifact directory (cleared first)")
    p.add_argument("--only", default="", help="comma-separated flow names (dashboard always runs first)")
    p.add_argument("--inject-failure", action="store_true", help="self-test: add a check that must fail (exit code 1)")
    return p.parse_args(argv)


def run(argv=None) -> int:
    args = parse(argv if argv is not None else sys.argv[1:])
    for p in (str(ROOT / "tests"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from browser_tests import app_server as AS
    from browser_tests import flows as F
    from browser_tests.cdp import Browser
    from browser_tests.checks import ExternalCallBlocked, Report

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("*.png"), out / "results.json", out / "harness.log"]:     # only files this harness writes
        if old.is_file():
            old.unlink()
    logging.basicConfig(filename=str(out / "harness.log"), level=logging.WARNING, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tmp = Path(tempfile.mkdtemp(prefix="stock-agent-browser-"))
    rep, guard = Report(), AS.Guard()
    server = browser = None
    t0 = time.perf_counter()
    try:
        app, world, ai, broker = AS.prepare(tmp, guard)
        rep.timings["world_build_s"] = round(time.perf_counter() - t0, 1)
        port, cport = AS.free_port(), AS.free_port()
        guard.allowed_ports |= {port, cport}
        server = AS.Server(app, port)
        server.start()
        browser = Browser(cport, tmp / "edge-profile")
        browser.launch()
        ctx = F.Ctx(browser, rep, f"http://127.0.0.1:{port}", out, ai, world, guard, broker)
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        flows = [f for f in F.FLOWS if not only or f.__name__ in only or f is F.dashboard]
        for flow in flows:
            rep.flow = flow.__name__
            f0 = time.perf_counter()
            try:
                flow(ctx)
            except ExternalCallBlocked as exc:
                rep.error(flow.__name__, exc)
                break                                          # a real external call stops the run immediately
            except Exception as exc:  # noqa: BLE001 - record and continue with the next flow
                rep.error(flow.__name__, exc)
                try:
                    ctx.shot(f"FAILED_{flow.__name__}")
                except Exception:  # noqa: BLE001
                    pass
            rep.timings[f"flow_{flow.__name__}_s"] = round(time.perf_counter() - f0, 1)
        if args.inject_failure:
            rep.flow = "self_test"
            rep.check("self-test: an element that does not exist is present", browser.js("!!document.querySelector('#harness-self-test-missing')"))
        rep.check("broker gateway: read-only GETs only", all(str(r).upper().startswith("GET") or "GET" in str(r).upper()[:8]
                                                            for r in getattr(broker, "requests", [])), getattr(broker, "requests", [])[:5])
        browser.drain()
        rep.absorb_page(browser.js_errors(), browser.console_errors(), browser.http_errors())
    except Exception as exc:  # noqa: BLE001 - setup failure is a harness failure
        rep.error("setup", exc)
    finally:
        if browser is not None:
            browser.close()
        if server is not None:
            rep.timings["server_stopped"] = server.stop()
        guard.uninstall()
        rep.violations = list(guard.violations)
        rep.timings["total_runtime_s"] = round(time.perf_counter() - t0, 1)
        rep.write(out / "results.json")
        shutil.rmtree(tmp, ignore_errors=True)
    print(rep.summary())
    print(f"artifacts: {out}")
    return rep.exit_code()


if __name__ == "__main__":
    sys.exit(run())
