import asyncio
from common.bot import Bot
from common.db import get_setting
from common.bot_ui import settings_text, local_time
from signals.api import latest, candidates, cycles, sources


from signals.settings_service import parse, compare_and_set, PROVIDERS


async def handle(bot, action, pending):
    if action == "save":
        if get_setting(pending["key"]) != pending["old"]:
            raise ValueError("配置已改变，请重新预览")
        compare_and_set(pending["key"], pending["value"], pending["old"], actor="analysis_bot")
        await bot.reply("已保存。")
    elif action in ("/start", "/help"):
        await bot.reply(
            "分析管理\n/status 周期状态\n/signals 最新信号\n/top10 候选\n/sources 来源健康\n/settings 配置\n/pause 暂停分析\n/resume 恢复分析\n/set 字段 值（预览后保存）",
            {
                "keyboard": [
                    ["/status", "/signals", "/top10"],
                    ["/sources", "/settings"],
                    ["/pause", "/resume"],
                ],
                "resize_keyboard": True,
            },
        )
    elif action == "/settings":
        await bot.reply(
            "⚙️ 分析设置\n\n"
            + settings_text(
                {
                    k: get_setting(k)
                    for k in ("cycle_minutes", "information_window", "ranking_top_n", "telegram_channels")
                }
            ),
            {
                "inline_keyboard": [
                    [{"text": label, "callback_data": "edit:" + key}]
                    for key, label in [
                        ("cycle_minutes", "⏱ 分析间隔"),
                        ("information_window", "🕒 信息时间窗口"),
                        ("ranking_top_n", "📋 榜单数量"),
                        ("telegram_channels", "📣 频道白名单"),
                    ]
                ]
            },
        )
    elif action in ("/pause", "/resume"):
        await bot.preview("analysis_enabled", action == "/resume", get_setting("analysis_enabled"))
    elif action.startswith("/source "):
        name = action.split(maxsplit=1)[1]
        if name not in PROVIDERS:
            raise ValueError("Unknown provider")
        key = "source_" + name
        current = get_setting(key)
        await bot.preview(key, not current, current)
    elif action.startswith("/set "):
        _, key, value = action.split(maxsplit=2)
        await bot.preview(key, parse(key, value), get_setting(key))
    elif action in ("/status", "/signals", "/top10", "/sources"):
        try:
            if action == "/signals":
                doc = latest()
                lines = [f"周期 {doc['cycle_id']}\n生成 {doc['generated_at']}\n过期/降级：{doc['is_stale']}"]
                for r in doc["signals"]:
                    lines.append(
                        f"{r['rank']}. {r['symbol']} {r['direction']} | 确信度 {r['confidence']}\n参考价 {r['reference_price']}\n{r['reason_summary']}\n失效：{r['invalidation_condition']}"
                    )
                await bot.reply("\n\n".join(lines))
            elif action == "/top10":
                doc = candidates()
                rows = sorted(doc["candidates"], key=lambda r: r["candidate_rank"])
                await bot.reply(
                    "Top10\n\n"
                    + "\n\n".join(
                        f"{r['candidate_rank']}. {r['symbol']} {r['information_bias']}\n{r['summary'][:220]}"
                        for r in rows
                    )
                )
            elif action == "/sources":
                await bot.reply(
                    "来源健康\n"
                    + "\n".join(
                        f"{r['source']}: {'禁用' if not r['enabled'] else r['error'] or '正常'}"
                        for r in sources()
                    )
                    + "\n\n点击来源切换启停，预览保存后生效。付费来源还需配置密钥。",
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": ("🟢 " if get_setting("source_" + name) else "⚪ ") + name,
                                    "callback_data": "/source " + name,
                                }
                            ]
                            for name in PROVIDERS
                        ]
                    },
                )
            else:
                doc = cycles()
                details = doc.get("details") or {}
                await bot.reply(
                    f"🧭 分析运行状态\n\n分析：{'▶ 运行中' if get_setting('analysis_enabled') else '⏸ 已暂停'}\n周期状态：{doc['status']}\n开始：{local_time(doc['started_at'].isoformat())}\n结束：{local_time(doc['finished_at'].isoformat()) if doc['finished_at'] else '进行中'}\n原始信息：{details.get('raw_count', 0)} 条\n候选：{details.get('prefilter_count', 0)} 个\nAI 调用：{details.get('ai_calls', 0)} 次\n信号：{details.get('signal_count', 0)} 个\n最近错误：{doc.get('error') or '无'}\n\n周期 ID：{doc['id']}"
                )
        except Exception:
            await bot.reply("当前尚无可用结果，请查看运行状态。")
    else:
        await bot.reply("使用 /help 查看命令。")


async def notices(bot):
    from common.db import latest_cycle

    while True:
        c = latest_cycle()
        if c and c.status in ("SUCCESS", "FAILED") and bot.store.get("last_cycle_notice") != c.id:
            if c.status == "SUCCESS":
                await handle(bot, "/signals", None)
            else:
                await bot.reply(f"分析周期失败\n{c.id}\n{c.error}\n没有发布新信号。")
            bot.store.set("last_cycle_notice", c.id)
        await asyncio.sleep(10)


async def main():
    bot = Bot("analysis", handle)
    await asyncio.gather(bot.run(), notices(bot))


if __name__ == "__main__":
    asyncio.run(main())
