"""One validated, audited settings boundary for Bot and internal API."""

import json
import re
from common.db import DEFAULTS, Session, Setting, Audit
from signals.ranking_config import RankingConfig

BOUNDS = {
    "cycle_minutes": (1, 1440),
    "information_window": (1, 1440),
    "ranking_top_n": (10, 100),
    "prefilter_count": (15, 25),
    "signal_ttl_minutes": (60, 90),
}
PROVIDERS = (
    "binance",
    "tradingview",
    "telegram",
    "cryptoquant",
    "nansen",
    "coinmarketcal",
    "coinglass",
    "lunarcrush",
)


def validate(key, value):
    if key in BOUNDS:
        lo, hi = BOUNDS[key]
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError(f"{key} must be an integer from {lo} to {hi}")
    elif key == "ranking_config":
        value = RankingConfig.model_validate(value).model_dump()
    elif key == "telegram_channels":
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 20
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", v) for v in value
            )
        ):
            raise ValueError("Invalid Telegram channel usernames")
        value = list(dict.fromkeys(value))
    elif key == "analysis_enabled" or key in {"source_" + p for p in PROVIDERS}:
        if type(value) is not bool:
            raise ValueError("Expected true or false")
    elif key == "ranking_limits":
        from signals.tradingview import RANKINGS

        allowed = {name + "_" + kind for name, rows in RANKINGS.items() for kind, _, _ in rows} | {
            "most_recent",
            "most_popular",
        }
        if (
            not isinstance(value, dict)
            or not value.keys() <= allowed
            or any(type(n) is not int or not 1 <= n <= 100 for n in value.values())
        ):
            raise ValueError("Unknown ranking or Top N outside 1..100")
    else:
        raise ValueError("Unknown setting")
    return value


def parse(key, text):
    if key == "telegram_channels":
        value = [v.strip().lstrip("@") for v in text.split(",")]
    else:
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError("Use an integer, true/false, or JSON object") from None
    return validate(key, value)


def compare_and_set(key, value, expected, actor):
    value = validate(key, value)
    with Session.begin() as s:
        # Cross-process settings write serialization, including missing default rows.
        from sqlalchemy import text

        s.execute(text("SELECT pg_advisory_xact_lock(194201704)"))
        row = s.get(Setting, key)
        current = row.value if row else DEFAULTS.get(key)
        if current != expected:
            raise ValueError("Configuration changed; refresh and preview again")
        s.merge(Setting(key=key, value=value))
        s.add(Audit(actor=actor, key=key, value=value))
    return value
