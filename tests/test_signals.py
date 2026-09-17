import time
import pytest
from signals.sources import Universe, item, rank_items
from signals.market import features, book_features
from signals.schemas import Directions, select_directions


def test_universe_filters_and_aliases():
    rows = [
        dict(
            symbol="BTCUSDT", baseAsset="BTC", quoteAsset="USDT", contractType="PERPETUAL", status="TRADING"
        ),
        dict(symbol="ETHUSDT", baseAsset="ETH", quoteAsset="USDT", contractType="PERPETUAL", status="BREAK"),
    ]
    u = Universe(rows)
    assert u.map("BINANCE:BTCUSDT.P") == "BTCUSDT"
    assert u.map("BTC/USD") == "BTCUSDT"
    assert u.map("ETH") is None
    assert u.extract("BTC rises, myBTC wallet does not imply ETH") == ["BTCUSDT"]


def test_dedupe_keeps_separate_symbols_and_distinct_rankings():
    a = item("telegram", "BTCUSDT", "BTC and ETH move together", identity="a")
    b = dict(a, id="b")
    c = dict(a, id="c", symbol="ETHUSDT")
    ranked = rank_items([a, b, c], 20)
    assert len(ranked) == 2
    assert all(len(r["evidence"]) == 1 for r in ranked)


def directions():
    return Directions.model_validate(
        {
            "selected": [],
            "analyzed": [
                {
                    "symbol": f"COIN{i}USDT",
                    "direction": "LONG",
                    "confidence": i,
                    "information_alignment": "mixed",
                    "market_state": "uncertain",
                    "supporting_factors": [],
                    "opposing_factors": [],
                    "key_levels": {"support": [], "resistance": []},
                    "invalidation_condition": "test",
                    "reason_summary": "test",
                }
                for i in range(10)
            ],
        }
    )


def test_empty_selection_falls_back_without_threshold_and_missing_price_excluded():
    d = directions()
    markets = {r.symbol: {"reference_price": 1, "data_age_seconds": 1} for r in d.analyzed}
    markets["COIN9USDT"]["reference_price"] = None
    assert select_directions(d, markets, markets)[0].symbol == "COIN8USDT"
    d.selected = ["COIN0USDT", "COIN1USDT"]
    assert [r.symbol for r in select_directions(d, markets, markets)] == ["COIN8USDT", "COIN7USDT"]


def test_wrong_top10_and_stale_prices_rejected():
    d = directions()
    markets = {r.symbol: {"reference_price": 1, "data_age_seconds": 181} for r in d.analyzed}
    with pytest.raises(ValueError):
        select_directions(d, markets, markets)
    d.analyzed[-1].symbol = d.analyzed[0].symbol
    with pytest.raises(ValueError):
        select_directions(d, markets, markets)


def test_features_use_closed_candles_and_no_nan():
    clock = time.time()
    rows = [
        [
            int((clock - 100 * 60 + i * 60) * 1000),
            100 + i,
            101 + i,
            99 + i,
            100 + i,
            5,
            int((clock - 99 * 60 + i * 60) * 1000) - 1,
            500,
            0,
            0,
            0,
            0,
        ]
        for i in range(100)
    ]
    before = features(rows, clock)
    rows.append([int(clock * 1000), 1, 999999, 1, 999999, 5, int((clock + 60) * 1000), 900000, 0, 0, 0, 0])
    assert features(rows, clock) == before
    assert before["rsi"] == 100
    assert before["trend"] == "bullish"


def test_depth_coverage_exposes_truncated_bands():
    result = book_features({"bids": [["99.99", "3"]], "asks": [["100.01", "2"]]}, 100)
    assert result["coverage"]["0.01"] is False
    assert result["imbalance_0.01"] > 0


