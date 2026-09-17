import json
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from trader.engine import Engine, identity
from trader.store import Store
from trader.risk import Instrument


class SimulatedDemo:
    """Deterministic exchange fixture; no network/real orders."""

    def __init__(self):
        self.orders = {}
        self.submissions = []
        self.open = False
        self.actual_quantity = "0.3"
        self.entry_id = 1

    async def positions(self, symbol=None):
        return [{"symbol": "TESTUSDT", "positionAmt": self.actual_quantity if self.open else "0"}]

    async def account(self):
        return {"availableBalance": "100"}

    async def instrument(self, symbol):
        return Instrument(symbol, D(".01"), D(".001"), D(".001"), D(5), D(100), D(100), D(20), D(1))

    async def quote(self, symbol):
        return D("99.99"), D(100)

    async def fees(self, symbol):
        return D(".0005"), D(".0002")

    async def configure(self, *args):
        pass

    async def lookup(self, kind, symbol, cid):
        return self.orders.get(cid)

    async def submit(self, kind, payload):
        self.submissions.append((kind, dict(payload)))
        cid = payload.get("newClientOrderId", payload.get("clientAlgoId"))
        if kind == "entry":
            self.open = True
            row = {"orderId": 1, "status": "FILLED", "avgPrice": "100", "executedQty": self.actual_quantity}
        else:
            row = {"orderId": len(self.submissions), "status": "NEW", "algoStatus": "NEW"}
        self.orders[cid] = row
        return row

    async def fills(self, *args):
        return [
            {
                "qty": self.actual_quantity,
                "commission": "0.015",
                "commissionAsset": "USDT",
                "realizedPnl": "0",
                "orderId": 1,
                "side": "BUY",
                "id": 1,
            }
        ]

    async def request(self, method, path, params=None, signed=False):
        if path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            return []
        if path == "/fapi/v1/ticker/price":
            return {"price": "100"}
        if path == "/fapi/v1/userTrades":
            return await self.fills()
        raise AssertionError(path)

    async def cancel(self, kind, symbol, cid):
        if cid in self.orders:
            self.orders[cid]["status"] = "CANCELED"
            self.orders[cid]["algoStatus"] = "CANCELED"


def signal():
    at = datetime.now(timezone.utc)
    return {
        "signal_id": "fixture-id",
        "symbol": "TESTUSDT",
        "direction": "LONG",
        "generated_at": at.isoformat(),
        "execution_expires_at": (at + timedelta(seconds=60)).isoformat(),
    }


async def reachable_candles(symbol):
    at = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return [
        SimpleNamespace(
            open_time=at - timedelta(minutes=i + 1),
            close_time=at - timedelta(minutes=i),
            low=D(110),
            high=D(111),
            volume=D(2),
            timeframe="1m",
            completed=True,
        )
        for i in range(10)
    ]


