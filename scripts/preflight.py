"""Read-only deployment check; private account read requires --private."""

import asyncio
import json
import sys
from common.http import client as routed_client
from common.config import settings
from trader.exchange import Exchange


async def main():
    result = {
        "credentials": {
            k: bool(getattr(settings, k).get_secret_value())
            for k in (
                "binance_demo_api_key",
                "binance_demo_api_secret",
                "analysis_bot_token",
                "trading_bot_token",
            )
        },
        "telegram_admin_configured": bool(settings.telegram_chat_id and settings.telegram_user_id),
        "trading_enabled": settings.trading_enabled,
        "endpoints": {},
    }
    async with routed_client(timeout=20) as client:
        for name, url in [
            ("binance", "https://fapi.binance.com/fapi/v1/exchangeInfo"),
            ("binance_demo", "https://demo-fapi.binance.com/fapi/v1/exchangeInfo"),
            ("bybit", "https://api.bybit.com/v5/market/time"),
            ("okx", "https://www.okx.com/api/v5/public/time"),
            ("api", settings.signal_api_url + "/health"),
            ("codex_bridge", settings.codex_bridge_url + "/health"),
        ]:
            try:
                r = await client.get(url)
                result["endpoints"][name] = {"http_status": r.status_code}
                if name == "codex_bridge" and r.status_code == 200:
                    result["endpoints"][name].update(r.json())
            except Exception as exc:
                result["endpoints"][name] = {"error": type(exc).__name__}
    if "--private" in sys.argv:
        exchange = Exchange()
        try:
            await exchange.sync()
            account = await exchange.account()
            result["demo_account"] = {
                "availableBalance": account["availableBalance"],
                "position_count": sum(float(p["positionAmt"]) != 0 for p in await exchange.positions()),
            }
        except Exception as exc:
            result["demo_account"] = {"error": type(exc).__name__}
        finally:
            await exchange.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


asyncio.run(main())
