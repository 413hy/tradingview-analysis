import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from pydantic import BaseModel, Field, ConfigDict
from trader.risk import number as D, reachable_tp, tp_price, sl_price
from trader.exchange import ExchangeError


class TradingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    margin: float = Field(10, gt=0, allow_inf_nan=False)
    leverage: int = Field(3, ge=1, le=5)
    tp: float = Field(0.5, gt=0, allow_inf_nan=False)
    sl: float = Field(2.7, gt=0, allow_inf_nan=False)
    reserve: float = Field(20, ge=20, allow_inf_nan=False)


def identity(tid, kind):
    return "tv-" + hashlib.sha256((tid + ":" + kind).encode()).hexdigest()[:28]


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Engine:
    def __init__(self, store, exchange):
        self.store, self.exchange = store, exchange
        self.lock = asyncio.Lock()

    def config(self):
        return TradingSettings.model_validate(self.store.get("trading_settings", {}))

    async def enter(self, signal):
        async with self.lock:
            self.validate_signal(signal)
            if self.store.get("entries_paused", False):
                return "PAUSED"
            if not self.store.claim(signal):
                return "DUPLICATE"
            sid = signal["signal_id"]
            try:
                result = await self._enter(signal)
            except ValueError as exc:
                result = str(exc) if str(exc).startswith("SKIP_") else "ERROR"
                if result == "ERROR":
                    self.store.event("ENTRY_ERROR", {"signal_id": sid, "type": type(exc).__name__}, True)
            except Exception as exc:
                result = "ERROR"
                self.store.event("ENTRY_ERROR", {"signal_id": sid, "type": type(exc).__name__}, True)
            self.store.execute("UPDATE signals SET status=? WHERE id=?", (result, sid))
            return result

    def validate_signal(self, s):
        import re

        if not re.fullmatch(r"[A-Z0-9]{1,24}USDT", s.get("symbol", "")) or s.get("direction") not in (
            "LONG",
            "SHORT",
        ):
            raise ValueError("Invalid signal")
        if not isinstance(s.get("signal_id"), str) or not s["signal_id"]:
            raise ValueError("Missing signal ID")
        generated, expires = timestamp(s["generated_at"]), timestamp(s["execution_expires_at"])
        if expires - generated != 60 or not generated <= time.time() < expires:
            raise ValueError("SKIP_SIGNAL_EXPIRED")

    async def candles(self, symbol):
        rows = await self.exchange.request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": 1500}
        )
        clock = time.time()
        rows = [r for r in rows if r[0] / 1000 >= clock - 86400 and r[6] / 1000 < clock]
        if len(rows) < 1438 or any(b[0] - a[0] != 60000 for a, b in zip(rows, rows[1:])):
            raise ValueError("SKIP_INCOMPLETE_TP_CANDLES")
        if clock - rows[-1][6] / 1000 > 90:
            raise ValueError("SKIP_STALE_TP_CANDLES")
        return [
            SimpleNamespace(
                open_time=datetime.fromtimestamp(r[0] / 1000, timezone.utc),
                close_time=datetime.fromtimestamp(r[0] / 1000, timezone.utc) + timedelta(minutes=1),
                open=D(r[1]),
                high=D(r[2]),
                low=D(r[3]),
                close=D(r[4]),
                volume=D(r[5]),
                timeframe="1m",
                completed=True,
            )
            for r in rows
        ]

    async def _enter(self, s):
        symbol, side, tid = s["symbol"], s["direction"], s["signal_id"]
        if self.store.rows(
            "SELECT id FROM trades WHERE symbol=? AND status NOT IN ('CLOSED','REJECTED','CANCELED')",
            (symbol,),
        ):
            return "SKIP_LOCAL_PENDING_OR_POSITION"
        if any(D(p["positionAmt"]) for p in await self.exchange.positions(symbol)):
            return "SKIP_EXISTING_POSITION"
        # Do not coexist with unmanaged open orders on the same symbol.
        orders = await self.exchange.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, True)
        algos = await self.exchange.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, True)
        if orders or algos:
            return "SKIP_EXISTING_ORDERS"
        config = self.config()
        ins = await self.exchange.instrument(symbol)
        bid, ask = await self.exchange.quote(symbol)
        price = ask if side == "LONG" else bid
        qty, leverage, margin = ins.size(
            price, margin_target=D(config.margin), leverage_target=D(config.leverage)
        )
        taker, maker = await self.exchange.fees(symbol)
        account = await self.exchange.account()
        if account.get("multiAssetsMargin", False):
            return "SKIP_MULTI_ASSET_MODE"
        if D(account["availableBalance"]) - margin - price * qty * taker < D(config.reserve):
            return "SKIP_BALANCE"
        reach = reachable_tp(
            side,
            price,
            qty,
            ins,
            taker,
            maker,
            ask - bid,
            await self.candles(symbol),
            targets=(D(config.tp),),
        )
        if not reach:
            return "SKIP_TP_UNREACHABLE"
        await self.exchange.configure(symbol, leverage)
        if self.store.get("entries_paused", False):
            return "SKIP_PAUSED"
        self.validate_signal(s)
        if any(D(p["positionAmt"]) for p in await self.exchange.positions(symbol)):
            return "SKIP_EXISTING_POSITION"
        if await self.exchange.request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol}, True
        ) or await self.exchange.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, True):
            return "SKIP_EXISTING_ORDERS"
        # Recheck balance immediately before the durable entry intent.
        account = await self.exchange.account()
        if account.get("multiAssetsMargin", False):
            return "SKIP_MULTI_ASSET_MODE"
        if D(account["availableBalance"]) - margin - price * qty * taker < D(config.reserve):
            return "SKIP_BALANCE"
        payload = {
            "config": config.model_dump(),
            "quantity": str(qty),
            "tick": str(ins.tick),
            "taker": str(taker),
            "maker": str(maker),
            "reachability": reach,
            "signal": s,
        }
        order = {
            "symbol": symbol,
            "side": "BUY" if side == "LONG" else "SELL",
            "type": "MARKET",
            "quantity": str(qty),
            "newClientOrderId": identity(tid, "entry"),
            "newOrderRespType": "RESULT",
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO trades VALUES (?,?,?,?,?,?)",
                (tid, symbol, side, "INTENT", time.time(), json.dumps(payload)),
            )
            db.execute(
                "INSERT INTO intents(id,trade_id,kind,payload,status) VALUES (?,?,?,?,?)",
                (identity(tid, "entry"), tid, "entry", json.dumps(order), "PLANNED"),
            )
        # No network operations between final deadline check and submit.
        try:
            self.validate_signal(s)
        except ValueError:
            self.store.update_trade(tid, "CANCELED", payload)
            self.store.execute("UPDATE intents SET status='CANCELED' WHERE trade_id=?", (tid,))
            return "SKIP_SIGNAL_EXPIRED"
        await self.submit_intent(identity(tid, "entry"))
        await self.reconcile(tid)
        return "ENTRY_PROCESSED"

    async def submit_intent(self, iid, manual=False, budget_override=None):
        row = self.store.rows("SELECT * FROM intents WHERE id=?", (iid,))[0]
        payload = json.loads(row["payload"])
        existing = await self.exchange.lookup(row["kind"], payload["symbol"], iid)
        if existing:
            self.store.execute(
                "UPDATE intents SET status='ACK',response=? WHERE id=?", (json.dumps(existing), iid)
            )
            return existing
        if row["status"] in ("UNKNOWN", "SUBMITTING", "ACK", "CANCELED"):
            return None  # Never infer that timeout means failure, never replay an entry.
        budget = 1 if row["kind"] == "entry" else 2
        if budget_override is not None:
            budget = budget_override
        if row["attempts"] >= budget and not manual:
            return None
        if row["kind"] == "entry":
            trade = self.store.rows("SELECT * FROM trades WHERE id=?", (row["trade_id"],))[0]
            try:
                self.validate_signal(json.loads(trade["payload"])["signal"])
                if self.store.get("entries_paused", False):
                    raise ValueError("SKIP_PAUSED")
            except ValueError:
                self.store.update_trade(row["trade_id"], "CANCELED", json.loads(trade["payload"]))
                self.store.execute("UPDATE intents SET status='CANCELED' WHERE id=?", (iid,))
                raise
        self.store.execute("UPDATE intents SET status='SUBMITTING',attempts=attempts+1 WHERE id=?", (iid,))
        try:
            result = await self.exchange.submit(row["kind"], payload)
            self.store.execute(
                "UPDATE intents SET status='ACK',response=? WHERE id=?", (json.dumps(result), iid)
            )
            return result
        except ExchangeError as exc:
            status = "UNKNOWN" if exc.uncertain else "REJECTED"
            self.store.execute("UPDATE intents SET status=? WHERE id=?", (status, iid))
            self.store.event(
                "ORDER_" + status, {"trade_id": row["trade_id"], "kind": row["kind"], "code": exc.code}, True
            )
            if not exc.uncertain and row["kind"] != "entry" and row["attempts"] + 1 < budget and not manual:
                return await self.submit_intent(iid, budget_override=budget_override)
            return None
        except Exception:
            self.store.execute("UPDATE intents SET status='UNKNOWN' WHERE id=?", (iid,))
            raise

    async def reconcile(self, tid):
        t = self.store.rows("SELECT * FROM trades WHERE id=?", (tid,))[0]
        data = json.loads(t["payload"])
        entry = await self.exchange.lookup("entry", t["symbol"], identity(tid, "entry"))
        if not entry and data.get("entry_order"):
            # Exchange history retention must not disable protection of a long-held position.
            entry = data["entry_order"]
        if not entry:
            intent = self.store.rows("SELECT * FROM intents WHERE id=?", (identity(tid, "entry"),))[0]
            if intent["status"] == "PLANNED" and intent["attempts"] == 0:
                # Never submitted before interruption: abandon instead of replaying old entry.
                self.store.update_trade(tid, "CANCELED", data)
                self.store.execute("UPDATE intents SET status='CANCELED' WHERE id=?", (intent["id"],))
            if intent["status"] == "REJECTED":
                self.store.update_trade(tid, "REJECTED", data)
            return
        qty = D(entry.get("executedQty", "0"))
        if not qty:
            if entry["status"] in ("CANCELED", "EXPIRED", "REJECTED"):
                self.store.update_trade(tid, "CANCELED", data)
            return
        if entry["status"] in ("NEW", "PARTIALLY_FILLED"):
            # Freeze remaining entry before protecting actual fills.
            await self.exchange.cancel("entry", t["symbol"], identity(tid, "entry"))
            entry = await self.exchange.lookup("entry", t["symbol"], identity(tid, "entry"))
            if not entry or entry["status"] in ("NEW", "PARTIALLY_FILLED"):
                raise ValueError("Entry cancellation not confirmed")
            qty = D(entry.get("executedQty", "0"))
            if not qty:
                self.store.update_trade(tid, "CANCELED", data)
                return
        if "entry_price" not in data:
            price = D(entry["avgPrice"])
            if price <= 0:
                raise ValueError("Missing actual average fill price")
            fills = await self.exchange.fills(t["symbol"], entry["orderId"])
            if not fills or sum((D(f["qty"]) for f in fills), D(0)) != qty:
                fee = price * qty * D(data["taker"])
                data["entry_fee_estimated"] = True
            else:
                if any(f["commissionAsset"] != "USDT" for f in fills):
                    raise ValueError("Non-USDT commission requires conversion")
                fee = sum((D(f["commission"]) for f in fills), D(0))
                data["entry_fee_estimated"] = False
            tp = tp_price(
                t["side"], price, qty, D(data["tick"]), D(data["config"]["tp"]), fee, D(data["maker"])
            )
            sl = sl_price(
                t["side"], price, qty, D(data["tick"]), fee, D(data["taker"]), D(data["config"]["sl"])
            )
            data.update(
                entry_price=str(price),
                quantity=str(qty),
                entry_fee=str(fee),
                tp=str(tp),
                sl=str(sl),
                entry_order_id=entry["orderId"],
                entry_order=entry,
                entry_fills=fills,
            )
            self.store.update_trade(tid, "OPEN", data)
            self.store.event(
                "POSITION_OPEN",
                {
                    "symbol": t["symbol"],
                    "side": t["side"],
                    "quantity": str(qty),
                    "entry": str(price),
                    "tp": str(tp),
                    "sl": str(sl),
                },
                True,
            )
        if data.get("entry_fee_estimated"):
            fills = await self.exchange.fills(t["symbol"], data["entry_order_id"])
            if (
                fills
                and sum((D(f["qty"]) for f in fills), D(0)) == D(data["quantity"])
                and all(f["commissionAsset"] == "USDT" for f in fills)
            ):
                data["entry_fee_actual"] = str(sum((D(f["commission"]) for f in fills), D(0)))
                data["entry_fills"] = fills
                data["entry_fee_estimated"] = False
                self.store.update_trade(tid, "OPEN", data)
        positions = await self.exchange.positions(t["symbol"])
        position = next((p for p in positions if D(p["positionAmt"])), None)
        if not position:
            await self.settle(t, data)
            return
        expected_sign = 1 if t["side"] == "LONG" else -1
        if D(position["positionAmt"]) * expected_sign <= 0 or abs(D(position["positionAmt"])) > D(
            data["quantity"]
        ):
            raise ValueError("Unexpected external position change")
        await self.protect(t, data)

    async def protect(self, t, data, manual=False):
        close_side = "SELL" if t["side"] == "LONG" else "BUY"
        # Persist immutable legs once. SL before TP; automatic budget stored across restarts.
        for kind in ("sl", "tp"):
            legs = self.store.rows(
                "SELECT * FROM intents WHERE trade_id=? AND kind=? ORDER BY rowid", (t["id"], kind)
            )
            iid = legs[-1]["id"] if legs else identity(t["id"], kind)
            if not legs:
                payload = {
                    "symbol": t["symbol"],
                    "side": close_side,
                    "quantity": data["quantity"],
                    "reduceOnly": "true",
                }
                if kind == "sl":
                    payload.update(
                        algoType="CONDITIONAL",
                        type="STOP_MARKET",
                        triggerPrice=data["sl"],
                        workingType="CONTRACT_PRICE",
                        clientAlgoId=iid,
                    )
                else:
                    payload.update(type="LIMIT", price=data["tp"], timeInForce="GTC", newClientOrderId=iid)
                self.store.execute(
                    "INSERT INTO intents(id,trade_id,kind,payload,status) VALUES (?,?,?,?,?)",
                    (iid, t["id"], kind, json.dumps(payload), "PLANNED"),
                )
            row = self.store.rows("SELECT * FROM intents WHERE id=?", (iid,))[0]
            if row["status"] == "ACK":
                actual = await self.exchange.lookup(kind, t["symbol"], iid)
                state = actual.get("algoStatus", actual.get("status")) if actual else "MISSING"
                if state in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "MISSING"):
                    key = "protection_missing:" + iid
                    if not self.store.get(key):
                        self.store.set(key, True)
                        self.store.event(
                            "PROTECTION_MISSING", {"trade_id": t["id"], "kind": kind, "state": state}, True
                        )
                if not manual or state not in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                    continue
                # Only an explicitly confirmed terminal order can be manually replaced.
                # Keep the old identity and response for reconciliation/settlement.
                iid = identity(t["id"], kind + ":manual:" + str(len(legs)))
                payload = json.loads(row["payload"])
                payload["clientAlgoId" if kind == "sl" else "newClientOrderId"] = iid
                self.store.execute(
                    "INSERT OR IGNORE INTO intents(id,trade_id,kind,payload,status) VALUES (?,?,?,?,?)",
                    (iid, t["id"], kind, json.dumps(payload), "PLANNED"),
                )
                row = self.store.rows("SELECT * FROM intents WHERE id=?", (iid,))[0]
            if row["status"] in ("SUBMITTING", "UNKNOWN"):
                await self.submit_intent(iid)  # lookup only
                continue
            exits = self.store.rows("SELECT * FROM intents WHERE id=?", (identity(t["id"], "exit_" + kind),))
            exit_attempts = exits[0]["attempts"] if exits else 0
            if exits and exits[0]["status"] in ("UNKNOWN", "SUBMITTING", "ACK"):
                await self.submit_intent(exits[0]["id"])
                continue
            leg_attempts = self.store.rows(
                "SELECT COALESCE(SUM(attempts),0) AS n FROM intents WHERE trade_id=? AND kind=?",
                (t["id"], kind),
            )[0]["n"]
            if leg_attempts + exit_attempts >= 2 and not manual:
                continue
            # Original strategy: if a missed target has already been crossed, exit reduce-only.
            last = await self.exchange.request("GET", "/fapi/v1/ticker/price", {"symbol": t["symbol"]})
            price = D(last["price"])
            crossed = (
                (price <= D(data["sl"]) if t["side"] == "LONG" else price >= D(data["sl"]))
                if kind == "sl"
                else (price >= D(data["tp"]) if t["side"] == "LONG" else price <= D(data["tp"]))
            )
            if crossed:
                await self.target_exit(t, data, kind, manual=manual)
                continue
            await self.submit_intent(
                iid, manual=manual, budget_override=max(0, 2 - exit_attempts - leg_attempts + row["attempts"])
            )

    async def target_exit(self, t, data, kind, manual=False):
        iid = identity(t["id"], "exit_" + kind)
        positions = await self.exchange.positions(t["symbol"])
        sign = 1 if t["side"] == "LONG" else -1
        if any(D(p["positionAmt"]) * sign < 0 for p in positions):
            raise ValueError("Unexpected position direction at target exit")
        qty = sum((abs(D(p["positionAmt"])) for p in positions), D(0))
        if qty > D(data["quantity"]):
            raise ValueError("Unexpected external size increase")
        if not qty:
            return
        payload = {
            "symbol": t["symbol"],
            "side": "SELL" if t["side"] == "LONG" else "BUY",
            "type": "MARKET",
            "quantity": str(qty),
            "reduceOnly": "true",
            "newClientOrderId": iid,
        }
        self.store.execute(
            "INSERT OR IGNORE INTO intents(id,trade_id,kind,payload,status) VALUES (?,?,?,?,?)",
            (iid, t["id"], "target_exit", json.dumps(payload), "PLANNED"),
        )
        exit_row = self.store.rows("SELECT * FROM intents WHERE id=?", (iid,))[0]
        leg_attempts = self.store.rows(
            "SELECT COALESCE(SUM(attempts),0) AS n FROM intents WHERE trade_id=? AND kind=?", (t["id"], kind)
        )[0]["n"]
        if exit_row["status"] in ("UNKNOWN", "SUBMITTING", "ACK"):
            await self.submit_intent(iid)
            return
        total = leg_attempts + exit_row["attempts"]
        if total >= 2 and not manual:
            return
        # Target-exit shares remaining automatic budget with original protection leg.
        await self.submit_intent(iid, manual=manual, budget_override=max(0, 2 - leg_attempts))

    async def settle(self, t, data):
        intents = self.store.rows("SELECT * FROM intents WHERE trade_id=? AND kind!='entry'", (t["id"],))
        for intent in intents:
            if intent["kind"] in ("sl", "tp"):
                await self.exchange.cancel(intent["kind"], t["symbol"], intent["id"])
        # Attribute settlement to our exact order IDs; missing exit fills remain pending.
        # Never mark a vanished position closed with only the entry fee as its PnL.
        entry_fills = await self.exchange.fills(t["symbol"], data["entry_order_id"])
        if not entry_fills:
            entry_fills = data.get("entry_fills", [])
        exits = []
        for intent in intents:
            kind = intent["kind"]
            query_kind = "sl" if kind == "sl" else "tp"
            order = await self.exchange.lookup(query_kind, t["symbol"], intent["id"])
            if not order:
                continue
            oid = order.get("actualOrderId") if kind == "sl" else order.get("orderId")
            if oid and str(oid) != "0":
                found = await self.exchange.fills(t["symbol"], oid)
                if any(
                    str(f["orderId"]) != str(oid)
                    or f.get("side") != ("SELL" if t["side"] == "LONG" else "BUY")
                    for f in found
                ):
                    raise ValueError("Exit fill identity mismatch")
                exits.extend(found)
        fills = {str(f["id"]): f for f in entry_fills + exits}
        entry_qty = sum((D(f["qty"]) for f in entry_fills), D(0))
        exit_fills = {str(f["id"]): f for f in exits}
        exit_qty = sum((D(f["qty"]) for f in exit_fills.values()), D(0))
        if entry_qty != D(data["quantity"]) or exit_qty != D(data["quantity"]):
            raise ValueError("Settlement pending: exit fills incomplete or external position closure")
        if any(f["commissionAsset"] != "USDT" for f in fills.values()):
            raise ValueError("Non-USDT commission requires conversion")
        data["settlement_fills"] = list(fills.values())
        data["net_pnl"] = str(sum((D(f["realizedPnl"]) - D(f["commission"]) for f in fills.values()), D(0)))
        data["pnl_scope"] = "realized_trading_pnl_minus_commissions; funding_not_included"
        # Terminal ledger + notification commit atomically.
        with self.store.connect() as db:
            db.execute("UPDATE trades SET status='CLOSED',payload=? WHERE id=?", (json.dumps(data), t["id"]))
            notice = json.dumps(
                {"symbol": t["symbol"], "net_pnl": data["net_pnl"], "scope": data["pnl_scope"]},
                ensure_ascii=False,
            )
            db.execute(
                "INSERT INTO events(at,kind,payload) VALUES (?,?,?)", (time.time(), "POSITION_CLOSED", notice)
            )
            db.execute("INSERT INTO outbox(text) VALUES (?)", ("POSITION_CLOSED\n" + notice,))

    async def monitor(self):
        async with self.lock:
            for row in self.store.rows(
                "SELECT id FROM trades WHERE status NOT IN ('CLOSED','REJECTED','CANCELED')"
            ):
                try:
                    await self.reconcile(row["id"])
                except Exception as exc:
                    key = "incident:" + row["id"] + ":" + type(exc).__name__
                    if not self.store.get(key):
                        self.store.set(key, True)
                        self.store.event(
                            "MONITOR_ERROR", {"trade_id": row["id"], "type": type(exc).__name__}, True
                        )
