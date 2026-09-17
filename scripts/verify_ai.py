"""Two real local Codex calls on labeled synthetic market fixtures. Never publish."""

import asyncio
import json
import time
from pathlib import Path
from common.config import settings
from signals.ai import AIClient, CANDIDATE_PROMPT, DIRECTION_PROMPT
from signals.schemas import Candidates, Directions, validate_candidates, select_directions
from signals.sources import item, rank_items


async def main():
    names = [
        "BTC",
        "ETH",
        "SOL",
        "BNB",
        "XRP",
        "DOGE",
        "ADA",
        "LINK",
        "AVAX",
        "LTC",
        "SUI",
        "AAVE",
        "HYPE",
        "DOT",
        "TRX",
        "NEAR",
        "UNI",
        "ETC",
        "FIL",
        "ATOM",
    ]
    bundles = rank_items(
        [
            item("fixture", s + "USDT", f"{s} synthetic testing evidence, not real market data", rank=i + 1)
            for i, s in enumerate(names)
        ],
        20,
    )
    evidence = []
    client = AIClient(lambda kind, payload: evidence.append({"kind": kind, "payload": payload}))
    candidates = await client.structured_completion(
        CANDIDATE_PROMPT,
        {"test_fixture": True, "candidates": bundles},
        Candidates,
        lambda r: validate_candidates(r, bundles),
    )
    markets = {
        c.symbol: {
            "symbol": c.symbol,
            "test_fixture": True,
            "reference_price": 100 + i,
            "snapshot_at": time.time(),
            "data_age_seconds": 0,
            "returns_pct": {"15m": 0.2 * i - 1},
            "data_quality": {"score": 0.5, "missing_fields": ["oi", "funding", "orderbook"]},
        }
        for i, c in enumerate(candidates)
    }
    result = await client.structured_completion(
        DIRECTION_PROMPT,
        {
            "test_fixture": True,
            "symbols": [
                {"symbol": c.symbol, "information": c.model_dump(), "market": markets[c.symbol]}
                for c in candidates
            ],
        },
        Directions,
        lambda r: r,
    )
    selected = select_directions(result, list(markets), markets)
    report = {
        "mode": "REAL_CODEX_SYNTHETIC_DATA_NOT_PUBLISHED",
        "model": settings.ai_model,
        "calls": client.calls,
        "usage": client.usage,
        "top10": [c.symbol for c in candidates],
        "selected": [d.model_dump() for d in selected],
        "evidence": evidence,
    }
    path = Path("runtime/verification/two_stage_ai.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps({k: v for k, v in report.items() if k not in ("evidence", "selected")}, ensure_ascii=False)
    )
    print("Selected count:", len(selected), "No signal publication.")


asyncio.run(main())
