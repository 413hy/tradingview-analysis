"""Explicit engineering defaults; no automatic strategy learning."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RankingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "binance": 1.0,
            "tradingview": 1.0,
            "telegram": 1.0,
        }
    )
    ranking_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "coins_technical_rating_bullish": 0.6,
            "coins_technical_rating_bearish": 0.6,
            "cex_technical_rating_bullish": 0.6,
            "cex_technical_rating_bearish": 0.6,
            "circulating_supply": 0.1,
            "market_cap": 0.2,
        }
    )
    freshness_decay_seconds: float = Field(3600, gt=0, le=604800)
    per_source_ranking_cap: float = Field(2, gt=0, le=100)
    diversity_bonus: float = Field(0.25, ge=0, le=10)
    evidence_limit: int = Field(8, ge=1, le=8)

    @field_validator("source_weights", "ranking_weights")
    @classmethod
    def valid_weights(cls, values):
        import math

        if any(not k or not math.isfinite(v) or not 0 <= v <= 100 for k, v in values.items()):
            raise ValueError("Weights must be finite values between 0 and 100")
        return values
