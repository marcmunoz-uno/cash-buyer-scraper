"""FastAPI app — the dispenser HTTP surface.

Endpoints:
  POST   /api/dispense                — request N cash buyers for a user.
  GET    /api/dispense/jobs/{job_id}  — poll an async job's status + results.
  GET    /api/dispense/stock          — how many buyers available for a market.
  GET    /api/dispense/history        — already-dispensed list for a user.
  GET    /healthz                     — liveness probe.

Response envelope matches the rest of the cash-buyer-intel CLI:
  { "ok": bool, "data": ..., "errors": [...], "meta": {...} }
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_bearer
from .dispense import cache_stock, dispense_from_cache, already_dispensed
from .jobs import create_job, get_job, spawn_worker


app = FastAPI(
    title="cash-buyer-intel dispenser",
    description=(
        "On-demand cash-buyer dispenser for tranchi.ai. Tranchi posts a request "
        "with a market + quantity; we serve cache-bearing rows immediately and "
        "kick off an async BatchData fetch for any shortfall. Every dispensed "
        "row carries a real phone or email from skip-trace; no fabricated data."
    ),
    version="0.1.0",
)


class DispenseRequest(BaseModel):
    user_id:  str = Field(..., description="Stable identifier for the recipient.")
    market:   str = Field(..., description='Market label, e.g. "Cleveland OH".')
    quantity: int = Field(..., gt=0, le=500,
                          description="How many buyers to dispense (1-500).")
    fetch_if_short: bool = Field(
        default=True,
        description=(
            "If True and cache is short, kick off a BatchData fetch job for the "
            "shortfall. If False, return only cache hits + a stock_remaining count."
        ),
    )


def _envelope(ok: bool, data: Any = None,
              errors: list | None = None, meta: dict | None = None) -> dict:
    return {"ok": ok, "data": data, "errors": errors or [], "meta": meta or {}}


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    return _envelope(True, {"status": "ok"})


@app.get("/api/dispense/stock", tags=["dispense"])
def stock(
    market: str = Query(..., description='Market label, e.g. "Cleveland OH"'),
    user_id: str = Query(..., description="Recipient's stable id"),
    _token: str = Depends(require_bearer),
) -> dict:
    """How many cache-bearing, never-dispensed buyers are available right now."""
    n = cache_stock(market=market, user_id=user_id)
    return _envelope(True, {"market": market, "user_id": user_id, "stock": n})


@app.post("/api/dispense", tags=["dispense"])
def dispense(req: DispenseRequest, _token: str = Depends(require_bearer)) -> dict:
    """Dispense up to `quantity` buyers to `user_id` for `market`.

    Returns a synchronous payload of cache hits plus, if shortfall and
    fetch_if_short, a job_id for the async BatchData fulfillment.
    """
    cached = dispense_from_cache(
        user_id=req.user_id, market=req.market, quantity=req.quantity,
    )
    shortfall = req.quantity - len(cached)

    data: dict[str, Any] = {
        "dispensed":         cached,
        "dispensed_count":   len(cached),
        "shortfall":         shortfall,
        "estimated_cost_usd": 0.0,         # cache hits are $0 to us
        "job_id":            None,
    }

    if shortfall > 0 and req.fetch_if_short:
        job_id = create_job(
            user_id=req.user_id, market=req.market, quantity=shortfall,
        )
        spawn_worker(job_id)
        data["job_id"] = job_id
        # Honest cost estimate at observed yield (~22.7% match rate, ~$0.077 each).
        # The caller can use this for a Stripe pre-authorize / quote display.
        data["estimated_cost_usd"] = round(
            shortfall * 0.077, 4,
        )

    meta = {
        "market":   req.market,
        "user_id":  req.user_id,
        "quantity": req.quantity,
    }
    return _envelope(True, data, meta=meta)


@app.get("/api/dispense/jobs/{job_id}", tags=["dispense"])
def get_dispense_job(job_id: str, _token: str = Depends(require_bearer)) -> dict:
    """Poll a dispense job's status + the buyers it has dispensed so far."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return _envelope(True, job)


@app.get("/api/dispense/history", tags=["dispense"])
def history(
    user_id: str = Query(...),
    limit:   int = Query(100, ge=1, le=1000),
    _token:  str = Depends(require_bearer),
) -> dict:
    """List buyers already dispensed to this user (most recent first)."""
    rows = already_dispensed(user_id=user_id, limit=limit)
    return _envelope(True, {"user_id": user_id, "count": len(rows), "buyers": rows})
