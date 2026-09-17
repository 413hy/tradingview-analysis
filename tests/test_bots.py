import pytest
from common.bot import Bot
from common.config import settings
from signals.bot import parse


def test_analysis_settings_bounds():
    assert parse("cycle_minutes", "20") == 20
    assert parse("telegram_channels", "@binance_announcements,cointelegraph") == [
        "binance_announcements",
        "cointelegraph",
    ]
    with pytest.raises(ValueError):
        parse("cycle_minutes", "0")
    with pytest.raises(ValueError):
        parse("telegram_channels", "../credentials")
    with pytest.raises(ValueError):
        parse("source_tradingview", "maybe")


@pytest.mark.asyncio
async def test_bot_requires_both_chat_and_user_and_preview_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_chat_id", 10)
    monkeypatch.setattr(settings, "telegram_user_id", 20)
    received = []

    async def handle(bot, action, pending):
        received.append(action)

    bot = Bot("analysis", handle)
    await bot.process({"message": {"chat": {"id": 10}, "from": {"id": 99}, "text": "/resume"}})
    await bot.process({"message": {"chat": {"id": 99}, "from": {"id": 20}, "text": "/resume"}})
    assert not received
    await bot.process({"message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/status"}})
    assert received == ["/status"]
    await bot.client.aclose()


def test_codex_command_disables_tools(tmp_path):
    from signals.codex_bridge import command

    args = command(tmp_path)
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--model") + 1] == "gpt-5.6-terra"
    for feature in ("shell_tool", "apps", "plugins", "browser_use", "computer_use", "multi_agent"):
        assert ["--disable", feature] == args[args.index(feature) - 1 : args.index(feature) + 1]


@pytest.mark.asyncio
async def test_duplicate_bot_update_cannot_repeat_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_chat_id", 10)
    monkeypatch.setattr(settings, "telegram_user_id", 20)
    received = []

    async def handle(bot, action, pending):
        received.append(action)

    bot = Bot("analysis", handle)
    update = {"update_id": 42, "message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/resume"}}
    await bot.process(update)
    await bot.process(update)
    await bot.client.aclose()
    bot = Bot("analysis", handle)
    await bot.process(update)
    assert received == ["/resume"]
    await bot.client.aclose()


@pytest.mark.asyncio
async def test_inline_settings_text_then_preview_save(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_chat_id", 10)
    monkeypatch.setattr(settings, "telegram_user_id", 20)
    received = []

    async def handle(bot, action, pending):
        received.append((action, pending))
        if action.startswith("/set "):
            await bot.preview("margin", 12, 10)

    bot = Bot("trading", handle)

    async def call(*args):
        return True

    bot.call = call

    def callback(i, text):
        return {
            "update_id": i,
            "callback_query": {
                "id": str(i),
                "message": {"chat": {"id": 10}},
                "from": {"id": 20},
                "data": text,
            },
        }

    await bot.process(callback(1, "edit:margin"))
    await bot.process({"update_id": 2, "message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "12"}})
    assert received[0][0] == "/set margin 12"
    token = bot.store.get("pending")["token"]
    await bot.process(callback(3, "save:" + token))
    await bot.process(callback(4, "save:" + token))
    assert len(received) == 2 and received[1][0] == "save"
    await bot.client.aclose()


@pytest.mark.asyncio
async def test_callback_ack_failure_still_executes_command(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_chat_id", 10)
    monkeypatch.setattr(settings, "telegram_user_id", 20)
    received = []

    async def handle(bot, action, pending):
        received.append(action)

    bot = Bot("trading", handle)

    async def fail(*args):
        raise RuntimeError("expired callback")

    bot.call = fail
    await bot.process(
        {
            "update_id": 100,
            "callback_query": {
                "id": "a",
                "message": {"chat": {"id": 10}},
                "from": {"id": 20},
                "data": "/status",
            },
        }
    )
    assert received == ["/status"]
    await bot.client.aclose()


@pytest.mark.asyncio
async def test_bad_input_keeps_editor_and_menu_cancels_old_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_chat_id", 10)
    monkeypatch.setattr(settings, "telegram_user_id", 20)

    async def handle(bot, action, pending):
        if action.startswith("/set "):
            value = float(action.split()[-1])
            await bot.preview("margin", value, 10)

    bot = Bot("trading", handle)
    bot.store.set("pending_input", {"key": "margin", "expires": __import__("time").time() + 600})

    def message(i, text):
        return {"update_id": i, "message": {"chat": {"id": 10}, "from": {"id": 20}, "text": text}}

    with pytest.raises(ValueError):
        await bot.process(message(1, "invalid"))
    assert bot.store.get("pending_input")
    await bot.process(message(2, "12"))
    assert bot.store.get("pending")["value"] == 12
    await bot.process(message(3, "⚙️ 开仓设置"))
    assert bot.store.get("pending") is None
    await bot.client.aclose()


def test_ui_units_and_keyboard():
    from common.bot_ui import keyboard, preview_text, settings_text

    text = settings_text({"margin": 10, "leverage": 3, "tp": 0.5})
    assert "30 USDT" in text and "3 倍" in text
    assert "尚未生效" in preview_text("entries_paused", False, True)
    assert keyboard("trading")["resize_keyboard"]


@pytest.mark.asyncio
async def test_preconfiguration_notification_uses_current_owner(tmp_path, monkeypatch):
    import httpx
    import json

    monkeypatch.setattr(settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_chat_id", 10)

    async def handle(*args):
        pass

    bot = Bot("trading", handle)
    await bot.client.aclose()
    seen = []

    def response(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    bot.token = "fixture-token"
    await bot.call("sendMessage", {"chat_id": 0, "text": "old queued notice"})
    assert seen[0]["chat_id"] == 10
    assert "keyboard" in seen[0]["reply_markup"]
    await bot.client.aclose()


@pytest.mark.asyncio
async def test_source_toggle_previews_without_saving(monkeypatch):
    from signals import bot as analysis

    monkeypatch.setattr(analysis, "get_setting", lambda key: True)
    previews = []

    class UI:
        async def preview(self, *args):
            previews.append(args)

    await analysis.handle(UI(), "/source tradingview", None)
    assert previews == [("source_tradingview", False, True)]
    with pytest.raises(ValueError):
        await analysis.handle(UI(), "/source unknown", None)


def test_pause_resume_are_one_state_dependent_button():
    from common.bot_ui import keyboard

    for kind, noun in [("trading", "开仓"), ("analysis", "分析")]:
        for paused in (False, True):
            labels = [v for row in keyboard(kind, paused)["keyboard"] for v in row]
            assert sum(("暂停" + noun in v or "恢复" + noun in v) for v in labels) == 1
            assert ("▶️ 恢复" + noun if paused else "⏸️ 暂停" + noun) in labels


@pytest.mark.asyncio
async def test_reply_wakes_delivery_without_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)

    async def handle(*args):
        pass

    bot = Bot("trading", handle)
    assert not bot.delivery_ready.is_set()
    await bot.reply("test")
    assert bot.delivery_ready.is_set()
    await bot.client.aclose()
    await bot.poll_client.aclose()
