import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import httpx
import pytest
from common.config import settings
from trader.exchange import Exchange, ExchangeError, DEMO
from trader.engine import Engine, TradingSettings
from trader.store import Store
from trader.risk import Instrument, tp_price, sl_price


def test_reject_production_transport():
    with pytest.raises(ValueError):
        Exchange(httpx.AsyncClient(base_url="https://fapi.binance.com"))


def test_settings_and_price_economics():
    with pytest.raises(ValueError):
        TradingSettings(leverage=6)
    with pytest.raises(ValueError):
        TradingSettings(tp=float("nan"))
    tp = tp_price("LONG", D(100), D(".3"), D(".01"), D(".5"), D(".015"), D(".0002"))
    sl = sl_price("LONG", D(100), D(".3"), D(".01"), D(".015"), D(".0005"))
    assert tp > 100 > sl > 0
    ins = Instrument("BTCUSDT", D(".1"), D(".001"), D(".001"), D(100), D(100), D(100), D(20), D(1))
    with pytest.raises(ValueError, match="SKIP_SIZE_LIMIT"):
        ins.size(D(50000))


def test_signal_expiry_and_durable_claim(tmp_path):
    store = Store(tmp_path / "t.db")
    eng = Engine(store, None)
    now = datetime.now(timezone.utc)
    s = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "signal_id": "a",
        "generated_at": now.isoformat(),
        "execution_expires_at": (now + timedelta(seconds=60)).isoformat(),
    }
    eng.validate_signal(s)
    assert store.claim(s)
    assert not Store(tmp_path / "t.db").claim(s)
    s["execution_expires_at"] = (now + timedelta(seconds=90)).isoformat()
    with pytest.raises(ValueError):
        eng.validate_signal(s)


class FakeExchange:
    def __init__(self, uncertain):
        self.calls = 0
        self.uncertain = uncertain
        self.found = None

    async def lookup(self, *args):
        return self.found

    async def submit(self, *args):
        self.calls += 1
        raise ExchangeError(-1, self.uncertain)


def seed(store, kind="sl"):
    payload = {"symbol": "BTCUSDT"}
    store.execute(
        "INSERT INTO intents(id,trade_id,kind,payload,status) VALUES (?,?,?,?,?)",
        ("test", "t", kind, json.dumps(payload), "PLANNED"),
    )


@pytest.mark.asyncio
async def test_unknown_never_resubmitted_after_restart(tmp_path):
    store = Store(tmp_path / "t.db")
    seed(store)
    exchange = FakeExchange(True)
    await Engine(store, exchange).submit_intent("test")
    await Engine(Store(tmp_path / "t.db"), exchange).submit_intent("test", manual=True)
    assert exchange.calls == 1
    exchange.found = {"orderId": 12}
    await Engine(store, exchange).submit_intent("test")
    assert store.rows("SELECT status FROM intents")[0]["status"] == "ACK"
    assert exchange.calls == 1


@pytest.mark.asyncio
async def test_protection_retry_budget_persists(tmp_path):
    store = Store(tmp_path / "t.db")
    seed(store)
    exchange = FakeExchange(False)
    await Engine(store, exchange).submit_intent("test")
    assert exchange.calls == 2
    await Engine(Store(tmp_path / "t.db"), exchange).submit_intent("test")
    assert exchange.calls == 2
    await Engine(store, exchange).submit_intent("test", manual=True)
    assert exchange.calls == 3


@pytest.mark.asyncio
async def test_signing_and_mutation_gate(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"availableBalance": "100"})

    monkeypatch.setattr(settings, "binance_demo_api_key", __import__("pydantic").SecretStr("key"))
    monkeypatch.setattr(settings, "binance_demo_api_secret", __import__("pydantic").SecretStr("secret"))
    monkeypatch.setattr(settings, "trading_enabled", False)
    client = httpx.AsyncClient(base_url=DEMO, transport=httpx.MockTransport(handler))
    ex = Exchange(client)
    await ex.account()
    assert seen[0].headers["X-MBX-APIKEY"] == "key"
    assert "signature=" in str(seen[0].url)
    with pytest.raises(ValueError, match="TRADING_ENABLED"):
        await ex.submit("entry", {"symbol": "BTCUSDT"})
    assert len(seen) == 1
    await ex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-1006, -1007, -4116])
async def test_ambiguous_exchange_errors_are_never_definitive_rejections(monkeypatch, code):
    monkeypatch.setattr(settings, "binance_demo_api_key", __import__("pydantic").SecretStr("key"))
    monkeypatch.setattr(settings, "binance_demo_api_secret", __import__("pydantic").SecretStr("secret"))
    monkeypatch.setattr(settings, "trading_enabled", True)
    client = httpx.AsyncClient(
        base_url=DEMO, transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"code": code}))
    )
    ex = Exchange(client)
    with pytest.raises(ExchangeError) as caught:
        await ex.submit("entry", {"symbol": "BTCUSDT", "newClientOrderId": "one"})
    assert caught.value.uncertain
    await ex.close()


@pytest.mark.asyncio
async def test_success_http_without_order_id_is_unknown(monkeypatch):
    monkeypatch.setattr(settings, "binance_demo_api_key", __import__("pydantic").SecretStr("key"))
    monkeypatch.setattr(settings, "binance_demo_api_secret", __import__("pydantic").SecretStr("secret"))
    monkeypatch.setattr(settings, "trading_enabled", True)
    client = httpx.AsyncClient(
        base_url=DEMO, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 200}))
    )
    ex = Exchange(client)
    with pytest.raises(ExchangeError) as caught:
        await ex.submit("sl", {"symbol": "BTCUSDT", "clientAlgoId": "one"})
    assert caught.value.uncertain and caught.value.code == "INVALID_ORDER_ACK"
    await ex.close()
