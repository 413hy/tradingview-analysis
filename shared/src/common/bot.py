import asyncio
import json
import secrets
import time
import httpx
import fcntl
from common.config import settings
from common.local_store import Store
from common.polling import PollingGuard
from common.bot_ui import ALIASES, LABELS, keyboard, preview_text


class Bot:
    def __init__(self, kind, handler):
        self.kind, self.handler = kind, handler
        self.token = getattr(settings, kind + "_bot_token").get_secret_value()
        self.store = Store(settings.runtime_dir / (kind + "-bot.db"))
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), trust_env=False)
        self.delivery_ready = asyncio.Event()
        self.polling = PollingGuard(self.store, self.token)
        self.poll_client = httpx.AsyncClient(timeout=httpx.Timeout(35, connect=5), trust_env=False)

    async def call(self, method, payload):
        if method != "getUpdates":
            return await self._call(method, payload)
        async with self.polling.request():
            try:
                return await self._call(method, payload)
            except (Exception, asyncio.CancelledError):
                await self.poll_client.aclose()
                self.poll_client = httpx.AsyncClient(timeout=httpx.Timeout(35, connect=5), trust_env=False)
                raise

    async def _call(self, method, payload):
        # Always preserve navigation, including already queued notifications.
        payload = dict(payload)
        if method == "sendMessage":
            # Notifications queued before first configuration must use the current owner.
            payload["chat_id"] = settings.telegram_chat_id
        if method == "sendMessage" and (
            not payload.get("reply_markup") or "keyboard" in payload["reply_markup"]
        ):
            if self.kind == "trading":
                paused = Store(settings.runtime_dir / "trader.db").get("entries_paused", False)
            else:
                from common.db import get_setting

                paused = not get_setting("analysis_enabled")
            payload["reply_markup"] = keyboard(self.kind, paused)
        # Never log HTTP request URLs containing the Bot token.
        try:
            client = self.poll_client if method == "getUpdates" else self.client
            r = await client.post("https://api.telegram.org/bot" + self.token + "/" + method, json=payload)
            doc = r.json()
            if not doc.get("ok"):
                raise RuntimeError("Telegram API failed: " + method)
            return doc["result"]
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Telegram transport failed") from None

    async def reply(self, text, markup=None):
        # Telegram measures length in UTF-16 units; 2,000 code points fit even with emoji.
        # Preserve full reports instead of silently cutting off later candidates/trades.
        chunks = [text[i : i + 2000] for i in range(0, len(text), 2000)] or ["（空）"]
        with self.store.connect() as db:
            for index, chunk in enumerate(chunks):
                payload = {"chat_id": settings.telegram_chat_id, "text": chunk}
                if markup and index == len(chunks) - 1:
                    payload["reply_markup"] = markup
                db.execute("INSERT INTO outbox(text) VALUES (?)", (json.dumps(payload),))

        self.delivery_ready.set()

    async def preview(self, key, value, old):
        token = secrets.token_hex(8)
        self.store.set(
            "pending", {"key": key, "value": value, "old": old, "token": token, "expires": time.time() + 600}
        )
        await self.reply(
            preview_text(key, old, value),
            {
                "inline_keyboard": [
                    [
                        {"text": "✅ 保存", "callback_data": "save:" + token},
                        {"text": "❌ 取消", "callback_data": "cancel:" + token},
                    ]
                ]
            },
        )

    async def process(self, u):
        callback = u.get("callback_query")
        message = callback.get("message", {}) if callback else u.get("message", {})
        actor = callback.get("from", {}) if callback else message.get("from", {})
        if (
            message.get("chat", {}).get("id") != settings.telegram_chat_id
            or actor.get("id") != settings.telegram_user_id
        ):
            return
        update_id = u.get("update_id")
        if update_id is not None and not self.store.execute(
            "INSERT OR IGNORE INTO bot_updates VALUES (?,?)", (str(update_id), time.time())
        ):
            return
        self.current_update_id = str(update_id) if update_id is not None else secrets.token_hex(12)
        action = callback.get("data", "") if callback else message.get("text", "")
        if not isinstance(action, str):
            return
        action = ALIASES.get(action, action)
        if callback:
            try:
                await self.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            except RuntimeError:
                pass  # UI acknowledgement failure must not discard a durable command.
        if action == "/cancel":
            self.store.set("pending_input", None)
            self.store.set("pending", None)
            await self.reply("已取消设置，配置未修改。")
            return
        if action in ("/start", "/help", "/settings"):
            self.store.set("pending_input", None)
            self.store.set("pending", None)
        pending_input = self.store.get("pending_input")
        if pending_input and not action.startswith(("/", "save:", "cancel:", "edit:")):
            if pending_input["expires"] < time.time():
                await self.reply("输入已过期，请重新选择设置。")
                return
            if message.get("message_id", 0) and message["message_id"] <= pending_input.get(
                "after_message_id", 0
            ):
                await self.reply("这条输入早于当前设置，请重新输入。")
                return
            action = "/set " + pending_input["key"] + " " + action
        if action.startswith("edit:"):
            allowed = {
                "analysis": {"cycle_minutes", "information_window", "ranking_top_n", "telegram_channels"},
                "trading": {"margin", "notional", "leverage", "tp", "sl"},
            }[self.kind]
            key = action[5:]
            if key not in allowed:
                return
            self.store.set("pending", None)
            self.store.set(
                "pending_input",
                {"key": key, "expires": time.time() + 600, "after_message_id": message.get("message_id", 0)},
            )
            unit = (
                "整数，范围 1—5 倍"
                if key == "leverage"
                else "USDT，填写正数，不加单位"
                if key in ("margin", "notional", "tp", "sl")
                else "分钟，填写整数"
                if key in ("cycle_minutes", "information_window")
                else "新值"
            )
            hint = (
                "\n总价值≈保证金×杠杆；修改总价值按当前杠杆换算保证金。"
                if key in ("margin", "notional", "leverage")
                else ""
            )
            await self.reply(
                f"✏️ {LABELS.get(key, key)}\n\n请输入{unit}。{hint}\n输入后先预览，保存才生效；10分钟内有效。",
                {"inline_keyboard": [[{"text": "↩️ 取消编辑", "callback_data": "/cancel"}]]},
            )
            return
        if action.startswith(("save:", "cancel:")):
            pending = self.store.get("pending")
            if not pending or pending["token"] != action.split(":", 1)[1] or pending["expires"] < time.time():
                await self.reply("操作已结束或过期，请重新设置。")
                return
            if action.startswith("save:"):
                await self.handler(self, "save", pending)
            else:
                await self.reply("已取消。")
            self.store.set("pending", None)
        else:
            await self.handler(self, action, None)
            if action.startswith("/set "):
                self.store.set("pending_input", None)

    async def deliver(self):
        while True:
            try:
                for row in self.store.rows(
                    "SELECT * FROM outbox WHERE status='PENDING' ORDER BY id LIMIT 20"
                ):
                    await self.call("sendMessage", json.loads(row["text"]))
                    self.store.execute("UPDATE outbox SET status='SENT' WHERE id=?", (row["id"],))
                self.store.set("delivery_heartbeat", time.time())
                self.delivery_ready.clear()
                if self.store.rows("SELECT id FROM outbox WHERE status='PENDING' LIMIT 1"):
                    continue
                try:
                    await asyncio.wait_for(self.delivery_ready.wait(), timeout=0.25)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)

    async def poll(self):
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    {
                        "offset": self.store.get("offset", 0),
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for u in updates:
                    try:
                        await self.process(u)
                    except (ValueError, KeyError):
                        await self.reply("输入无效或配置已改变。正在编辑时可重新输入，或发送 /cancel 取消。")
                    self.store.set("offset", u["update_id"] + 1)
                self.store.set("poll_heartbeat", time.time())
                self.store.set("poll_error", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.set("poll_error", type(exc).__name__)
                self.store.event("TELEGRAM_POLL_FAILURE", {"type": type(exc).__name__})
                await asyncio.sleep(5)

    async def run(self):
        lock = (settings.runtime_dir / (self.kind + "-bot.lock")).open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if not self.token or not settings.telegram_chat_id or not settings.telegram_user_id:
                self.store.set("status", "WAITING_FOR_CONFIGURATION")
                while True:
                    await asyncio.sleep(30)
            await self.call("getMe", {})
            hook = await self.call("getWebhookInfo", {})
            if hook.get("url"):
                raise RuntimeError("Bot has an existing webhook; use an independent token")
            self.polling.startup()
            self.store.set("status", "RUNNING")
            await asyncio.gather(self.poll(), self.deliver())
        finally:
            await self.client.aclose()
            await self.poll_client.aclose()
            self.polling.close()
            lock.close()
