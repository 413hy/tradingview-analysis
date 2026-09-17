import asyncio
from pathlib import Path
from telethon import TelegramClient
from common.config import settings


async def main():
    if not settings.telegram_api_id or not settings.telegram_api_hash.get_secret_value():
        raise SystemExit("Configure TELEGRAM_API_ID and TELEGRAM_API_HASH first.")
    Path(settings.telegram_session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        settings.telegram_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
    )
    await client.start()
    print("Telegram session saved; verification code/password not stored in configuration.")
    await client.disconnect()


asyncio.run(main())
