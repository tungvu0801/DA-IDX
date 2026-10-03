"""Provider adapter + gateway-secret reader. The stock-agent only ever talks to loopback rh_gateway."""
import ctypes
import json
from ctypes import wintypes

import pytest
import requests

import config
from pf_fixtures import FakeGatewayHttp, fake_provider
from portfolio import gateway_secret
from portfolio.provider import (PortfolioUnavailable, RobinhoodGatewayPortfolioProvider, UnavailablePortfolioProvider,
                                get_portfolio_provider, require_loopback)


def test_disabled_flag_gives_unavailable_provider(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)
    p = get_portfolio_provider()
    assert isinstance(p, UnavailablePortfolioProvider)
    with pytest.raises(PortfolioUnavailable) as exc:
        p.get_positions()
    assert exc.value.reason == "DISABLED"


@pytest.mark.parametrize("url", ["http://example.com:8787", "https://127.0.0.1:8787", "http://10.0.0.5:8787",
                                 "http://127.0.0.1.evil.com:8787", "ftp://127.0.0.1"])
def test_gateway_url_must_be_loopback(url):
    with pytest.raises(ValueError):
        require_loopback(url)


def test_non_loopback_config_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "RH_GATEWAY_URL", "http://attacker.example:8787")
    p = get_portfolio_provider()
    assert isinstance(p, UnavailablePortfolioProvider) and p.reason == "DATA_UNAVAILABLE"


def test_requests_carry_alias_and_secret_only():
    http = FakeGatewayHttp()
    p = fake_provider(http)
    p.get_portfolio(); p.get_positions(); p.get_tax_lots("NVDA"); p.get_orders("2026-09-01")
    p.get_realized_pnl("3month"); p.get_pnl_history("3month")
    assert [r["path"] for r in http.requests] == ["/portfolio", "/positions", "/tax-lots/NVDA", "/orders",
                                                  "/realized-pnl", "/pnl-history"]
    for r in http.requests:
        assert r["url"].startswith("http://127.0.0.1:8787/")
        assert r["params"]["account"] == "holdings"
        assert set(r["headers"]) == {"X-RH-Gateway-Secret"}


def test_ok_and_stale_pass_through():
    http = FakeGatewayHttp({"/portfolio": (200, {"status": "STALE", "data": {"x": 1}, "fetched_at": "t",
                                                "cache_age_s": 120, "truncated": False, "message": "stale"})})
    r = fake_provider(http).get_portfolio()
    assert r.status == "STALE" and r.cache_age_s == 120 and r.message == "stale"


@pytest.mark.parametrize("code,status,reason", [(503, "REAUTH_REQUIRED", "REAUTH_REQUIRED"),
                                                (502, "UPSTREAM_ERROR", "UPSTREAM_ERROR"),
                                                (404, "DATA_UNAVAILABLE", "DATA_UNAVAILABLE"),
                                                (401, "DATA_UNAVAILABLE", "GATEWAY_REJECTED"),
                                                (500, "WEIRD", "UPSTREAM_ERROR")])
def test_gateway_failures_become_portfolio_unavailable(code, status, reason):
    http = FakeGatewayHttp({"/positions": (code, {"status": status, "data": None, "message": "m"})})
    with pytest.raises(PortfolioUnavailable) as exc:
        fake_provider(http).get_positions()
    assert exc.value.reason == reason


def test_gateway_down():
    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    with pytest.raises(PortfolioUnavailable) as exc:
        fake_provider(Down()).get_portfolio()
    assert exc.value.reason == "GATEWAY_DOWN"


def test_missing_secret_makes_no_request():
    http = FakeGatewayHttp()

    def no_secret():
        raise gateway_secret.GatewaySecretUnavailable("missing")
    p = RobinhoodGatewayPortfolioProvider("http://127.0.0.1:8787", "holdings", secret_loader=no_secret, http=http)
    with pytest.raises(PortfolioUnavailable) as exc:
        p.get_portfolio()
    assert exc.value.reason == "GATEWAY_SECRET_UNAVAILABLE" and http.requests == []


def test_alias_must_be_identifier():
    with pytest.raises(ValueError):
        RobinhoodGatewayPortfolioProvider("http://127.0.0.1:8787", "1234 5678")


# ---- DPAPI secret reader (dummy secret, written with the same envelope rh_gateway uses) ----------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _protect(data: bytes, entropy: bytes) -> bytes:
    inb = ctypes.create_string_buffer(data, len(data))
    enb = ctypes.create_string_buffer(entropy, len(entropy))
    bi = _Blob(len(data), ctypes.cast(inb, ctypes.POINTER(ctypes.c_char)))
    be = _Blob(len(entropy), ctypes.cast(enb, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    assert ctypes.windll.crypt32.CryptProtectData(ctypes.byref(bi), "t", ctypes.byref(be), None, None, 1,
                                                  ctypes.byref(out))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _envelope(blob: bytes, record="gateway-secret", version=1) -> bytes:
    header = json.dumps({"storage_schema_version": version, "encryption": "DPAPI_USER", "record": record},
                        separators=(",", ":")).encode()
    return b"RHGWENC1" + len(header).to_bytes(4, "big") + header + blob


def test_secret_reader_round_trip(tmp_path):
    secret = "dummy-local-secret-" + "q" * 30
    p = tmp_path / "gateway-secret.dpapi"
    p.write_bytes(_envelope(_protect(secret.encode(), b"rh_gateway/credential/v1:gateway-secret")))
    assert gateway_secret.load_gateway_secret(p) == secret
    assert secret.encode() not in p.read_bytes()


@pytest.mark.parametrize("variant", ["wrong_entropy", "wrong_record", "wrong_version", "garbage", "missing"])
def test_secret_reader_fails_closed(tmp_path, variant):
    p = tmp_path / "gateway-secret.dpapi"
    good_entropy = b"rh_gateway/credential/v1:gateway-secret"
    if variant == "wrong_entropy":
        p.write_bytes(_envelope(_protect(b"s", b"other")))
    elif variant == "wrong_record":
        p.write_bytes(_envelope(_protect(b"s", good_entropy), record="oauth-tokens"))
    elif variant == "wrong_version":
        p.write_bytes(_envelope(_protect(b"s", good_entropy), version=2))
    elif variant == "garbage":
        p.write_bytes(b"not an envelope")
    with pytest.raises(gateway_secret.GatewaySecretUnavailable):
        gateway_secret.load_gateway_secret(p)
