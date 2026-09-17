from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from common.config import settings


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


J = JSON().with_variant(JSONB, "postgresql")


class Cycle(Base):
    __tablename__ = "analysis_cycles"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="RUNNING")
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(J, default=dict)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str | None] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(J)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(J)


class Health(Base):
    __tablename__ = "source_health"
    source: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(J, default=dict)


class Setting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(J)


class Audit(Base):
    __tablename__ = "settings_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    actor: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String)
    value: Mapped[Any] = mapped_column(J)


engine = create_engine(settings.database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)

DEFAULTS = {
    "ranking_config": {},
    "ranking_limits": {},
    **{"source_" + p: True for p in ("cryptoquant", "nansen", "coinmarketcal", "coinglass", "lunarcrush")},
    "analysis_enabled": True,
    "cycle_minutes": 20,
    "information_window": 60,
    "prefilter_count": 20,
    "ranking_top_n": 20,
    "signal_ttl_minutes": 60,
    "telegram_channels": ["binance_announcements", "cointelegraph"],
    "source_binance": True,
    "source_tradingview": True,
    "source_telegram": True,
}


def get_setting(key):
    with Session() as s:
        row = s.get(Setting, key)
        return row.value if row else DEFAULTS.get(key)


def set_setting(key, value, actor="system"):
    with Session.begin() as s:
        s.merge(Setting(key=key, value=value))
        s.add(Audit(actor=actor, key=key, value=value))


def source_health(source, error=None, *, enabled=True, **details):
    with Session.begin() as s:
        row = s.get(Health, source) or Health(source=source, consecutive_failures=0)
        row.enabled, row.details, row.error = enabled, details, error
        if error:
            row.last_error_at = now()
            row.consecutive_failures += 1
        elif enabled:
            row.last_success_at, row.consecutive_failures = now(), 0
        s.add(row)


def latest_cycle(success=False):
    with Session() as s:
        q = select(Cycle).order_by(Cycle.started_at.desc()).limit(1)
        if success:
            q = q.where(Cycle.status == "SUCCESS")
        return s.scalar(q)
