import asyncio
import math
import statistics
import time
from signals.sources import BINANCE


def ema(values, period):
    out, value = [], values[0]
    alpha = 2 / (period + 1)
    for n in values:
        value += alpha * (n - value)
        out.append(value)
    return out


def features(rows, clock=None):
    clock = clock or time.time()
    rows = [r for r in rows if r[6] < clock * 1000]
    if len(rows) < 35:
        raise ValueError("Insufficient closed candles")
    interval = (rows[1][0] - rows[0][0]) / 1000
    if interval <= 0 or any(b[0] - a[0] != interval * 1000 for a, b in zip(rows, rows[1:])):
        raise ValueError("Missing/duplicate candle intervals")
    if clock - rows[-1][6] / 1000 > interval + 60:
        raise ValueError("Stale closed candles")
    for r in rows:
        values = [float(r[i]) for i in (1, 2, 3, 4, 5, 7)]
        if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or min(values[4:]) < 0:
            raise ValueError("Invalid OHLCV")
        if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
            raise ValueError("Invalid candle geometry")
    closes = [float(r[4]) for r in rows]
    if not all(math.isfinite(x) and x > 0 for x in closes):
        raise ValueError("Invalid candle prices")
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(0, v) for v in changes[:14]) / 14
    loss = sum(max(0, -v) for v in changes[:14]) / 14
    for v in changes[14:]:
        gain = (gain * 13 + max(0, v)) / 14
        loss = (loss * 13 + max(0, -v)) / 14
    rsi = (100 if gain else 50) if not loss else 100 - 100 / (1 + gain / loss)
    macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    hist = macd[-1] - ema(macd, 9)[-1]
    trs = [
        max(float(r[2]) - float(r[3]), abs(float(r[2]) - closes[i - 1]), abs(float(r[3]) - closes[i - 1]))
        for i, r in enumerate(rows)
        if i
    ]
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    vols = [float(r[7]) for r in rows[-21:-1]]
    sd = statistics.pstdev(vols)
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    return {
        "rsi": rsi,
        "macd_histogram": hist,
        "ema9": e9,
        "ema21": e21,
        "sma20": statistics.mean(closes[-20:]),
        "atr_pct": atr / closes[-1] * 100,
        "volume_zscore": (float(rows[-1][7]) - statistics.mean(vols)) / sd if sd else 0,
        "volume_change_pct": (float(rows[-1][7]) / float(rows[-2][7]) - 1) * 100
        if float(rows[-2][7])
        else None,
        "trend": "bullish" if closes[-1] > e9 > e21 else "bearish" if closes[-1] < e9 < e21 else "neutral",
        "last_closed_at": rows[-1][6] / 1000,
    }


def book_features(doc, price):
    bids = [(float(p), float(q)) for p, q in doc["bids"]]
    asks = [(float(p), float(q)) for p, q in doc["asks"]]
    if (
        not bids
        or not asks
        or not 0 < bids[0][0] <= asks[0][0]
        or any(not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q < 0 for p, q in bids + asks)
        or any(a[0] < b[0] for a, b in zip(bids, bids[1:]))
        or any(a[0] > b[0] for a, b in zip(asks, asks[1:]))
    ):
        raise ValueError("Invalid orderbook")
    out = {"spread_bps": (asks[0][0] - bids[0][0]) / price * 10000, "coverage": {}}
    for band in (0.001, 0.005, 0.01):
        b = sum(p * q for p, q in bids if p >= price * (1 - band))
        a = sum(p * q for p, q in asks if p <= price * (1 + band))
        key = str(band)
        out["coverage"][key] = bids[-1][0] <= price * (1 - band) and asks[-1][0] >= price * (1 + band)
        out["imbalance_" + key] = (b - a) / (b + a) if b + a else None
    for name, levels in [("bid", bids), ("ask", asks)]:
        p, q = max(levels, key=lambda pq: pq[0] * pq[1])
        out["largest_" + name + "_wall"] = {
            "price": p,
            "notional": p * q,
            "distance_bps": abs(p - price) / price * 10000,
        }
    return out


