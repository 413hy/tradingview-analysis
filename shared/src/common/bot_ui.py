"""Chinese Telegram presentation shared by both independently managed bots."""

from datetime import datetime
from zoneinfo import ZoneInfo

LABELS = {
    "margin": "每笔保证金",
    "notional": "每笔总价值",
    "leverage": "杠杆",
    "tp": "净止盈目标",
    "sl": "净止损目标",
    "reserve": "保留资金",
    "cycle_minutes": "分析间隔",
    "information_window": "信息窗口",
    "ranking_top_n": "榜单数量",
    "telegram_channels": "频道白名单",
    "analysis_enabled": "分析运行",
    "entries_paused": "暂停新仓",
}
ALIASES = {
    "📊 当前持仓": "/positions",
    "🧭 运行状态": "/status",
    "🧾 最近交易": "/trades",
    "⚠️ 最近异常": "/errors",
    "⚙️ 开仓设置": "/settings",
    "⚙️ 分析设置": "/settings",
    "⏸️ 暂停开仓": "/pause",
    "▶️ 恢复开仓": "/resume",
    "⏸️ 暂停分析": "/pause",
    "▶️ 恢复分析": "/resume",
    "🎯 最新信号": "/signals",
    "🔎 Top10 候选": "/top10",
    "📡 数据来源": "/sources",
    "🏠 主菜单": "/start",
}


def keyboard(kind, paused=False):
    rows = (
        [
            ["📊 当前持仓", "🧭 运行状态"],
            ["🧾 最近交易", "▶️ 恢复开仓" if paused else "⏸️ 暂停开仓"],
            ["⚠️ 最近异常", "⚙️ 开仓设置"],
        ]
        if kind == "trading"
        else [
            ["🎯 最新信号", "🔎 Top10 候选"],
            ["🧭 运行状态", "📡 数据来源"],
            ["⚙️ 分析设置", "▶️ 恢复分析" if paused else "⏸️ 暂停分析"],
        ]
    )
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "点击菜单查看或管理",
    }


def local_time(value):
    if not value:
        return "暂无"
    try:
        stamp = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai"))
        )
        return stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "暂无"


def display(key, value):
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    unit = (
        " USDT"
        if key in ("margin", "notional", "tp", "sl", "reserve")
        else " 倍"
        if key == "leverage"
        else " 分钟"
        if key in ("cycle_minutes", "information_window")
        else ""
    )
    return str(value) + unit


def settings_text(values):
    lines = [f"{LABELS.get(k, k)}：{display(k, v)}" for k, v in values.items()]
    if "margin" in values and "leverage" in values:
        lines.insert(1, f"预计每笔总价值：{values['margin'] * values['leverage']:g} USDT")
    return "\n".join(lines)


def preview_text(key, old, value):
    if isinstance(old, dict) and isinstance(value, dict):
        changed = [
            f"{LABELS.get(k, k)}：{display(k, old.get(k))} → {display(k, v)}"
            for k, v in value.items()
            if old.get(k) != v
        ]
        return (
            "📝 设置预览 · 尚未生效\n\n"
            + "\n".join(changed)
            + "\n\n修改后\n"
            + settings_text(value)
            + "\n\n点击保存生效，10分钟内有效。已有仓位目标不变。"
        )
    return f"📝 设置预览 · 尚未生效\n\n{LABELS.get(key, key)}\n当前：{display(key, old)}\n修改为：{display(key, value)}\n\n点击保存生效，10分钟内有效。"
