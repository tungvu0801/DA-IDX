"""
portfolio/provider.py — Portfolio data provider abstraction (Stage 2.7C).

The stock-agent ONLY ever talks to the local rh_gateway over loopback HTTP.
It has no MCP client, no broker tool names, no OAuth tokens and no account
numbers — only an account ALIAS (config.PORTFOLIO_ACCOUNT_ALIAS) and the
gateway's local API secret. Every failure surfaces as PortfolioUnavailable
with a reason code; nothing is fabricated and stale data is never silent
(the gateway marks it STALE and that status is passed through).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

import requests

import config
from portfolio.gateway_secret import GatewaySecretUnavailable, load_gateway_secret

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-RH-Gateway-Secret"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


class PortfolioUnavailable(Exception):
    """reason: DISABLED | GATEWAY_DOWN | GATEWAY_SECRET_UNAVAILABLE | REAUTH_REQUIRED | UPSTREAM_ERROR |
    DATA_UNAVAILABLE | GATEWAY_REJECTED"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class GatewayResult:
    status: str                       # OK | STALE
    data: Any
    fetched_at: Optional[str]
    cache_age_s: Optional[int]
    truncated: bool
    message: Optional[str]
    components: Optional[dict] = None


class PortfolioProvider(ABC):
    @abstractmethod
    def get_accounts(self) -> GatewayResult: ...

    @abstractmethod
    def get_portfolio(self) -> GatewayResult: ...

    @abstractmethod
    def get_positions(self) -> GatewayResult: ...

    @abstractmethod
    def get_tax_lots(self, symbol: str) -> GatewayResult: ...

    @abstractmethod
    def get_orders(self, since: Optional[str] = None) -> GatewayResult: ...

    @abstractmethod
    def get_realized_pnl(self, span: str) -> GatewayResult: ...

    @abstractmethod
    def get_pnl_history(self, span: str) -> GatewayResult: ...


class UnavailablePortfolioProvider(PortfolioProvider):
    """Used when portfolio awareness is disabled. Every call reports PORTFOLIO_UNAVAILABLE."""

    def __init__(self, reason: str = "DISABLED",
                 message: str = "Portfolio awareness is turned off (PORTFOLIO_AWARENESS_ENABLED=false)."):
        self.reason, self.message = reason, message

    def _fail(self):
        raise PortfolioUnavailable(self.reason, self.message)

    def get_accounts(self): self._fail()
    def get_portfolio(self): self._fail()
    def get_positions(self): self._fail()
    def get_tax_lots(self, symbol): self._fail()
    def get_orders(self, since=None): self._fail()
    def get_realized_pnl(self, span): self._fail()
    def get_pnl_history(self, span): self._fail()


def require_loopback(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("RH_GATEWAY_URL must be http://127.0.0.1:<port> (loopback only)")
    return url.rstrip("/")


class RobinhoodGatewayPortfolioProvider(PortfolioProvider):
    def __init__(self, base_url: str, account_alias: str, *, timeout: float = 30.0,
                 secret_loader: Callable[[], str] = load_gateway_secret, http: Any = requests):
        self._base = require_loopback(base_url)
        if not account_alias.isidentifier():
            raise ValueError("PORTFOLIO_ACCOUNT_ALIAS must be a short identifier")
        self._alias = account_alias
        self._timeout = timeout
        self._secret_loader = secret_loader
        self._http = http

    def _get(self, path: str, params: Optional[dict] = None) -> GatewayResult:
        try:
            secret = self._secret_loader()
        except GatewaySecretUnavailable as exc:
            raise PortfolioUnavailable("GATEWAY_SECRET_UNAVAILABLE", str(exc)) from None
        try:
            resp = self._http.get(self._base + path, params=params or None, headers={SECRET_HEADER: secret},
                                  timeout=self._timeout, allow_redirects=False)
        except requests.RequestException:
            raise PortfolioUnavailable("GATEWAY_DOWN", "The local Robinhood gateway is not reachable. Start it "
                                       "with: python -m rh_gateway serve") from None
        try:
            env = resp.json()
        except ValueError:
            raise PortfolioUnavailable("UPSTREAM_ERROR", f"Gateway returned an unreadable response "
                                       f"(HTTP {resp.status_code}).") from None
        status = env.get("status") if isinstance(env, dict) else None
        if resp.status_code in (401, 403):
            raise PortfolioUnavailable("GATEWAY_REJECTED", "The gateway rejected this app's request "
                                       "(secret or host check).")
        if status in ("OK", "STALE"):
            return GatewayResult(status=status, data=env.get("data"), fetched_at=env.get("fetched_at"),
                                 cache_age_s=env.get("cache_age_s"), truncated=bool(env.get("truncated")),
                                 message=env.get("message"), components=env.get("components"))
        if status in ("REAUTH_REQUIRED", "UPSTREAM_ERROR", "DATA_UNAVAILABLE"):
            raise PortfolioUnavailable(status, env.get("message") or status)
        raise PortfolioUnavailable("UPSTREAM_ERROR", f"Unexpected gateway response (HTTP {resp.status_code}).")

    def _acct(self) -> dict:
        return {"account": self._alias}

    def get_accounts(self):
        return self._get("/accounts")

    def get_portfolio(self):
        return self._get("/portfolio", self._acct())

    def get_positions(self):
        return self._get("/positions", self._acct())

    def get_tax_lots(self, symbol: str):
        return self._get(f"/tax-lots/{quote(symbol, safe='')}", self._acct())

    def get_orders(self, since: Optional[str] = None):
        return self._get("/orders", {**self._acct(), **({"since": since} if since else {})})

    def get_realized_pnl(self, span: str):
        return self._get("/realized-pnl", {**self._acct(), "span": span})

    def get_pnl_history(self, span: str):
        return self._get("/pnl-history", {**self._acct(), "span": span})


def get_portfolio_provider() -> PortfolioProvider:
    if not config.PORTFOLIO_AWARENESS_ENABLED:
        return UnavailablePortfolioProvider()
    try:
        return RobinhoodGatewayPortfolioProvider(config.RH_GATEWAY_URL, config.PORTFOLIO_ACCOUNT_ALIAS,
                                                 timeout=config.RH_GATEWAY_TIMEOUT_SECONDS)
    except ValueError as exc:
        return UnavailablePortfolioProvider("DATA_UNAVAILABLE", f"Portfolio configuration invalid: {exc}")
