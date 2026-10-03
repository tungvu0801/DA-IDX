"""Gateway-shaped (already normalized) dummy data. Only masked ids — the stock-agent never sees full numbers.

Hand-checkable numbers:
  NVDA 2 @ 111 (extended, newest)  cost 100 -> value 222, basis 200, +22  (+11%)
  SNDK 1 @ 450                      cost 500 -> value 450, basis 500, -50  (-10%)
  SHOP 3 @ 102 (all bought today)   cost 100 -> value 306, basis 300, +6   (+2%), day change partial
  MU   1 @ 12  crossed bid/ask      cost ??  -> value 12, basis unavailable
  positions 990 + cash 10 = calculated total 1000; Robinhood equity 995 -> gap +5 (0.50%)
"""
from datetime import datetime, timezone

NOW = datetime(2026, 9, 28, 0, 5, 0, tzinfo=timezone.utc)
MASKED = "••••3450"


def quote(symbol, price, *, reg_price=None, reg_time="2026-09-25T20:00:00Z", ext_time="2026-09-28T00:00:00.123456789Z",
          bid=None, ask=None, has_traded=True, state="active", prev="100.00"):
    return {"symbol": symbol, "last_trade_price": reg_price, "venue_last_trade_time": reg_time,
            "last_non_reg_trade_price": price, "venue_last_non_reg_trade_time": ext_time,
            "adjusted_previous_close": prev, "previous_close": prev, "previous_close_date": "2026-09-25",
            "bid_price": bid, "venue_bid_time": ext_time, "ask_price": ask, "venue_ask_time": ext_time,
            "has_traded": has_traded, "state": state,
            "official_close": {"date": "2026-09-25", "price": prev, "interpolated": False, "source": "sip"},
            "gateway_fetched_at": "2026-09-28T00:04:59+00:00", "gateway_cache_age_s": 1}


def position(symbol, qty, avg, intraday="0.000000"):
    return {"symbol": symbol, "quantity": qty, "intraday_quantity": intraday, "average_buy_price": avg,
            "shares_available_for_sells": qty, "type": "long", "shares_held_for_sells": "0.000000",
            "shares_held_for_stock_grants": "0.000000", "shares_held_for_options_events": "0.000000",
            "shares_held_for_asset_transfer": "0.000000", "shares_pending_from_options_events": "0.000000"}


PORTFOLIO = {"account": {"alias": "holdings", "masked_id": MASKED}, "total_value": "1005.00", "equity_value": "995.00",
             "cash": "10.00", "buying_power": "10.0000", "unleveraged_buying_power": "10.0000",
             "crypto_buying_power": "10.0000", "options_value": "0", "crypto_value": "0", "futures_value": "0",
             "event_contracts_value": "0", "mutual_funds_value": "0", "fixed_income_value": "0",
             "pending_deposits": "0", "currency": "USD"}

POSITIONS = {
    "account": {"alias": "holdings", "masked_id": MASKED},
    "positions": [position("NVDA", "2.000000", "100.000000"), position("SNDK", "1.000000", "500.000000"),
                  position("SHOP", "3.000000", "100.000000", intraday="3.000000"), position("MU", "1.000000", None)],
    "quotes": {"NVDA": quote("NVDA", "111.00", reg_price="110.00"),
               "SNDK": quote("SNDK", "450.00", reg_price="449.00"),
               "SHOP": quote("SHOP", "102.00", reg_price="101.00"),
               "MU": quote("MU", "12.00", reg_price="11.90", bid="12.10", ask="11.95")},
}

REALIZED = {"account": {"alias": "holdings", "masked_id": MASKED}, "span": "3month", "window": "3month",
            "display_currency": "USD", "total_returns": "5.69", "total_rate_of_return": "0.0379",
            "buckets": [{"start_time": "2026-09-21T04:00:00Z", "end_time": "2026-09-22T03:59:59Z",
                         "realized_gain": "5.69", "rate_of_realized_gain": "0.0379", "number_of_trades": 1},
                        {"start_time": "2026-09-22T04:00:00Z", "end_time": "2026-09-23T03:59:59Z",
                         "realized_gain": None, "rate_of_realized_gain": None, "number_of_trades": 0}]}

