"""Local operations; no credentials are passed on the command line."""

import argparse
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path[:0] = [str(ROOT / p) for p in ("shared/src", "signal-system/src", "trading-system/src")]
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    "deployment/.env",
    "-f",
    "deployment/compose.yml",
    "--profile",
    "bots",
]


def run(*args):
    subprocess.run(list(args), check=True)


def main():
    parser = argparse.ArgumentParser(description="Demo 系统管理（密钥从本地配置读取）")
    parser.add_argument("action", choices=["start", "status", "check", "enable-demo", "disable-entries"])
    parser.add_argument("--build", action="store_true", help="更新镜像后启动")
    args = parser.parse_args()
    if args.action == "start":
        run(sys.executable, "scripts/prepare_config.py")
        run(*COMPOSE, "config", "--quiet")
        run(*COMPOSE, "up", "-d", *(["--build"] if args.build else []), "postgres", "api")
        run("systemctl", "restart", "tradingview-codex-bridge.service")
        run(*COMPOSE, "up", "-d", "worker", "trader", "analysis-bot", "trading-bot")
        print("服务已启动；缺失凭据的服务等待配置。运行 status / check 查看业务就绪状态。")
    elif args.action == "status":
        run(*COMPOSE, "ps")
        run("systemctl", "is-active", "tradingview-codex-bridge.service")
        run(sys.executable, "scripts/run.py", sys.executable, "scripts/api.py", "/api/v1/cycles/latest")
    elif args.action == "check":
        run(sys.executable, "scripts/run.py", sys.executable, "scripts/preflight.py", "--private")
    elif args.action == "disable-entries":
        # Keep TRADING_ENABLED true so existing positions can still be protected.
        from common.local_store import Store

        Store(ROOT / "runtime/trading/trader.db").set("entries_paused", True)
        print("已暂停新仓；已有仓位监控和保护继续运行。恢复使用交易 Bot /resume。")
    elif args.action == "enable-demo":
        values = dotenv_values(ROOT / "deployment/.env")
        required = (
            "BINANCE_DEMO_API_KEY",
            "BINANCE_DEMO_API_SECRET",
            "TRADING_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_USER_ID",
        )
        if any(not values.get(k) or values.get(k) == "0" for k in required):
            raise SystemExit("请先运行 scripts/configure.py，补齐 Demo 与交易 Bot/管理员配置。")
        # Only arm after an authenticated read; never send a test order implicitly.
        for key, value in values.items():
            if value is not None:
                os.environ[key] = value

        async def verify():
            from common.http import client as routed_client
            from trader.exchange import Exchange
            from common.config import settings

            ex = Exchange()
            try:
                await ex.sync()
                account = await ex.account()
                mode = await ex.request("GET", "/fapi/v1/positionSide/dual", signed=True)
                if mode["dualSidePosition"] or account.get("multiAssetsMargin", False):
                    raise ValueError("单向持仓、单资产保证金模式必需")
                async with routed_client(timeout=15) as client:
                    r = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
                    r.raise_for_status()
                    token = settings.trading_bot_token.get_secret_value()
                    r = await client.post("https://api.telegram.org/bot" + token + "/getMe")
                    if r.status_code != 200 or not r.json().get("ok"):
                        raise ValueError("交易 Bot Token 验证失败")
            finally:
                await ex.close()

        try:
            asyncio.run(verify())
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            raise SystemExit("只读检查未通过，未启用新仓。错误类型：" + str(code)) from None
        set_key(str(ROOT / "deployment/.env"), "TRADING_ENABLED", "true", quote_mode="never")
        os.chmod(ROOT / "deployment/.env", 0o600)
        from common.local_store import Store

        store = Store(ROOT / "runtime/trading/trader.db")
        store.set("entries_paused", False)
        store.event("LOCAL_ENABLE_DEMO", {"actor": "local_cli"})
        run(*COMPOSE, "up", "-d", "trader")
        print("已启用 Binance Demo 新仓；没有发送测试订单。下一条有效信号将按规则处理。")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"管理命令失败，退出码 {exc.returncode}") from None
