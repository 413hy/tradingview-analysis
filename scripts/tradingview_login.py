import asyncio
from playwright.async_api import async_playwright
from common.config import settings


async def main():
    settings.tradingview_profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(settings.tradingview_profile_dir), headless=False, args=["--no-sandbox"], locale="en-US"
        )
        page = await context.new_page()
        await page.goto("https://www.tradingview.com/accounts/signin/")
        print("Complete login in Chromium, then press Enter here. No password is recorded.")
        await asyncio.to_thread(input)
        await context.close()


asyncio.run(main())
