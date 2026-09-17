from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Levels(Strict):
    support: list[float]
    resistance: list[float]


class Information(Strict):
    symbol: str
    candidate_rank: int = Field(ge=1, le=10)
    candidate_reason: str
    attention_state: Literal["rising", "high", "normal", "falling"]
    information_bias: Literal["BULLISH", "BEARISH", "MIXED"]
    key_information: list[str]
    bullish_arguments: list[str]
    bearish_arguments: list[str]
    contradictions: list[str]
    key_levels: Levels
    events: list[str]
    evidence_ids: list[str]
    information_freshness: Literal["fresh", "mixed", "stale"]
    summary: str


class Candidates(Strict):
    candidates: list[Information] = Field(min_length=10, max_length=10)


class Direction(Strict):
    symbol: str
    direction: Literal["LONG", "SHORT"]
    confidence: float = Field(ge=0, le=100, allow_inf_nan=False)
    information_alignment: Literal["supports", "conflicts", "mixed"]
    market_state: Literal["trend", "range", "breakout", "breakdown", "squeeze", "uncertain"]
    supporting_factors: list[str]
    opposing_factors: list[str]
    key_levels: Levels
    invalidation_condition: str
    reason_summary: str = Field(min_length=1, max_length=600)


class Directions(Strict):
    analyzed: list[Direction] = Field(min_length=10, max_length=10)
    selected: list[str] = Field(max_length=3)


def validate_candidates(result, bundles):
    allowed = {b["symbol"]: {i["id"] for i in b["evidence"]} for b in bundles}
    names = [p.symbol for p in result.candidates]
    if len(set(names)) != 10 or not set(names) <= allowed.keys():
        raise ValueError("Candidate symbols not unique or outside supplied universe")
    if {p.candidate_rank for p in result.candidates} != set(range(1, 11)):
        raise ValueError("Invalid candidate ranks")
    for p in result.candidates:
        if not p.evidence_ids or not set(p.evidence_ids) <= allowed[p.symbol]:
            raise ValueError("Evidence IDs do not belong to this symbol")
    return sorted(result.candidates, key=lambda p: p.candidate_rank)


def select_directions(result, symbols, markets):
    if len({d.symbol for d in result.analyzed}) != 10 or {d.symbol for d in result.analyzed} != set(symbols):
        raise ValueError("Direction symbols differ from Top10")
    eligible = [
        d
        for d in result.analyzed
        if (markets[d.symbol].get("reference_price") or 0) > 0
        and markets[d.symbol]["data_age_seconds"] <= 180
    ]
    if not eligible:
        raise ValueError("No fresh Binance prices for publication")
    count = max(1, len(result.selected))
    return sorted(eligible, key=lambda d: (-d.confidence, d.symbol))[:count]
