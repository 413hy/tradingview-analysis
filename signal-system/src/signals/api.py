import secrets
from datetime import datetime
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import select, text
from common.config import settings
from common.db import Session, Artifact, Signal, Health, latest_cycle, get_setting, now

app = FastAPI(title="AI Crypto Direction Signals", version="1.0.0")


def authorize(authorization: str = Header(default="")):
    token = settings.api_token.get_secret_value()
    if not token or not secrets.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Unauthorized")


@app.get("/health")
def health():
    with Session() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok"}


def serialize(row):
    return dict(
        row.payload,
        generated_at=row.generated_at.isoformat(),
        valid_until=row.valid_until.isoformat(),
        execution_expires_at=row.execution_expires_at.isoformat(),
    )


@app.get("/api/v1/signals/latest", dependencies=[Depends(authorize)])
def latest():
    success = latest_cycle(True)
    if not success:
        raise HTTPException(404, "No successful cycle yet")
    current = latest_cycle()
    with Session() as s:
        rows = s.scalars(select(Signal).where(Signal.cycle_id == success.id).order_by(Signal.rank)).all()
    stale = (
        current.status == "FAILED"
        or now() >= rows[0].valid_until
        or (now() - success.finished_at).total_seconds() > get_setting("cycle_minutes") * 60 + 180
    )
    return {
        "cycle_id": success.id,
        "generated_at": success.finished_at.isoformat(),
        "time_horizon_minutes": [30, 60],
        "is_stale": stale,
        "signals": [serialize(r) for r in rows],
    }


@app.get("/api/v1/signals/history", dependencies=[Depends(authorize)])
def history(
    limit: int = Query(50, ge=1, le=500),
    symbol: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    until: datetime | None = None,
):
    if any(v is not None and v.tzinfo is None for v in (from_, until)):
        raise HTTPException(422, "History timestamps require an explicit timezone")
    if from_ and until and from_ > until:
        raise HTTPException(422, "from must be before until")
    q = select(Signal).order_by(Signal.generated_at.desc(), Signal.rank).limit(limit)
    if symbol:
        q = q.where(Signal.symbol == symbol)
    if from_:
        q = q.where(Signal.generated_at >= from_)
    if until:
        q = q.where(Signal.generated_at <= until)
    with Session() as s:
        return [dict(serialize(r), cycle_id=r.cycle_id) for r in s.scalars(q)]


@app.get("/api/v1/cycles/latest", dependencies=[Depends(authorize)])
def cycles():
    c = latest_cycle()
    if not c:
        raise HTTPException(404, "No cycles")
    result = {k: getattr(c, k) for k in ("id", "status", "started_at", "finished_at", "error", "details")}
    with Session() as s:
        rows = s.scalars(
            select(Artifact).where(
                Artifact.cycle_id == c.id,
                Artifact.kind.in_(["prefilter", "market_packet", "candidate_packet"]),
            )
        ).all()
        result["prefiltered_candidates"] = [r.payload for r in rows if r.kind == "prefilter"]
        result["top10"] = [r.symbol for r in rows if r.kind == "candidate_packet"]
        result["market_status"] = [
            {
                "symbol": r.symbol,
                "data_quality": r.payload.get("data_quality"),
                "error": r.payload.get("error"),
            }
            for r in rows
            if r.kind == "market_packet"
        ]
        result["signals"] = [
            serialize(r)
            for r in s.scalars(select(Signal).where(Signal.cycle_id == c.id).order_by(Signal.rank))
        ]
    return result


@app.get("/api/v1/candidates/latest", dependencies=[Depends(authorize)])
def candidates():
    c = latest_cycle()
    if not c:
        raise HTTPException(404, "No cycle")
    with Session() as s:
        return {
            "cycle_id": c.id,
            "candidates": [
                r.payload
                for r in s.scalars(
                    select(Artifact).where(Artifact.cycle_id == c.id, Artifact.kind == "candidate_packet")
                )
            ],
        }


@app.get("/api/v1/sources/status", dependencies=[Depends(authorize)])
def sources():
    with Session() as s:
        return [
            {
                k: getattr(r, k)
                for k in (
                    "source",
                    "enabled",
                    "last_success_at",
                    "last_error_at",
                    "consecutive_failures",
                    "error",
                    "details",
                )
            }
            for r in s.scalars(select(Health))
        ]


@app.get("/api/v1/settings", dependencies=[Depends(authorize)])
def settings_view():
    from common.db import DEFAULTS

    return {k: get_setting(k) for k in DEFAULTS}


@app.put("/api/v1/settings/{key}", dependencies=[Depends(authorize)])
def settings_update(key: str, body: dict):
    from signals.settings_service import compare_and_set

    if set(body) != {"value", "expected"}:
        raise HTTPException(422, "Required fields: value, expected")
    try:
        return {"key": key, "value": compare_and_set(key, body["value"], body["expected"], "internal_api")}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
