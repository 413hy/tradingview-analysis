"""Bounded optional enrichments. Failures never block the free core pipeline."""

import asyncio
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
import httpx
from common.config import settings
from common.db import get_setting, source_health
from common.errors import error_code
from signals.sources import item


@dataclass
class Provider:
    name: str
    base: str
    header: str
    bearer: bool = False


REGISTRY = {
    p.name: p
    for p in (
        Provider("coinmarketcal", "https://api.coinmarketcal.com", "x-api-key"),
        Provider("lunarcrush", "https://lunarcrush.com/api4", "Authorization", True),
        Provider("nansen", "https://api.nansen.ai", "apikey"),
        Provider("cryptoquant", "https://api.cryptoquant.com/v1", "Authorization", True),
        Provider("coinglass", "https://open-api-v4.coinglass.com", "CG-API-KEY"),
    )
}


def enabled(name):
    key = getattr(settings, name + "_api_key").get_secret_value()
    active = bool(key) and get_setting("source_" + name) is not False
    if not active:
        source_health(name, enabled=False, reason="missing_key" if not key else "disabled_by_settings")
    return active


class OptionalProviders:
    def __init__(self, save):
        self.save = save
        self.blocked = set()
        self.locks = {name: asyncio.Lock() for name in REGISTRY}
        self.last_request = {}

    async def request(self, name, path, params=None, body=None):
        p = REGISTRY[name]
        async with self.locks[name]:
            if name in self.blocked:
                raise RuntimeError("Provider disabled for this cycle after auth/quota failure")
            delay = settings.optional_request_interval_seconds - (
                time.monotonic() - self.last_request.get(name, 0)
            )
            if delay > 0:
                await asyncio.sleep(delay)
            key = getattr(settings, name + "_api_key").get_secret_value()
            headers = {p.header: ("Bearer " if p.bearer else "") + key, "Accept": "application/json"}
            self.last_request[name] = time.monotonic()
            async with httpx.AsyncClient(trust_env=False, timeout=20, follow_redirects=False) as client:
                r = await client.request(
                    "POST" if body is not None else "GET",
                    p.base + path,
                    params=params,
                    json=body,
                    headers=headers,
                )
                if r.status_code in (401, 402, 403, 429):
                    self.blocked.add(name)
                r.raise_for_status()
                doc = r.json()
            self.save(
                "optional_raw", {"provider": name, "path": path, "received_at": time.time(), "response": doc}
            )
            if name == "coinglass" and str(doc.get("code")) != "0":
                self.blocked.add(name)
                raise ValueError("CoinGlass application error")
            if name == "cryptoquant" and doc.get("status", {}).get("code") != 200:
                raise ValueError("CryptoQuant application error")
            return doc

    async def discovery(self, universe, top_n):
        async def one(name):
            if not enabled(name):
                return []
            try:
                out = await getattr(self, name)(universe, top_n)
                source_health(name, count=len(out), collector_version="optional-1")
                return out
            except Exception as exc:
                source_health(name, error_code(exc), collector_version="optional-1")
                return []

        parts = await asyncio.gather(*(one(n) for n in ("coinmarketcal", "lunarcrush", "nansen")))
        for name in ("coinglass", "cryptoquant"):
            enabled(name)
        return [r for part in parts for r in part]

    async def coinmarketcal(self, universe, top_n):
        doc = await self.request("coinmarketcal", "/v2/events", {"limit": min(100, top_n)})
        out = []
        for r in doc.get("data", []):
            event_date = r.get("displayedDate", r.get("date", "unknown"))
            for coin in r.get("coins", []):
                symbol = universe.map(coin.get("symbol", ""))
                if not symbol:
                    continue
                text = f"Event background: {r.get('title', '')} ({event_date}); {r.get('description', '')}"
                row = item(
                    "coinmarketcal",
                    symbol,
                    text,
                    url=r.get("sourceUrl", ""),
                    identity=str(r.get("id", "")),
                    raw=r,
                )
                row["source_type"] = "event"
                row["metrics"] = {
                    "event_date": r.get("date"),
                    "displayed_date": event_date,
                    "is_estimated": r.get("isEstimated", True),
                }
                row["provenance"]["direction_evidence"] = (
                    "background_only; event date is not publication time"
                )
                out.append(row)
        return out

    async def lunarcrush(self, universe, top_n):
        doc = await self.request("lunarcrush", "/public/coins/list/v1")
        rows = [r for r in doc.get("data", []) if universe.map(r.get("symbol", ""))]
        rows.sort(key=lambda r: float(r.get("alt_rank") or 1e9))
        out = []
        for rank, r in enumerate(rows[:top_n], 1):
            fields = {
                k: r.get(k)
                for k in ("alt_rank", "galaxy_score", "social_volume_24h", "social_dominance", "sentiment")
            }
            row = item(
                "lunarcrush",
                universe.map(r["symbol"]),
                json.dumps(fields),
                rank=rank,
                ranking_type="alt_rank",
                raw=r,
                identity=str(r.get("id", r["symbol"])),
            )
            row["provenance"]["underlying_sources"] = ["lunarcrush"]
            row["provenance"]["correlated_with"] = ["tradingview_social"]
            out.append(row)
        return out

    async def nansen(self, universe, top_n):
        doc = await self.request(
            "nansen",
            "/api/v1/token-screener",
            body={
                "chains": ["ethereum", "solana", "base"],
                "timeframe": "1h",
                "pagination": {"page": 1, "per_page": top_n},
                "order_by": [{"field": "volume", "direction": "DESC"}],
            },
        )
        out = []
        # On-chain symbols collide. Require explicit chain/address -> Binance symbol mappings.
        mapping = settings.nansen_token_map
        for rank, r in enumerate(doc.get("data", []), 1):
            identity = r.get("chain", "") + ":" + r.get("token_address", "")
            symbol = mapping.get(identity)
            if symbol not in universe.rows:
                continue
            row = item(
                "nansen",
                symbol,
                json.dumps(r, ensure_ascii=False),
                rank=rank,
                ranking_type="onchain_volume",
                raw=r,
                identity=identity,
            )
            row["source_type"] = "smart_money"
            row["provenance"]["chain"] = r.get("chain")
            row["provenance"]["token_address"] = r.get("token_address")
            out.append(row)
        return out

    async def enrich(self, symbol, base_asset):
        out = {}

        async def one(name):
            if not enabled(name):
                out[name] = {"available": False, "provider_status": "disabled"}
                return
            try:
                if name == "cryptoquant":
                    if base_asset not in ("BTC", "ETH"):
                        out[name] = {"available": False, "provider_status": "unsupported_asset"}
                        return
                    doc = await self.request(
                        name,
                        f"/{base_asset.lower()}/exchange-flows/netflow",
                        {"exchange": "all_exchange", "window": "hour", "limit": 2},
                    )
                    data = doc.get("result", {}).get("data", [])
                    underlying = ["onchain_exchange_wallets"]
                else:
                    data = {}
                    for metric, path in [
                        ("oi", "open-interest/aggregated-history"),
                        ("funding", "funding-rate/oi-weight-history"),
                        ("liquidations", "liquidation/aggregated-history"),
                    ]:
                        doc = await self.request(
                            name,
                            "/api/futures/" + path,
                            {"symbol": base_asset, "interval": "15m", "limit": 2},
                        )
                        data[metric] = doc.get("data", [])
                    underlying = ["binance", "bybit", "okx", "other_exchanges"]
                out[name] = {
                    "available": True,
                    "data": data,
                    "provenance": {
                        "provider": name,
                        "underlying_sources": underlying,
                        "independent_confirmation": False,
                    },
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
                source_health(name, collector_version="optional-1")
            except Exception as exc:
                out[name] = {"available": False, "provider_status": error_code(exc)}
                source_health(name, error_code(exc), collector_version="optional-1")

        await asyncio.gather(one("coinglass"), one("cryptoquant"))
        return out
