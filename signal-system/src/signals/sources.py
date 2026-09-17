import asyncio
import hashlib
import math
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from common.http import client as routed_client
from bs4 import BeautifulSoup
from common.config import settings
from common.db import get_setting, set_setting, source_health
from common.errors import error_code

BINANCE = "https://fapi.binance.com"


class PublicHTTP:
    def __init__(self):
        self.client = routed_client(timeout=25, follow_redirects=True)
        self.gate = asyncio.Semaphore(5)
        self.blocked_until = {}

    async def get(self, url, params=None):
        async with self.gate:
            host = urlsplit(url).netloc
            if time.time() < self.blocked_until.get(host, 0):
                raise RuntimeError("Provider rate limit cooldown")
            r = await self.client.get(url, params=params)
            if r.status_code in (418, 429):
                self.blocked_until[host] = time.time() + max(60, int(r.headers.get("retry-after", "60")))
            if r.status_code in (403, 451):
                self.blocked_until[host] = time.time() + 3600
            r.raise_for_status()
            return r.json()

    async def close(self):
        await self.client.aclose()


class Universe:
    def __init__(self, rows):
        self.rows = {
            r["symbol"]: r
            for r in rows
            if r.get("quoteAsset") == "USDT"
            and r.get("contractType") == "PERPETUAL"
            and r.get("status") == "TRADING"
        }
        self.aliases = {r["baseAsset"]: r["symbol"] for r in self.rows.values()}
        self.aliases.update({s: s for s in self.rows})

    def map(self, raw):
        raw = raw.upper().split(":")[-1].replace(".P", "").replace("/", "").replace("-", "")
        if raw.endswith("USD"):
            raw += "T"
        return self.aliases.get(raw)

    def extract(self, text):
        # Require ticker tokens; do not substring-match ordinary words.
        tokens = re.findall(r"(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9]{1,24})(?![A-Za-z0-9])", text)
        ignored = {"THE", "FOR", "AND", "ALL", "NOT", "API", "USD", "USDT", "AI", "ONE"}
        found = {self.map(t) for t in tokens if t not in ignored and self.map(t)}
        names = {
            "bitcoin": "BTC",
            "ethereum": "ETH",
            "ether": "ETH",
            "solana": "SOL",
            "dogecoin": "DOGE",
            "cardano": "ADA",
            "litecoin": "LTC",
            "chainlink": "LINK",
        }
        for name, ticker in names.items():
            if self.map(ticker) and re.search(r"(?i)\b" + re.escape(name) + r"\b", text):
                found.add(self.map(ticker))
        return sorted(found)

    @classmethod
    async def load(cls, http):
        cached = get_setting("universe_cache")
        if cached and time.time() - cached["at"] < 1800:
            return cls(cached["rows"])
        try:
            doc = await http.get(BINANCE + "/fapi/v1/exchangeInfo")
            obj = cls(doc["symbols"])
            if len(obj.rows) < 10:
                raise ValueError("Universe too small")
            set_setting("universe_cache", {"at": time.time(), "rows": doc["symbols"]})
            return obj
        except Exception:
            if cached and time.time() - cached["at"] < 3600:
                return cls(cached["rows"])
            raise


def item(source, symbol, text, *, at=None, rank=None, ranking_type=None, url="", raw=None, identity=None):
    normalized = " ".join(text.lower().split())
    return {
        "id": str(uuid.uuid4()),
        "source": source,
        "source_name": source,
        "source_type": "ranking" if ranking_type else "social_post",
        "base_asset": symbol[:-4],
        "author": None,
        "title": None,
        "direction_tag": "UNKNOWN",
        "metrics": {},
        "symbol": symbol,
        "provider_item_id": identity,
        "published_at": at if at is not None else time.time(),
        "collected_at": time.time(),
        "ranking_position": rank,
        "ranking_type": ranking_type,
        "raw_text": text,
        "url": url,
        "raw_payload": raw or {},
        "provenance": {"provider": source, "underlying_sources": []},
        "content_hash": hashlib.sha256(normalized.encode()).hexdigest(),
    }


async def binance_discovery(http, universe, top_n):
    rows = [r for r in await http.get(BINANCE + "/fapi/v1/ticker/24hr") if r["symbol"] in universe.rows]
    out = []
    for field, reverse, kind in [
        ("priceChangePercent", True, "gainers"),
        ("priceChangePercent", False, "losers"),
        ("quoteVolume", True, "volume"),
    ]:
        for rank, row in enumerate(sorted(rows, key=lambda r: float(r[field]), reverse=reverse)[:top_n], 1):
            out.append(
                item(
                    "binance",
                    row["symbol"],
                    f"{kind} #{rank}: {field}={row[field]}",
                    rank=rank,
                    ranking_type=kind,
                    raw=row,
                )
            )
    return out


