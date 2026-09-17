"""PostgreSQL integration in a disposable schema, never in production tables."""

import os
import time
import uuid
import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import sessionmaker
from common import db
from signals import pipeline, api
from signals.sources import Universe, item
from signals.schemas import Candidates, Directions


@pytest.fixture
def isolated_db(monkeypatch):
    url = os.environ.get("DATABASE_URL")
    if not url or "postgresql" not in url:
        pytest.skip("Run via scripts/run.py for PostgreSQL integration")
    schema = "test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as c:
        c.execute(text("CREATE SCHEMA " + schema))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    db.Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "Session", session)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(pipeline, "Session", session)
    monkeypatch.setattr(pipeline, "engine", engine)
    monkeypatch.setattr(api, "Session", session)
    yield session
    engine.dispose()
    with admin.begin() as c:
        c.execute(text("DROP SCHEMA " + schema + " CASCADE"))
    admin.dispose()


@pytest.fixture
def synthetic_sources(monkeypatch):
    names = [f"COIN{i}USDT" for i in range(20)]
    u = Universe(
        [
            dict(symbol=n, baseAsset=n[:-4], quoteAsset="USDT", contractType="PERPETUAL", status="TRADING")
            for n in names
        ]
    )

    async def universe(http):
        return u

    async def discovery(*args):
        return [
            item("binance", n, f"Test fixture {n}", rank=i + 1, ranking_type="volume")
            for i, n in enumerate(names)
        ]

    async def no_items(*args):
        return []

    async def market(http, symbol):
        return {"symbol": symbol, "reference_price": 10, "snapshot_at": time.time(), "data_age_seconds": 0}, {
            "fixture": True
        }

    monkeypatch.setattr(pipeline.Universe, "load", universe)
    monkeypatch.setattr(pipeline, "binance_discovery", discovery)
    monkeypatch.setattr(pipeline, "telegram_collect", no_items)
    monkeypatch.setattr(pipeline.tradingview, "collect", no_items)
    monkeypatch.setattr(pipeline, "collect_market", market)
    return names


class FakeAI:
    def __init__(self, save):
        self.calls = 0
        self.usage = {}
        self.save = save

    async def structured_completion(self, prompt, payload, response_model, validate):
        self.calls += 1
        if response_model is Candidates:
            result = Candidates.model_validate(
                {
                    "candidates": [
                        dict(
                            symbol=b["symbol"],
                            candidate_rank=i + 1,
                            candidate_reason="fixture",
                            attention_state="normal",
                            information_bias="MIXED",
                            key_information=[],
                            bullish_arguments=[],
                            bearish_arguments=[],
                            contradictions=[],
                            key_levels={"support": [], "resistance": []},
                            events=[],
                            evidence_ids=[b["evidence"][0]["id"]],
                            information_freshness="fresh",
                            summary="fixture",
                        )
                        for i, b in enumerate(payload["candidates"][:10])
                    ]
                }
            )
        else:
            result = Directions.model_validate(
                {
                    "selected": [],
                    "analyzed": [
                        dict(
                            symbol=b["symbol"],
                            direction="LONG",
                            confidence=i,
                            information_alignment="mixed",
                            market_state="uncertain",
                            supporting_factors=[],
                            opposing_factors=[],
                            key_levels={"support": [], "resistance": []},
                            invalidation_condition="fixture",
                            reason_summary="fixture",
                        )
                        for i, b in enumerate(payload["symbols"])
                    ],
                }
            )
        return validate(result)


@pytest.mark.asyncio
async def test_complete_cycle_two_calls_and_atomic_publication(isolated_db, synthetic_sources, monkeypatch):
    monkeypatch.setattr(pipeline, "AIClient", FakeAI)
    cid = await pipeline.cycle()
    with isolated_db() as s:
        c = s.get(db.Cycle, cid)
        assert c.status == "SUCCESS"
        assert c.details["ai_calls"] == 2
        signals = s.scalars(select(db.Signal)).all()
        assert len(signals) == 1
        assert (signals[0].execution_expires_at - signals[0].generated_at).total_seconds() == 60
    response = api.latest()
    assert not response["is_stale"]
    assert response["signals"][0]["confidence"] == 9
    assert len(api.candidates()["candidates"]) == 10


