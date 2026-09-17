import asyncio
import json
from common.bot import Bot
from trader.store import Store
from trader.engine import TradingSettings
from common.bot_ui import settings_text, local_time
import time

store = Store()


async def handle(bot, action, pending):
    if action == "save":
        key = pending["key"]
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            current = (
                json.loads(row[0])
                if row
                else (False if key == "entries_paused" else TradingSettings().model_dump())
            )
            if current != pending["old"]:
                raise ValueError("配置已改变，请重新预览")
            db.execute(
                "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(pending["value"])),
            )
            db.execute(
                "INSERT INTO events(at,kind,payload) VALUES (?,?,?)",
                (
                    time.time(),
                    "SETTINGS_CHANGED",
                    json.dumps({"key": key, "value": pending["value"], "actor": "trading_bot"}),
                ),
            )
        await bot.reply("已保存；已有仓位的原止盈止损保持不变。")
    elif action in ("/start", "/help"):
        await bot.reply(
            "Binance Demo 交易管理\n/status 状态\n/positions 持仓账本\n/trades 最近交易\n/errors 异常记录\n/settings 开仓参数\n/pause 暂停新仓\n/resume 恢复新仓\n/set margin|notional|leverage|tp|sl 数值\n/retry 交易ID 重试失败保护（仅增加一次手动尝试）",
            {
                "keyboard": [["/status", "/positions", "/trades"], ["/settings"], ["/pause", "/resume"]],
                "resize_keyboard": True,
            },
        )
    elif action == "/settings":
        current = store.get("trading_settings", TradingSettings().model_dump())
        await bot.reply(
            "⚙️ 开仓设置 · Binance Demo\n\n"
            + settings_text(current)
            + "\n\n仅作用于新仓；已有仓位 TP/SL 不变。",
            {
                "inline_keyboard": [
                    [
                        {"text": "💰 每笔保证金", "callback_data": "edit:margin"},
                        {"text": "💵 每笔总价值", "callback_data": "edit:notional"},
                    ],
                    [{"text": "⚡ 杠杆", "callback_data": "edit:leverage"}],
                    [
                        {"text": "🎯 净止盈", "callback_data": "edit:tp"},
                        {"text": "🛑 净止损", "callback_data": "edit:sl"},
                    ],
                    [{"text": "🏠 主菜单", "callback_data": "/start"}],
                ]
            },
        )
    elif action in ("/pause", "/resume"):
        await bot.preview("entries_paused", action == "/pause", store.get("entries_paused", False))
    elif action.startswith("/set "):
        _, key, value = action.split(maxsplit=2)
        old = store.get("trading_settings", TradingSettings().model_dump())
        if key not in ("margin", "notional", "leverage", "tp", "sl"):
            raise ValueError("允许设置 margin、notional、leverage、tp、sl")
        updated = dict(old)
        if key == "notional":
            updated["margin"] = float(value) / updated["leverage"]
        else:
            updated[key] = int(value) if key == "leverage" else float(value)
        updated = TradingSettings.model_validate(updated).model_dump()
        await bot.preview("trading_settings", updated, old)
    elif action == "/status":
        heartbeat = store.get("heartbeat")
        fresh = heartbeat and time.time() - heartbeat < 90
        await bot.reply(
            "🧭 交易运行状态 · Binance Demo\n\n"
            + f"进程：{'🟢 在线' if fresh else '🔴 无新鲜心跳'}\n"
            + f"新仓：{'⏸ 已暂停' if store.get('entries_paused', False) else '▶ 未暂停'}\n"
            + f"最近心跳：{local_time(heartbeat)}（北京时间）\n"
            + f"最近错误：{store.get('last_error') or '无'}\n\n暂停新仓后，已有仓位管理继续。"
        )
    elif action in ("/positions", "/trades"):
        if action == "/positions":
            snapshot = store.get("exchange_snapshot")
            if not snapshot:
                await bot.reply("📊 当前持仓\n\n尚无交易所快照，请先检查账户连接。")
            else:
                lines = [
                    "📊 交易所持仓",
                    "更新：" + local_time(snapshot["at"]) + "（北京时间）",
                    "可用余额：" + str(snapshot.get("available_balance")) + " USDT",
                ]
                for pos in snapshot.get("positions", []):
                    lines.append(
                        f"\n{pos['symbol']}｜数量 {pos['positionAmt']}\n入场 {pos['entryPrice']} · 标记 {pos['markPrice']}\n未实现盈亏 {pos['unRealizedProfit']} USDT"
                    )
                if not snapshot.get("positions"):
                    lines.append("\n暂无持仓。")
                await bot.reply("\n".join(lines))
        query = "SELECT id,symbol,side,status,created_at,payload FROM trades "
        if action == "/positions":
            query += "WHERE status NOT IN ('CLOSED','REJECTED','CANCELED') "
        rows = store.rows(query + "ORDER BY created_at DESC LIMIT 10")
        for r in rows:
            d = json.loads(r.pop("payload"))
            r.update({k: d.get(k) for k in ("quantity", "entry_price", "tp", "sl", "net_pnl")})
        if not rows:
            await bot.reply("🧾 暂无交易记录。")
        for r in rows:
            await bot.reply(
                f"🧾 {r['symbol']} · {'做多' if r['side'] == 'LONG' else '做空'} · {r['status']}\n\n数量：{r.get('quantity') or '待成交'}\n入场：{r.get('entry_price') or '待成交'}\n止盈：{r.get('tp') or '待确认'}\n止损：{r.get('sl') or '待确认'}\n净盈亏：{r.get('net_pnl') or '待结算'} USDT（不含资金费）\n时间：{local_time(r['created_at'])}\n交易 ID：{r['id']}",
                {"inline_keyboard": [[{"text": "🔄 核对 / 重试保护", "callback_data": "/retry " + r["id"]}]]}
                if r["status"] == "OPEN"
                else None,
            )
    elif action == "/errors":
        rows = store.rows(
            "SELECT id,at,kind,payload FROM events WHERE kind IN ('ORDER_UNKNOWN','ORDER_REJECTED','MONITOR_ERROR','PROTECTION_MISSING','SERVICE_ERROR') ORDER BY id DESC LIMIT 10"
        )
        await bot.reply(
            "⚠️ 最近异常\n\n"
            + (
                "\n\n".join(f"{local_time(r['at'])} · {r['kind']}\n{r['payload']}" for r in rows)
                or "暂无异常记录。"
            )
        )
    elif action.startswith("/retry "):
        tid = action.split(maxsplit=1)[1]
        if not store.rows("SELECT id FROM trades WHERE id=? AND status='OPEN'", (tid,)):
            raise ValueError("找不到未平仓交易")
        # Persist request; trading worker alone performs private mutations.
        store.execute(
            "INSERT OR IGNORE INTO commands(id,kind,payload) VALUES (?,?,?)",
            ("retry:" + bot.current_update_id, "retry_protection", json.dumps({"trade_id": tid})),
        )
        await bot.reply("已提交一次保护核对/重试请求。")
    else:
        await bot.reply("使用 /help 查看命令。")


async def deliver_notices(bot):
    while True:
        for row in store.rows("SELECT * FROM outbox WHERE status='PENDING' ORDER BY id LIMIT 20"):
            await bot.reply(row["text"])
            store.execute("UPDATE outbox SET status='SENT' WHERE id=?", (row["id"],))
        await asyncio.sleep(2)


async def main():
    bot = Bot("trading", handle)
    await asyncio.gather(bot.run(), deliver_notices(bot))


if __name__ == "__main__":
    asyncio.run(main())
