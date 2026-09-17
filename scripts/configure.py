"""Interactive local credentials setup. Values never appear in process arguments."""

import argparse
import getpass
import os
import re
import tempfile
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
FIELDS = [
    ("BINANCE_DEMO_API_KEY", "Binance Demo API Key", True),
    ("BINANCE_DEMO_API_SECRET", "Binance Demo Secret", True),
    ("ANALYSIS_BOT_TOKEN", "分析 Bot Token", True),
    ("TRADING_BOT_TOKEN", "交易 Bot Token（与分析 Bot 不同）", True),
    ("TELEGRAM_CHAT_ID", "允许管理的 chat_id（私聊通常等于 user_id）", False),
    ("TELEGRAM_USER_ID", "允许管理的 user_id", False),
    ("TELEGRAM_API_ID", "MTProto API_ID（可稍后填写）", False),
    ("TELEGRAM_API_HASH", "MTProto API_HASH（可稍后填写）", True),
]


def main():
    parser = argparse.ArgumentParser(description="交互配置密钥；留空保留原值")
    parser.add_argument("--optional", action="store_true", help="同时配置可选付费数据源")
    args = parser.parse_args()
    fields = list(FIELDS)
    if args.optional:
        fields += [
            (name + "_API_KEY", name + " API Key（可留空）", True)
            for name in ("COINGLASS", "CRYPTOQUANT", "NANSEN", "COINMARKETCAL", "LUNARCRUSH")
        ]
    os.chdir(ROOT)
    path = ROOT / "deployment/.env"
    if not path.exists():
        import subprocess

        subprocess.run([str(ROOT / ".venv/bin/python"), "scripts/prepare_config.py"], check=True)
    values = dict(dotenv_values(path))
    updates = {}
    print("输入留空保留原值；密钥隐藏输入。Ctrl+C 取消，本轮修改不会保存。")
    for key, label, secret in fields:
        present = bool(values.get(key)) and values.get(key) != "0"
        prompt = f"{label} [{'已配置' if present else '未配置'}]: "
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if not value:
            continue
        if key.endswith("_ID"):
            if not re.fullmatch(r"-?\d+", value) or int(value) == 0:
                raise SystemExit("ID 必须为非零整数；未保存。")
        elif not re.fullmatch(r"[A-Za-z0-9_.:\-]+", value):
            raise SystemExit("值包含非预期字符；未保存。")
        updates[key] = value
    combined = values | updates
    if combined.get("ANALYSIS_BOT_TOKEN") and combined.get("ANALYSIS_BOT_TOKEN") == combined.get(
        "TRADING_BOT_TOKEN"
    ):
        raise SystemExit("两个 Bot 必须使用不同 Token；未保存。")
    lines = path.read_text().splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in remaining:
            lines[i] = key + "=" + remaining.pop(key)
    lines.extend(k + "=" + v for k, v in remaining.items())
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print("已保存 deployment/.env（权限600）。交易开关保持原值；未向聊天或日志输出密钥。")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n已取消，未保存。")