@pytest.mark.asyncio
async def test_failed_new_cycle_marks_old_signal_stale_without_republish(
    isolated_db, synthetic_sources, monkeypatch
):
    monkeypatch.setattr(pipeline, "AIClient", FakeAI)
    await pipeline.cycle()
    before = api.latest()

    async def fail(http):
        raise RuntimeError("Universe unavailable")

    monkeypatch.setattr(pipeline.Universe, "load", fail)
    await pipeline.cycle()
    after = api.latest()
    assert after["is_stale"]
    assert after["signals"] == before["signals"]
    with isolated_db() as s:
        assert len(s.scalars(select(db.Signal)).all()) == 1


@pytest.mark.asyncio
async def test_advisory_lock_prevents_reentry(isolated_db, monkeypatch):
    with pipeline.engine.connect() as c:
        assert c.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": pipeline.LOCK})
        try:
            assert await pipeline.cycle() is None
            with isolated_db() as s:
                assert not s.scalars(select(db.Cycle)).all()
        finally:
            c.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": pipeline.LOCK})


@pytest.mark.asyncio
async def test_database_failure_does_not_publish_partial_cycle(isolated_db, synthetic_sources, monkeypatch):
    class BadAI(FakeAI):
        async def structured_completion(self, *args):
            if self.calls:
                raise RuntimeError("Model unavailable")
            return await super().structured_completion(*args)

    monkeypatch.setattr(pipeline, "AIClient", BadAI)
    cid = await pipeline.cycle()
    with isolated_db() as s:
        assert s.get(db.Cycle, cid).status == "FAILED"
        assert not s.scalars(select(db.Signal)).all()
        assert len(s.scalars(select(db.Artifact).where(db.Artifact.kind == "candidate_packet")).all()) == 10


@pytest.mark.asyncio
async def test_published_rest_signal_consumed_once_with_both_protection_legs(
    isolated_db, synthetic_sources, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient
    from common.config import settings
    from trader.engine import Engine
    from trader.store import Store
    from test_execution_flow import SimulatedDemo, reachable_candles

    monkeypatch.setattr(pipeline, "AIClient", FakeAI)
    await pipeline.cycle()
    client = TestClient(api.app)
    assert client.get("/api/v1/signals/latest").status_code == 401
    response = client.get(
        "/api/v1/signals/latest", headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    )
    assert response.status_code == 200
    signal = response.json()["signals"][0]
    store = Store(tmp_path / "trader.db")
    exchange = SimulatedDemo()
    engine = Engine(store, exchange)
    engine.candles = reachable_candles
    assert await engine.enter(signal) == "ENTRY_PROCESSED"
    assert await engine.enter(signal) == "DUPLICATE"
    assert [k for k, _ in exchange.submissions] == ["entry", "sl", "tp"]
    assert all(p["symbol"] == signal["symbol"] for _, p in exchange.submissions)


def test_settings_api_compare_and_set_audit(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient
    from common.config import settings
    from signals import settings_service

    monkeypatch.setattr(settings_service, "Session", isolated_db)
    client = TestClient(api.app)
    headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    result = client.put("/api/v1/settings/cycle_minutes", headers=headers, json={"expected": 20, "value": 15})
    assert result.status_code == 200
    assert db.get_setting("cycle_minutes") == 15
    stale = client.put("/api/v1/settings/cycle_minutes", headers=headers, json={"expected": 20, "value": 30})
    assert stale.status_code == 409
    assert db.get_setting("cycle_minutes") == 15
    with isolated_db() as session:
        assert session.scalar(text("SELECT count(*) FROM settings_audit")) == 1
