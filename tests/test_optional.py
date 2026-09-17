import pytest
from pydantic import SecretStr
from common.config import settings
from signals import optional
from signals.sources import Universe


@pytest.fixture
def universe():
    return Universe(
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


@pytest.mark.asyncio
async def test_optional_no_keys_performs_no_network(monkeypatch, universe):
    for name in optional.REGISTRY:
        monkeypatch.setattr(settings, name + "_api_key", SecretStr(""))
    monkeypatch.setattr(optional, "source_health", lambda *a, **k: None)
    monkeypatch.setattr(optional, "get_setting", lambda k: True)
    providers = optional.OptionalProviders(lambda *a: None)

    async def never(*args, **kw):
        raise AssertionError("network must not run")

    providers.request = never
    assert await providers.discovery(universe, 20) == []
    result = await providers.enrich("BTCUSDT", "BTC")
    assert all(r["provider_status"] == "disabled" for r in result.values())


@pytest.mark.asyncio
async def test_optional_failure_isolated_and_secret_not_in_health(monkeypatch, universe):
    health = []
    monkeypatch.setattr(optional, "source_health", lambda *a, **k: health.append((a, k)))
    monkeypatch.setattr(optional, "get_setting", lambda k: True)
    for name in optional.REGISTRY:
        monkeypatch.setattr(settings, name + "_api_key", SecretStr("test-secret"))
    providers = optional.OptionalProviders(lambda *a: None)

    async def fail(*a, **kw):
        raise ValueError("token=test-secret")

    providers.request = fail
    assert await providers.discovery(universe, 20) == []
    assert "test-secret" not in str(health)


@pytest.mark.asyncio
async def test_nansen_requires_chain_address_identity(monkeypatch, universe):
    providers = optional.OptionalProviders(lambda *a: None)

    async def data(*args, **kwargs):
        return {"data": [{"chain": "ethereum", "token_address": "0x1", "token_symbol": "BTC"}]}

    providers.request = data
    monkeypatch.setattr(settings, "nansen_token_map", {})
    assert await providers.nansen(universe, 20) == []
    monkeypatch.setattr(settings, "nansen_token_map", {"ethereum:0x1": "BTCUSDT"})
    assert (await providers.nansen(universe, 20))[0]["symbol"] == "BTCUSDT"
