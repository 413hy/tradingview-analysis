from common.db import set_setting
import asyncio
import logging
import time
import uuid
from datetime import timedelta
from sqlalchemy import text, update
from common.db import Artifact, Cycle, Signal, Session, engine, now, get_setting, source_health
from signals.sources import PublicHTTP, Universe, binance_discovery, telegram_collect, rank_items
from signals import tradingview
from signals.optional import OptionalProviders
from common.errors import error_code
from signals.market import collect_market
from signals.ai import AIClient, CANDIDATE_PROMPT, DIRECTION_PROMPT
from signals.schemas import Candidates, Directions, validate_candidates, select_directions

log = logging.getLogger(__name__)
LOCK = 194201703


async def cycle(schedule_slot=None):
    with engine.connect() as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK}):
            return None
        try:
            if schedule_slot is not None:
                if get_setting("last_schedule_slot") == list(schedule_slot):
                    return None
                set_setting("last_schedule_slot", list(schedule_slot))
            return await run_cycle()
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK})


async def run_cycle():
    cid = str(uuid.uuid4())
    with Session.begin() as s:
        # Lock held: remaining RUNNING cycles belong to an interrupted worker.
        s.execute(
            update(Cycle)
            .where(Cycle.status == "RUNNING")
            .values(status="FAILED", finished_at=now(), error="Worker interrupted before completion")
        )
        s.add(Cycle(id=cid, status="RUNNING"))

    def save(kind, payload, symbol=None):
        with Session.begin() as s:
            s.add(Artifact(id=str(uuid.uuid4()), cycle_id=cid, kind=kind, symbol=symbol, payload=payload))

    ai = AIClient(save)
    optional = OptionalProviders(save)
    http = PublicHTTP()
    details = {}
    try:
        try:
            universe = await Universe.load(http)
        except Exception as exc:
            source_health("binance_universe", error_code(exc))
            raise
        source_health("binance_universe", count=len(universe.rows))
        items = []
        top_n = get_setting("ranking_top_n")
        providers = [
            ("binance", lambda: binance_discovery(http, universe, top_n)),
            ("telegram", lambda: telegram_collect(http, universe)),
            ("tradingview", lambda: tradingview.collect(universe, top_n)),
        ]

        async def collect(name, fn):
            if not get_setting("source_" + name):
                source_health(name, enabled=False)
                return []
            try:
                rows = await asyncio.wait_for(fn(), 180)
                source_health(name, count=len(rows))
                return rows
            except Exception as exc:
                source_health(name, error_code(exc))
                return []

        for rows in await asyncio.gather(*(collect(n, fn) for n, fn in providers)):
            items.extend(rows)
        items.extend(await optional.discovery(universe, top_n))
        with Session.begin() as s:
            s.add_all(
                Artifact(id=r["id"], cycle_id=cid, kind="source_item", symbol=r["symbol"], payload=r)
                for r in items
            )
        ranking_config = get_setting("ranking_config") or {}
        bundles = rank_items(items, get_setting("prefilter_count"), ranking_config)
        save("ranking_config", ranking_config)
        details.update(raw_count=len(items), prefilter_count=len(bundles))
        save("prefilter", {"candidates": bundles})
        if len(bundles) < 10:
            raise ValueError("Fewer than 10 real candidates")
        candidates = await ai.structured_completion(
            CANDIDATE_PROMPT, {"candidates": bundles}, Candidates, lambda r: validate_candidates(r, bundles)
        )
        information_packets = {}
        for c in candidates:
            bundle = next(b for b in bundles if b["symbol"] == c.symbol)
            information_packets[c.symbol] = dict(
                c.model_dump(),
                source_counts=bundle["source_counts"],
                ranking_evidence=bundle.get(
                    "ranking_evidence",
                    [
                        {
                            "source": r["source"],
                            "ranking_type": r["ranking_type"],
                            "rank": r["ranking_position"],
                        }
                        for r in bundle["evidence"]
                        if r["ranking_type"]
                    ],
                ),
            )
            save("candidate_packet", information_packets[c.symbol], c.symbol)
        names = [c.symbol for c in candidates]

        async def market(symbol):
            try:
                packet, raw = await collect_market(http, symbol)
                if packet.get("reference_price"):
                    packet["optional_enrichment"] = await optional.enrich(
                        symbol, universe.rows[symbol]["baseAsset"]
                    )
                save("market_raw", raw, symbol)
                save("market_packet", packet, symbol)
                return symbol, packet
            except Exception as exc:
                packet = {
                    "symbol": symbol,
                    "reference_price": None,
                    "snapshot_at": time.time(),
                    "data_age_seconds": 0,
                    "error": error_code(exc),
                }
                save("market_packet", packet, symbol)
                return symbol, packet

        markets = dict(await asyncio.gather(*(market(n) for n in names)))
        if not any(m.get("reference_price") for m in markets.values()):
            raise ValueError("No basic Binance market data")
        payload = {
            "symbols": [
                {
                    "symbol": c.symbol,
                    "information": information_packets[c.symbol],
                    "market": markets[c.symbol],
                }
                for c in candidates
            ]
        }

        def validate(result):
            if len(set(result.selected)) != len(result.selected) or not set(result.selected) <= set(names):
                raise ValueError("Selected symbols must be unique and belong to Top10")
            if len({d.symbol for d in result.analyzed}) != 10 or {d.symbol for d in result.analyzed} != set(
                names
            ):
                raise ValueError("Direction symbols differ from Top10")
            return result

        result = await ai.structured_completion(DIRECTION_PROMPT, payload, Directions, validate)
        for m in markets.values():
            m["data_age_seconds"] = max(0, time.time() - m.get("snapshot_at", 0))
        selected = select_directions(result, names, markets)
        for d in result.analyzed:
            save("direction_result", d.model_dump(), d.symbol)
        timestamp = now()
        details.update(
            top10=names,
            market_count=sum(bool(m.get("reference_price")) for m in markets.values()),
            ai_calls=ai.calls,
            ai_usage=ai.usage,
            signal_count=len(selected),
        )
        with Session.begin() as s:
            for rank, d in enumerate(selected, 1):
                sid = str(uuid.uuid5(uuid.UUID(cid), d.symbol))
                payload = dict(
                    d.model_dump(),
                    rank=rank,
                    signal_id=sid,
                    reference_price=markets[d.symbol]["reference_price"],
                    observed_at=markets[d.symbol]["snapshot_at"],
                )
                s.add(
                    Signal(
                        id=sid,
                        cycle_id=cid,
                        rank=rank,
                        symbol=d.symbol,
                        generated_at=timestamp,
                        valid_until=timestamp + timedelta(minutes=get_setting("signal_ttl_minutes")),
                        execution_expires_at=timestamp + timedelta(seconds=60),
                        payload=payload,
                    )
                )
            row = s.get(Cycle, cid)
            row.status, row.finished_at, row.details = "SUCCESS", timestamp, details
        log.info("cycle=%s success %s", cid, details)
        return cid
    except Exception as exc:
        details.update(ai_calls=ai.calls, ai_usage=ai.usage)
        with Session.begin() as s:
            row = s.get(Cycle, cid)
            row.status, row.finished_at, row.details = "FAILED", now(), details
            row.error = error_code(exc)
        log.error("cycle=%s failed type=%s", cid, type(exc).__name__)
        return cid
    finally:
        await http.close()


async def worker():
    last_bucket = None
    while True:
        interval = get_setting("cycle_minutes") * 60
        bucket = (interval, int(time.time() // interval))
        if get_setting("analysis_enabled") and bucket != last_bucket:
            last_bucket = bucket
            await cycle(schedule_slot=bucket)
        await asyncio.sleep(5)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(cycle() if "--once" in sys.argv else worker())