@pytest.mark.asyncio
async def test_public_telegram_video_time_does_not_hide_publish_date(monkeypatch):
    import httpx
    from datetime import datetime, timezone
    from signals import sources
    from common.config import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "telegram_api_id", 0)
    monkeypatch.setattr(settings, "telegram_api_hash", SecretStr(""))
    monkeypatch.setattr(sources, "get_setting", lambda k: ["testchannel"] if k == "telegram_channels" else 60)
    published = datetime.now(timezone.utc).isoformat()
    html = f'''<div class="tgme_widget_message" data-post="testchannel/1">
    <time>00:10</time><div class="tgme_widget_message_text">BTC test</div>
    <time datetime="{published}"></time></div>'''
    http = sources.PublicHTTP()
    await http.client.aclose()
    http.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    )
    u = Universe(
        [
            dict(
                symbol="BTCUSDT",
                baseAsset="BTC",
                quoteAsset="USDT",
                contractType="PERPETUAL",
                status="TRADING",
            )
        ]
    )
    result = await sources.telegram_collect(http, u)
    assert len(result) == 1
    assert result[0]["raw_payload"]["transport"] == "public_preview"
    await http.close()


@pytest.mark.asyncio
async def test_optional_provider_rate_limit_does_not_block_binance():
    import httpx
    from signals.sources import PublicHTTP

    def handler(request):
        if request.url.host == "optional.example":
            return httpx.Response(429, headers={"retry-after": "60"})
        return httpx.Response(200, json={"ok": True})

    http = PublicHTTP()
    await http.client.aclose()
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await http.get("https://optional.example/api")
    assert await http.get("https://binance.example/api") == {"ok": True}
    await http.close()


def test_ranking_weights_configurable_and_cap_limits_duplicate_source():
    rows = [item("telegram", "BTCUSDT", f"distinct item {i}", identity=str(i)) for i in range(20)]
    rows.append(item("binance", "ETHUSDT", "real anomaly"))
    ranked = rank_items(rows, 20, {"source_weights": {"telegram": 0, "binance": 1}})
    assert [r["symbol"] for r in ranked] == ["ETHUSDT"]
    ranked = rank_items(rows, 20, {"per_source_ranking_cap": 0.1, "diversity_bonus": 0})
    assert all(r["score"] <= 0.1 for r in ranked)
    assert all(len(r["evidence"]) <= 8 for r in ranked)
    with pytest.raises(ValueError):
        rank_items(rows, 20, {"source_weights": {"telegram": float("nan")}})


@pytest.mark.asyncio
async def test_stale_market_field_excluded_but_raw_preserved():
    from signals.market import collect_market
    import time

    class HTTP:
        async def get(self, url, params):
            if url.endswith("/fapi/v2/ticker/price"):
                return {"symbol": "BTCUSDT", "price": "100", "time": int(time.time() * 1000)}
            if url.endswith("/fapi/v1/premiumIndex"):
                return {
                    "symbol": "BTCUSDT",
                    "markPrice": "100",
                    "lastFundingRate": ".01",
                    "time": int((time.time() - 3600) * 1000),
                }
            raise RuntimeError("fixture unavailable")

    packet, raw = await collect_market(HTTP(), "BTCUSDT")
    assert packet["reference_price"] == 100
    assert packet["funding"] is None
    assert "premium" in packet["data_quality"]["stale_fields"]
    assert raw["premium"]["lastFundingRate"] == ".01"


@pytest.mark.asyncio
async def test_one_unavailable_telegram_channel_does_not_discard_healthy_channel(monkeypatch):
    from signals import sources
    from datetime import datetime, timezone
    from common.config import settings
    from pydantic import SecretStr
    import httpx
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "telegram_api_id", 0)
    monkeypatch.setattr(settings, "telegram_api_hash", SecretStr(""))
    monkeypatch.setattr(
        sources,
        "get_setting",
        lambda k: ["missing_channel", "healthy_channel"] if k == "telegram_channels" else 60,
    )
    health = []
    monkeypatch.setattr(sources, "source_health", lambda *args, **kwargs: health.append((args, kwargs)))

    class Universe:
        def extract(self, text):
            return ["BTCUSDT"] if "BTC" in text else []

    def handler(request):
        if "missing_channel" in str(request.url):
            return httpx.Response(404)
        stamp = datetime.now(timezone.utc).isoformat()
        return httpx.Response(
            200,
            text=f'<div class="tgme_widget_message" data-post="healthy_channel/1"><div class="tgme_widget_message_text">BTC news</div><time datetime="{stamp}"></time></div>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await sources.telegram_collect(SimpleNamespace(client=client), Universe())
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT"
    assert health[0][0][1] == "HTTP_404"
