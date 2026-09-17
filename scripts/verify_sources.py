"""Live collector smoke with explicit fixture universe, never publishes signals."""

import asyncio
import json
from collections import Counter
from pathlib import Path
from signals.sources import Universe, PublicHTTP, telegram_collect
from signals import tradingview
from common.config import settings


async def main():
    names = [
        "BTC",
        "ETH",
        "SOL",
        "BNB",
        "XRP",
        "DOGE",
        "ADA",
        "LINK",
        "AVAX",
        "LTC",
        "SUI",
        "AAVE",
        "HYPE",
        "DOT",
        "TRX",
        "NEAR",
        "UNI",
        "ETC",
        "FIL",
        "ATOM",
    ]
    universe = Universe(
        [
            dict(
                symbol=s + "USDT", baseAsset=s, quoteAsset="USDT", contractType="PERPETUAL", status="TRADING"
            )
            for s in names
        ]
    )
    report = {
        "verification_mode": "REAL_SOURCE_FETCH_WITH_FIXTURE_UNIVERSE_NOT_A_LIVE_CYCLE",
        "universe_verified_against_binance": False,
        "health": [],
    }
    tradingview.source_health = lambda *a, **kw: report["health"].append(
        {"source": a[0], "error": a[1] if len(a) > 1 else None, **kw}
    )
    settings.tradingview_profile_dir = Path("runtime/verification/tv-profile")
    http = PublicHTTP()
    try:
        tv = await tradingview.collect(universe, 20)
        telegram = await telegram_collect(http, universe)
        report["counts"] = dict(Counter(r["ranking_type"] for r in tv))
        report["telegram_items"] = len(telegram)
        report["raw_items"] = tv + telegram
        path = Path("runtime/verification/public_sources.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "raw_items"}, ensure_ascii=False, indent=2))
    finally:
        await http.close()


asyncio.run(main())
