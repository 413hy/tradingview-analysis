"""Binance USDT-M Demo only. Signed traffic cannot be redirected to production."""

import hashlib
import hmac
import time
from urllib.parse import urlencode
import httpx
from common.http import client as routed_client
from common.config import settings
from trader.risk import Instrument, number as D

DEMO = "https://demo-fapi.binance.com"


class ExchangeError(RuntimeError):
    def __init__(self, code, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(f"Binance Demo error {code}; uncertain={uncertain}")


class Exchange:
    def __init__(self, client=None):
        self.client = client or routed_client(base_url=DEMO, timeout=15, follow_redirects=False)
        if str(self.client.base_url).rstrip("/") != DEMO:
            raise ValueError("Only Binance Demo private endpoint is allowed")
        self.offset = 0
        self.cooldown = 0

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, params=None, signed=False):
        if not path.startswith("/fapi/") or "://" in path:
            raise ValueError("Invalid Binance API path")
        if time.time() < self.cooldown:
            raise ExchangeError(429)
        if method != "GET" and not settings.trading_enabled:
            raise ValueError("TRADING_ENABLED=false")
        query = dict(params or {})
        headers = {}
        if signed:
            key = settings.binance_demo_api_key.get_secret_value()
            secret = settings.binance_demo_api_secret.get_secret_value()
            if not key or not secret:
                raise ValueError("Binance Demo credentials missing")
            query.update(timestamp=int(time.time() * 1000) + self.offset, recvWindow=5000)
            encoded = urlencode(query)
            signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = key
            encoded += "&signature=" + signature
        else:
            encoded = urlencode(query)
        try:
            r = await self.client.request(method, path, params=encoded, headers=headers)
            if r.status_code in (418, 429):
                self.cooldown = time.time() + max(60, int(r.headers.get("retry-after", "60")))
            data = r.json()
        except (httpx.HTTPError, ValueError):
            raise ExchangeError("NETWORK_OR_JSON", uncertain=method != "GET") from None
        if r.status_code >= 400 or (isinstance(data, dict) and int(data.get("code", 0)) < 0):
            raise ExchangeError(
                data.get("code", r.status_code) if isinstance(data, dict) else r.status_code,
                uncertain=(
                    r.status_code >= 500
                    or r.status_code == 408
                    or (isinstance(data, dict) and data.get("code") in (-1006, -1007, -4116))
                ),
            )
        return data

    async def sync(self):
        start = int(time.time() * 1000)
        data = await self.request("GET", "/fapi/v1/time")
        self.offset = int(data["serverTime"]) - (start + int(time.time() * 1000)) // 2

    async def positions(self, symbol=None):
        return await self.request("GET", "/fapi/v3/positionRisk", {"symbol": symbol} if symbol else {}, True)

    async def account(self):
        return await self.request("GET", "/fapi/v3/account", signed=True)

    async def instrument(self, symbol):
        doc = await self.request("GET", "/fapi/v1/exchangeInfo")
        row = next((r for r in doc["symbols"] if r["symbol"] == symbol), None)
        if (
            not row
            or row["status"] != "TRADING"
            or row["contractType"] != "PERPETUAL"
            or row["quoteAsset"] != "USDT"
        ):
            raise ValueError("SKIP_UNSUPPORTED_DEMO_SYMBOL")
        f = {r["filterType"]: r for r in row["filters"]}
        market = f.get("MARKET_LOT_SIZE", f["LOT_SIZE"])
        brackets = await self.request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, True)
        lev = max(int(b["initialLeverage"]) for b in brackets[0]["brackets"])
        # Quantity must respect BOTH limit (TP) and market (entry/SL) grids.
        from decimal import Decimal
        import math

        a, b = D(f["LOT_SIZE"]["stepSize"]), D(market["stepSize"])
        scale = 10 ** max(-a.as_tuple().exponent, -b.as_tuple().exponent)
        step = Decimal(math.lcm(int(a * scale), int(b * scale))) / scale
        return Instrument(
            symbol,
            D(f["PRICE_FILTER"]["tickSize"]),
            step,
            max(D(f["LOT_SIZE"]["minQty"]), D(market["minQty"])),
            D(f.get("MIN_NOTIONAL", {}).get("notional", "0")),
            D(market["maxQty"]),
            D(f["LOT_SIZE"]["maxQty"]),
            D(lev),
            D(1),
        )

    async def quote(self, symbol):
        row = await self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        bid, ask = D(row["bidPrice"]), D(row["askPrice"])
        if row.get("symbol") != symbol or not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            raise ValueError("Invalid quote")
        return bid, ask

    async def fees(self, symbol):
        row = await self.request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, True)
        return D(row["takerCommissionRate"]), D(row["makerCommissionRate"])

    async def configure(self, symbol, leverage):
        mode = await self.request("GET", "/fapi/v1/positionSide/dual", signed=True)
        if mode["dualSidePosition"]:
            raise ValueError("One-way position mode required; not changed automatically")
        if any(D(p["positionAmt"]) for p in await self.positions(symbol)):
            raise ValueError("SKIP_EXISTING_POSITION")
        try:
            await self.request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSSED"}, True
            )
        except ExchangeError as exc:
            if exc.code != -4046:
                raise
        r = await self.request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, True
        )
        if int(r["leverage"]) != int(leverage):
            raise ValueError("Leverage verification failed")

    async def lookup(self, kind, symbol, client_id):
        path = "/fapi/v1/algoOrder" if kind == "sl" else "/fapi/v1/order"
        params = (
            {"clientAlgoId": client_id}
            if kind == "sl"
            else {"symbol": symbol, "origClientOrderId": client_id}
        )
        try:
            return await self.request("GET", path, params, True)
        except ExchangeError as exc:
            if exc.code in (-2013, -2011):
                return None
            raise

    async def submit(self, kind, payload):
        row = await self.request(
            "POST", "/fapi/v1/algoOrder" if kind == "sl" else "/fapi/v1/order", payload, True
        )
        # HTTP 200 without an order identity is not evidence of a rejected order.
        field = "algoId" if kind == "sl" else "orderId"
        cid_field = "clientAlgoId" if kind == "sl" else "clientOrderId"
        expected = payload.get("clientAlgoId", payload.get("newClientOrderId"))
        if (
            not isinstance(row, dict)
            or not row.get(field)
            or row.get("symbol", payload["symbol"]) != payload["symbol"]
            or row.get(cid_field, expected) != expected
        ):
            raise ExchangeError("INVALID_ORDER_ACK", uncertain=True)
        return row

    async def fills(self, symbol, order_id):
        found, cursor = {}, None
        for _ in range(100):
            params = {"symbol": symbol, "orderId": order_id, "limit": 1000}
            if cursor is not None:
                params["fromId"] = cursor
            rows = await self.request("GET", "/fapi/v1/userTrades", params, True)
            for row in rows:
                if str(row["orderId"]) != str(order_id) or row.get("symbol", symbol) != symbol:
                    raise ValueError("Fill identity mismatch")
                found[str(row["id"])] = row
            if len(rows) < 1000:
                return list(found.values())
            next_cursor = max(int(r["id"]) for r in rows) + 1
            if cursor is not None and next_cursor <= cursor:
                raise ValueError("Fill pagination did not advance")
            cursor = next_cursor
        raise ValueError("Fill pagination limit exceeded")

    async def cancel(self, kind, symbol, client_id):
        params = (
            {"clientAlgoId": client_id}
            if kind == "sl"
            else {"symbol": symbol, "origClientOrderId": client_id}
        )
        try:
            return await self.request(
                "DELETE", "/fapi/v1/algoOrder" if kind == "sl" else "/fapi/v1/order", params, True
            )
        except ExchangeError as exc:
            if exc.code not in (-2011, -2013):
                raise
