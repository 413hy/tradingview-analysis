"""Direct Telegram API smoke: edits/deletes only the diagnostic message it creates.
Never polls getUpdates concurrently with the deployed bots; no trading/settings mutations.
"""

import asyncio
import json
import time
from pathlib import Path
import httpx
from common.config import settings


async def main():
    report = []
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        for kind in ("analysis", "trading"):
            token = getattr(settings, kind + "_bot_token").get_secret_value()

            async def call(method, payload):
                start = time.monotonic()
                try:
                    response = await client.post(
                        "https://api.telegram.org/bot" + token + "/" + method, json=payload
                    )
                    doc = response.json()
                    report.append(
                        {
                            "bot": kind,
                            "method": method,
                            "http": response.status_code,
                            "ok": doc.get("ok", False),
                            "latency_ms": round((time.monotonic() - start) * 1000),
                        }
                    )
                    return doc.get("result") if doc.get("ok") else None
                except Exception as exc:
                    report.append({"bot": kind, "method": method, "error": type(exc).__name__})
                    return None

            await call("getMe", {})
            await call("getWebhookInfo", {})
            await call("getChat", {"chat_id": settings.telegram_chat_id})
            sent = await call(
                "sendMessage",
                {
                    "chat_id": settings.telegram_chat_id,
                    "text": "🧪 接口延迟测试：本条测试消息稍后自动删除，不改变任何交易设置。",
                },
            )
            if sent:
                target = {"chat_id": settings.telegram_chat_id, "message_id": sent["message_id"]}
                await call("editMessageText", dict(target, text="✅ 直连发送与编辑接口测试完成。"))
                await call(
                    "editMessageReplyMarkup",
                    dict(
                        target,
                        reply_markup={
                            "inline_keyboard": [[{"text": "🧭 查看状态", "callback_data": "/status"}]]
                        },
                    ),
                )
                await call("deleteMessage", target)
    path = Path("runtime/verification/bot_api_direct.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
