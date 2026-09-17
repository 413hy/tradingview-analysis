import asyncio
import fcntl
import json
import logging
import time
import httpx
from common.config import settings
from common.errors import error_code
from trader.store import Store
from trader.exchange import Exchange
from trader.engine import Engine


async def main():
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = (settings.runtime_dir / "trader.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = Store()
    exchange = Exchange()
    engine = Engine(store, exchange)
    credentials = bool(
        settings.binance_demo_api_key.get_secret_value()
        and settings.binance_demo_api_secret.get_secret_value()
    )

    async def periodic(name, seconds, action):
        while True:
            start = time.monotonic()
            try:
                store.set("heartbeat", time.time())
                if not credentials:
                    store.set("last_error", "Binance Demo API credentials missing")
                else:
                    await action()
                    store.set(name + "_error", None)
                    store.set(name + "_heartbeat", time.time())
                    store.set("last_error", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = error_code(exc)
                if hasattr(exc, "code"):
                    code = "BINANCE_" + str(exc.code)
                if store.get(name + "_error") != code:
                    store.event("SERVICE_ERROR", {"stage": name, "error": code}, notify=True)
                store.set(name + "_error", code)
                store.set("last_error", code)
            await asyncio.sleep(max(0.1, seconds - (time.monotonic() - start)))

    async def monitor():
        await exchange.sync()
        await engine.monitor()
        positions = await exchange.positions()
        account = await exchange.account()
        active = [
            {
                k: p.get(k)
                for k in (
                    "symbol",
                    "positionAmt",
                    "entryPrice",
                    "markPrice",
                    "unRealizedProfit",
                    "positionSide",
                )
            }
            for p in positions
            if float(p["positionAmt"]) != 0
        ]
        store.set(
            "exchange_snapshot",
            {
                "at": time.time(),
                "positions": active,
                "available_balance": account.get("availableBalance"),
                "wallet_balance": account.get("totalWalletBalance"),
            },
        )
        for command in store.rows("SELECT * FROM commands WHERE status='PENDING' ORDER BY rowid LIMIT 10"):
            # Durable at-most-once claim. Restart never grants the manual retry twice.
            if not store.execute(
                "UPDATE commands SET status='CLAIMED' WHERE id=? AND status='PENDING'", (command["id"],)
            ):
                continue
            try:
                data = json.loads(command["payload"])
                async with engine.lock:
                    rows = store.rows(
                        "SELECT * FROM trades WHERE id=? AND status='OPEN'", (data["trade_id"],)
                    )
                    if rows and command["kind"] == "retry_protection":
                        await engine.protect(rows[0], json.loads(rows[0]["payload"]), manual=True)
                store.execute("UPDATE commands SET status='DONE' WHERE id=?", (command["id"],))
            except Exception:
                store.execute("UPDATE commands SET status='FAILED' WHERE id=?", (command["id"],))
                raise

    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:

        async def consume():
            if not settings.trading_enabled or store.get("entries_paused", False):
                return
            if time.time() - store.get("monitor_heartbeat", 0) > 90:
                return  # Account/position observation must be healthy before creating new exposure.
            r = await client.get(
                settings.signal_api_url + "/api/v1/signals/latest",
                headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
            )
            if r.status_code == 404:
                return
            r.raise_for_status()
            doc = r.json()
            if doc["is_stale"]:
                return
            for signal in doc["signals"]:
                try:
                    await engine.enter(signal)
                except ValueError as exc:
                    if not str(exc).startswith("SKIP_"):
                        raise

        try:
            await asyncio.gather(periodic("monitor", 30, monitor), periodic("consumer", 1, consume))
        finally:
            await exchange.close()
            lock.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