async def collect_market(http, symbol):
    endpoints = {
        "price": ("/fapi/v2/ticker/price", {}),
        "premium": ("/fapi/v1/premiumIndex", {}),
        "ticker": ("/fapi/v1/ticker/24hr", {}),
        "depth": ("/fapi/v1/depth", {"limit": 100}),
        "oi": ("/fapi/v1/openInterest", {}),
        "oi_history": ("/futures/data/openInterestHist", {"period": "5m", "limit": 15}),
        "global_ratio": ("/futures/data/globalLongShortAccountRatio", {"period": "5m", "limit": 2}),
        "top_accounts": ("/futures/data/topLongShortAccountRatio", {"period": "5m", "limit": 2}),
        "top_positions": ("/futures/data/topLongShortPositionRatio", {"period": "5m", "limit": 2}),
    }
    for tf in ("1m", "5m", "15m", "30m", "1h", "4h"):
        endpoints["kline_" + tf] = ("/fapi/v1/klines", {"interval": tf, "limit": 100})
    for tf in ("5m", "15m", "30m", "1h"):
        endpoints["taker_" + tf] = ("/futures/data/takerlongshortRatio", {"period": tf, "limit": 1})

    async def fetch(key, path, params):
        try:
            return key, await http.get(BINANCE + path, dict(params, symbol=symbol)), time.time(), None
        except Exception as exc:
            return key, None, time.time(), type(exc).__name__

    results = await asyncio.gather(*(fetch(k, *v) for k, v in endpoints.items()))
    raw = {k: v for k, v, _, _ in results if v is not None}
    collected = {k: t for k, _, t, _ in results}
    missing = [k for k, v, _, _ in results if v is None]
    if "price" not in raw:
        return {
            "symbol": symbol,
            "reference_price": None,
            "data_age_seconds": None,
            "data_quality": {"missing_fields": missing, "score": 0},
        }, raw
    price = float(raw["price"]["price"])
    price_at = raw["price"].get("time", collected["price"] * 1000) / 1000
    if (
        price <= 0
        or not math.isfinite(price)
        or raw["price"].get("symbol") != symbol
        or price_at > time.time() + 5
    ):
        raise ValueError("Invalid reference price")
    field_health = {}
    stale = []
    usable = dict(raw)
    for key, value in raw.items():
        observed = None
        max_age = 180
        if key.startswith("kline_"):
            continue  # Validated against each interval by features().
        if isinstance(value, dict):
            if value.get("symbol", symbol) != symbol:
                usable.pop(key, None)
                missing.append(key)
                field_health[key] = {"status": "SYMBOL_MISMATCH"}
                continue
            observed = value.get("time", value.get("E", value.get("closeTime")))
        elif isinstance(value, list) and value:
            observed = value[-1].get("timestamp")
            # Historical endpoints have 5m/15m/30m/1h sampling, not live tick semantics.
            tf = key.removeprefix("taker_")
            max_age = {"15m": 1080, "30m": 1980, "1h": 3780}.get(tf, 480)
        stamp = float(observed) / 1000 if observed is not None else collected[key]
        age = time.time() - stamp
        bad = age > max_age or age < -5
        field_health[key] = {
            "observed_at": stamp,
            "collected_at": collected[key],
            "time_basis": "exchange" if observed is not None else "collection",
            "max_age_seconds": max_age,
            "status": "STALE" if bad else "OK",
        }
        if bad:
            stale.append(key)
            usable.pop(key, None)
    # Raw responses remain intact for audit; stale fields are excluded from model features.
    original_raw = raw
    raw = usable
    packet = {
        "symbol": symbol,
        "snapshot_at": price_at,
        "reference_price": price,
        "data_age_seconds": max(0, time.time() - price_at),
        "technical_features": {},
        "returns_pct": {},
        "funding": raw.get("premium"),
        "position_ratios": {k: raw.get(k) for k in ("global_ratio", "top_accounts", "top_positions")},
        "taker": {tf: raw.get("taker_" + tf) for tf in ("5m", "15m", "30m", "1h")},
        "open_interest": {"current": raw.get("oi")},
        "cross_exchange": {},
        "liquidations": None,
        "onchain_or_smart_money": {},
        "provenance": {"provider": "binance", "underlying_sources": ["binance"]},
    }
    for tf in ("1m", "5m", "15m", "30m", "1h", "4h"):
        if "kline_" + tf in raw:
            try:
                packet["technical_features"][tf] = features(raw["kline_" + tf])
            except (ValueError, TypeError, KeyError, IndexError):
                missing.append("technical_" + tf)
                raw.pop("kline_" + tf, None)
    minutes = raw.get("kline_1m", [])
    for mins in (1, 5, 15, 30, 60):
        target = price_at - mins * 60
        valid = [r for r in minutes if r[6] / 1000 <= target]
        if valid and target - valid[-1][6] / 1000 < 90:
            packet["returns_pct"]["1h" if mins == 60 else str(mins) + "m"] = (
                price / float(valid[-1][4]) - 1
            ) * 100
    hourly = raw.get("kline_5m", [])
    old_bars = [r for r in hourly if r[6] / 1000 <= price_at - 4 * 3600]
    if old_bars:
        packet["returns_pct"]["4h"] = (price / float(old_bars[-1][4]) - 1) * 100
    history = raw.get("oi_history", [])
    if history:
        last = history[-1]
        for mins in (5, 15, 30, 60):
            candidates = [r for r in history if r["timestamp"] <= last["timestamp"] - mins * 60000]
            if candidates and float(candidates[-1]["sumOpenInterest"]):
                packet["open_interest"]["change_" + ("1h" if mins == 60 else str(mins) + "m") + "_pct"] = (
                    float(last["sumOpenInterest"]) / float(candidates[-1]["sumOpenInterest"]) - 1
                ) * 100
    if "depth" in raw:
        try:
            packet["orderbook"] = book_features(raw["depth"], price)
        except (ValueError, TypeError, KeyError):
            missing.append("depth")

    async def cross(name, url, params):
        try:
            doc = await http.get(url, params)
            raw[name] = {"ticker": doc}
            if name == "bybit" and doc.get("retCode") != 0:
                raise ValueError("Bybit unavailable")
            if name == "okx" and doc.get("code") != "0":
                raise ValueError("OKX unavailable")
            r = doc["result"]["list"][0] if name == "bybit" else doc["data"][0]
            identity = r["symbol"] if name == "bybit" else r["instId"]
            expected = symbol if name == "bybit" else params["instId"]
            if identity != expected:
                raise ValueError("Cross-exchange symbol mismatch")
            p = float(r["lastPrice"] if name == "bybit" else r["last"])
            stamp = float(doc.get("time", time.time() * 1000) if name == "bybit" else r["ts"]) / 1000
            if not math.isfinite(p) or p <= 0 or time.time() - stamp > 180:
                raise ValueError("Cross-exchange stale or invalid price")
            cross_packet = {
                "available": True,
                "instrument_id": identity,
                "snapshot_at": stamp,
                "price": p,
                "price_delta_vs_binance_bps": (p / price - 1) * 10000,
                "funding": r.get("fundingRate"),
                "oi": r.get("openInterest"),
                "mark_price": r.get("markPrice"),
                "best_bid": r.get("bid1Price", r.get("bidPx")),
                "best_ask": r.get("ask1Price", r.get("askPx")),
                "returns_pct": {},
                "missing_fields": [],
            }
            if name == "bybit" and r.get("prevPrice1h") and float(r["prevPrice1h"]) > 0:
                cross_packet["returns_pct"]["1h"] = (p / float(r["prevPrice1h"]) - 1) * 100
            if name == "okx":
                for key, path, extra, field in [
                    ("funding", "/api/v5/public/funding-rate", {}, "fundingRate"),
                    ("oi", "/api/v5/public/open-interest", {"instType": "SWAP"}, "oi"),
                    ("mark_price", "/api/v5/public/mark-price", {"instType": "SWAP"}, "markPx"),
                ]:
                    try:
                        more = await http.get("https://www.okx.com" + path, dict(extra, instId=identity))
                        raw[name][key] = more
                        cross_packet[key] = more["data"][0][field]
                    except Exception:
                        cross_packet["missing_fields"].append(key)
            try:
                if name == "bybit":
                    kdoc = await http.get(
                        "https://api.bybit.com/v5/market/kline",
                        {"category": "linear", "symbol": symbol, "interval": "5", "limit": 15},
                    )
                    krows = kdoc["result"]["list"]
                else:
                    kdoc = await http.get(
                        "https://www.okx.com/api/v5/market/candles",
                        {"instId": identity, "bar": "5m", "limit": 15},
                    )
                    krows = kdoc["data"]
                raw[name]["kline_5m"] = kdoc
                for minutes in (5, 15, 60):
                    rows = [r for r in krows if int(r[0]) / 1000 + 300 <= stamp - minutes * 60]
                    if rows:
                        candle = max(rows, key=lambda r: int(r[0]))
                        old = float(candle[4])
                        if old > 0:
                            cross_packet["returns_pct"]["1h" if minutes == 60 else str(minutes) + "m"] = (
                                p / old - 1
                            ) * 100
            except Exception:
                cross_packet["missing_fields"].append("kline_5m")
            packet["cross_exchange"][name] = cross_packet
        except Exception as exc:
            packet["cross_exchange"][name] = {"available": False, "reason": type(exc).__name__}

    await asyncio.gather(
        cross("bybit", "https://api.bybit.com/v5/market/tickers", {"category": "linear", "symbol": symbol}),
        cross("okx", "https://www.okx.com/api/v5/market/ticker", {"instId": symbol[:-4] + "-USDT-SWAP"}),
    )
    packet["data_quality"] = {
        "missing_fields": missing,
        "stale_fields": stale,
        "field_health": field_health,
        "score": max(0, round((len(endpoints) - len(set(missing + stale))) / len(endpoints), 3)),
    }
    original_raw.update({k: v for k, v in raw.items() if k in ("bybit", "okx")})
    return packet, original_raw