HISTORY = {"account": {"alias": "holdings", "masked_id": MASKED}, "span": "3month",
           "trades": [{"timestamp": "2026-09-21T18:26:33Z", "symbol": "LLY", "side": "sell", "quantity": "0.133254",
                       "price": "1168.37", "realized_gain": "5.69"}]}

TAX_LOTS = {"account": {"alias": "holdings", "masked_id": MASKED}, "symbol": "NVDA",
            "tax_lots": [{"lot_id": "a", "symbol": "NVDA", "open_type": "buy", "order_id": "1", "quantity": "1.000000",
                          "quantity_available": "1.000000", "selectable": True, "cost_per_share": "90.000000",
                          "cost_basis": "90.000000", "open_date": "2026-09-01", "term": "st"},
                         {"lot_id": "b", "symbol": "NVDA", "open_type": "deposit", "order_id": None,
                          "quantity": "1.000000", "quantity_available": "1.000000", "selectable": False,
                          "cost_per_share": None, "cost_basis": None, "open_date": "2025-01-10", "term": "lt"}],
            "quote": quote("NVDA", "111.00", reg_price="110.00")}

ORDERS = {"account": {"alias": "holdings", "masked_id": MASKED}, "since": "2026-09-01",
          "orders": [{"order_id": "6ab6ccae-7625-48b9-b70e-4b3eb092226a", "symbol": "NVDA", "side": "buy",
                      "order_type": "market", "trigger": "immediate", "state": "filled", "quantity": "1.000000",
                      "cumulative_quantity": "1.000000", "limit_price": None, "stop_price": None,
                      "average_price": "100.00", "fees": "0", "dollar_amount": None, "time_in_force": "gfd",
                      "market_hours": "regular_hours", "placed_agent": "user",
                      "created_at": "2026-09-25T19:34:06Z", "last_transaction_at": "2026-09-25T19:34:06Z"}]}

ACCOUNTS = [{"alias": "holdings", "masked_id": MASKED, "status": "OK", "account_type": "margin",
             "brokerage_account_type": "individual", "state": "active", "unsettled_funds": "0.0000",
             "agent_tradable": False, "is_default": True}]


def env(data, status="OK"):
    return {"status": status, "data": data, "fetched_at": "2026-09-28T00:04:59+00:00", "cache_age_s": 1,
            "provider_as_of": None, "truncated": False, "message": None}


ROUTES = {"/accounts": ACCOUNTS, "/portfolio": PORTFOLIO, "/positions": POSITIONS, "/realized-pnl": REALIZED,
          "/pnl-history": HISTORY, "/tax-lots/NVDA": TAX_LOTS, "/orders": ORDERS}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeGatewayHttp:
    """Stands in for `requests` pointed at rh_gateway. Records every request."""

    def __init__(self, overrides=None):
        self.requests = []
        self.overrides = overrides or {}

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        path = url.split("8787", 1)[1]
        self.requests.append({"url": url, "path": path, "params": params, "headers": headers})
        if path in self.overrides:
            code, body = self.overrides[path]
            return FakeResponse(code, body)
        if path not in ROUTES:
            return FakeResponse(404, {"status": "DATA_UNAVAILABLE", "data": None, "message": "not found"})
        return FakeResponse(200, env(ROUTES[path]))


def fake_provider(http=None):
    from portfolio.provider import RobinhoodGatewayPortfolioProvider
    return RobinhoodGatewayPortfolioProvider("http://127.0.0.1:8787", "holdings", secret_loader=lambda: "x" * 43,
                                             http=http or FakeGatewayHttp())