async def telegram_collect(http, universe):
    channels = get_setting("telegram_channels")
    since = time.time() - get_setting("information_window") * 60
    out, successes = [], 0
    client = None
    try:
        if settings.telegram_api_id and settings.telegram_api_hash.get_secret_value():
            from telethon import TelegramClient

            client = TelegramClient(
                settings.telegram_session_path,
                settings.telegram_api_id,
                settings.telegram_api_hash.get_secret_value(),
            )
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram user login required")
        for channel in channels:
            count = 0
            try:
                messages = []
                if client:
                    async for m in client.iter_messages(channel, limit=100):
                        at = m.date.timestamp()
                        if at < since:
                            break
                        messages.append(
                            (
                                f"{channel}/{m.id}",
                                at,
                                m.raw_text or "",
                                {
                                    "views": m.views,
                                    "forwards": m.forwards,
                                    "transport": "mtproto",
                                    "has_media": bool(m.media),
                                    "media_type": type(m.media).__name__ if m.media else None,
                                    "edited_at": m.edit_date.isoformat() if m.edit_date else None,
                                    "forwarded": bool(m.fwd_from),
                                },
                            )
                        )
                else:
                    response = await http.client.get("https://t.me/s/" + channel)
                    response.raise_for_status()
                    nodes = BeautifulSoup(response.text, "html.parser").select(".tgme_widget_message")
                    if not nodes:
                        raise RuntimeError("Telegram public preview unavailable")
                    for m in nodes:
                        text_node, date = (
                            m.select_one(".tgme_widget_message_text"),
                            m.select_one("time[datetime]"),
                        )
                        if not text_node or not date or not date.get("datetime") or not m.get("data-post"):
                            continue
                        try:
                            at = datetime.fromisoformat(date["datetime"]).timestamp()
                        except ValueError:
                            continue
                        messages.append(
                            (
                                m["data-post"],
                                at,
                                text_node.get_text(" ", strip=True),
                                {"transport": "public_preview"},
                            )
                        )
                for mid, at, text, raw in messages:
                    if at < since or at > time.time() + 60:
                        continue
                    for symbol in universe.extract(text):
                        out.append(
                            item(
                                "telegram",
                                symbol,
                                text,
                                at=at,
                                url="https://t.me/" + mid,
                                identity=mid,
                                raw=dict(raw, channel=channel),
                            )
                        )
                        count += 1
                successes += 1
                source_health("telegram:" + channel, count=count)
            except Exception as exc:
                source_health("telegram:" + channel, error_code(exc))
        if channels and not successes:
            raise RuntimeError("All Telegram channels unavailable")
        return out
    finally:
        if client:
            await client.disconnect()


def simhash(text):
    words = re.findall(r"\w+", text.lower())
    grams = {" ".join(words[i : i + 3]) for i in range(max(0, len(words) - 2))}
    vector = [0] * 64
    for gram in grams:
        h = int.from_bytes(hashlib.sha256(gram.encode()).digest()[:8], "big")
        for i in range(64):
            vector[i] += 1 if h & (1 << i) else -1
    return sum((1 << i) for i, v in enumerate(vector) if v > 0)


def rank_items(items, count, config=None):
    from signals.ranking_config import RankingConfig

    config = RankingConfig.model_validate(config or {})
    seen, fingerprints, groups = set(), defaultdict(list), defaultdict(list)
    for row in sorted(items, key=lambda x: -len(x["raw_text"])):
        source, symbol = row["source"], row["symbol"]
        parts = urlsplit(row["url"])
        url = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        keys = [("hash", symbol, row["content_hash"])]
        if row.get("provider_item_id"):
            keys.append(("id", symbol, source, row["provider_item_id"]))
        if url:
            keys.append(("url", symbol, url))
        if any(k in seen for k in keys):
            continue
        fp = simhash(row["raw_text"])
        if len(row["raw_text"]) > 120 and any((fp ^ old).bit_count() <= 3 for old in fingerprints[symbol]):
            continue
        seen.update(keys)
        fingerprints[symbol].append(fp)
        age = max(0, time.time() - (row["published_at"] or row["collected_at"] - 86400))
        weight = config.source_weights.get(source, 1) * config.ranking_weights.get(row["ranking_type"], 1)
        if weight == 0:
            continue
        row = dict(
            row,
            score=weight
            * math.exp(-age / config.freshness_decay_seconds)
            / math.log2(max(1, row["ranking_position"] or 1) + 1.5),
        )
        groups[symbol].append(row)
    ranked = []
    for symbol, rows in groups.items():
        caps = defaultdict(float)
        for r in rows:
            caps[(r["source"], r["ranking_type"])] += r["score"]
        score = sum(
            min(config.per_source_ranking_cap, v) for v in caps.values()
        ) + config.diversity_bonus * len({r["source"] for r in rows})
        sorted_rows = sorted(rows, key=lambda r: -r["score"])
        natural = [
            r
            for r in sorted_rows
            if r["source"] != "binance"
            and (not r["ranking_type"] or r["ranking_type"] in ("most_recent", "most_popular"))
        ]
        chosen = natural[: min(5, config.evidence_limit)]
        chosen_ids = {r["id"] for r in chosen}
        evidence = (chosen + [r for r in sorted_rows if r["id"] not in chosen_ids])[: config.evidence_limit]
        ranked.append(
            {
                "symbol": symbol,
                "score": score,
                "ranking_evidence": [
                    {"source": r["source"], "ranking_type": r["ranking_type"], "rank": r["ranking_position"]}
                    for r in rows
                    if r["ranking_type"]
                ],
                "source_counts": {
                    s: sum(r["source"] == s for r in rows) for s in {r["source"] for r in rows}
                },
                "evidence": [
                    {
                        k: (v[:1000] if k == "raw_text" else v)
                        for k, v in r.items()
                        if k not in ("raw_payload",)
                    }
                    for r in evidence
                ],
            }
        )
    return sorted(ranked, key=lambda r: -r["score"])[:count]