@pytest.mark.asyncio
async def test_entry_freezes_actual_fill_targets_and_protects_sl_first(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    s = signal()
    assert await eng.enter(s) == "ENTRY_PROCESSED"
    assert [k for k, _ in ex.submissions] == ["entry", "sl", "tp"]
    assert ex.submissions[1][1]["type"] == "STOP_MARKET"
    assert ex.submissions[1][1]["reduceOnly"] == "true"
    assert ex.submissions[2][1]["type"] == "LIMIT"
    assert ex.submissions[2][1]["reduceOnly"] == "true"
    before = json.loads(store.rows("SELECT payload FROM trades")[0]["payload"])
    store.set("trading_settings", {"margin": 20, "leverage": 4, "tp": 1, "sl": 5})
    store.set("entries_paused", True)
    await eng.monitor()
    after = json.loads(store.rows("SELECT payload FROM trades")[0]["payload"])
    assert before["tp"] == after["tp"] and before["sl"] == after["sl"]
    assert len(ex.submissions) == 3
    assert await eng.enter(s) == "PAUSED"
    store.set("entries_paused", False)
    assert await eng.enter(s) == "DUPLICATE"


@pytest.mark.asyncio
async def test_existing_position_skips_without_order(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    ex.open = True
    assert await Engine(store, ex).enter(signal()) == "SKIP_EXISTING_POSITION"
    assert not ex.submissions


@pytest.mark.asyncio
async def test_missing_acknowledged_protection_alerts_without_replacement(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    s = signal()
    await eng.enter(s)
    ex.orders[identity(s["signal_id"], "sl")]["algoStatus"] = "CANCELED"
    await eng.monitor()
    await eng.monitor()
    assert len(ex.submissions) == 3
    assert len(store.rows("SELECT * FROM events WHERE kind='PROTECTION_MISSING'")) == 1


@pytest.mark.asyncio
async def test_never_submitted_intent_abandoned_on_recovery(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    tid = "never-sent"
    store.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?)", (tid, "TESTUSDT", "LONG", "INTENT", time.time(), "{}")
    )
    store.execute(
        "INSERT INTO intents(id,trade_id,kind,payload,status) VALUES (?,?,?,?,?)",
        (identity(tid, "entry"), tid, "entry", "{}", "PLANNED"),
    )
    await Engine(store, ex).monitor()
    assert store.rows("SELECT status FROM trades")[0]["status"] == "CANCELED"
    assert not ex.submissions


@pytest.mark.asyncio
async def test_missing_exit_fills_never_fabricates_closed_pnl(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    s = signal()
    await eng.enter(s)
    ex.open = False
    await eng.monitor()
    row = store.rows("SELECT * FROM trades")[0]
    assert row["status"] == "OPEN"
    assert "net_pnl" not in json.loads(row["payload"])
    assert store.rows("SELECT * FROM events WHERE kind='MONITOR_ERROR'")


@pytest.mark.asyncio
async def test_manual_replaces_confirmed_canceled_leg_once_and_keeps_original(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    s = signal()
    await eng.enter(s)
    old = identity(s["signal_id"], "sl")
    ex.orders[old]["algoStatus"] = "CANCELED"
    t = store.rows("SELECT * FROM trades")[0]
    data = json.loads(t["payload"])
    await eng.protect(t, data, manual=True)
    assert len(ex.submissions) == 4
    replacement = ex.submissions[-1][1]
    assert replacement["clientAlgoId"] != old
    assert replacement["triggerPrice"] == data["sl"]
    assert replacement["reduceOnly"] == "true"
    assert len(store.rows("SELECT * FROM intents WHERE kind='sl'")) == 2
    await eng.monitor()
    assert len(ex.submissions) == 4


@pytest.mark.asyncio
async def test_manual_cannot_replace_unknown_or_missing_ack(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    s = signal()
    await eng.enter(s)
    del ex.orders[identity(s["signal_id"], "sl")]
    t = store.rows("SELECT * FROM trades")[0]
    await eng.protect(t, json.loads(t["payload"]), manual=True)
    assert len(ex.submissions) == 3


@pytest.mark.asyncio
async def test_successful_settlement_exact_fills_only_and_single_notification(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    await eng.enter(signal())
    original = ex.fills

    async def fills(symbol, oid):
        if oid == 1:
            return await original(symbol, oid)
        if oid == 3:
            return [
                {
                    "qty": ".3",
                    "commission": ".006",
                    "commissionAsset": "USDT",
                    "realizedPnl": ".53",
                    "orderId": 3,
                    "side": "SELL",
                    "id": 2,
                }
            ]
        return []

    ex.fills = fills
    ex.open = False
    await eng.monitor()
    await eng.monitor()
    t = store.rows("SELECT * FROM trades")[0]
    assert t["status"] == "CLOSED"
    assert D(json.loads(t["payload"])["net_pnl"]) == D(".509")
    assert len(store.rows("SELECT * FROM events WHERE kind='POSITION_CLOSED'")) == 1


@pytest.mark.asyncio
async def test_cached_filled_entry_keeps_monitoring_after_exchange_history_expires(tmp_path):
    store = Store(tmp_path / "t.db")
    ex = SimulatedDemo()
    eng = Engine(store, ex)
    eng.candles = reachable_candles
    s = signal()
    await eng.enter(s)
    del ex.orders[identity(s["signal_id"], "entry")]
    ex.orders[identity(s["signal_id"], "sl")]["algoStatus"] = "CANCELED"
    await eng.monitor()
    assert store.rows("SELECT * FROM events WHERE kind='PROTECTION_MISSING'")
    assert len(ex.submissions) == 3
