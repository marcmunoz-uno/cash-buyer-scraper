"""Async dispense jobs — fetch from BatchData when cache is short.

Flow:
  1. API creates a dispense_jobs row in 'queued', returns job_id immediately.
  2. Background thread picks it up:
     a. Marks it 'running'.
     b. Subprocess: cash-buyer-intel sync-batchdata --query <market> --limit <N+30%>
        (over-fetch a bit since not every BatchData row dedups to a new entity.)
     c. Subprocess: cash-buyer-intel dedup
     d. Subprocess: cash-buyer-intel score
     e. Subprocess: cash-buyer-intel enrich-batchdata --limit <shortfall>
     f. Re-runs dispense_from_cache for the shortfall; logs each new dispense
        with source='fetch' and cost_usd=BATCHDATA_FULL_ENRICHMENT_COST.
     g. Marks job 'complete' (or 'failed' with error).

Caller polls GET /api/dispense/jobs/{job_id} for status + final buyers.

v0 limitations (documented, not bugs):
  - In-process threading; if the server restarts mid-job, the job stays
    'running' forever. Add a 'mark stale jobs as failed' cron later.
  - Subprocess shells out to the same `cash-buyer-intel` CLI on PATH; no
    in-process dedup/score/enrich. That's deliberate — keeps the side-effects
    behind the existing tested command boundary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import open_buyers
from .dispense import (
    BATCHDATA_FULL_ENRICHMENT_COST,
    cache_stock,
    dispense_from_cache,
)


def _cli_bin() -> str:
    """Resolve the cash-buyer-intel CLI for subprocess calls.

    Prefers the venv-local binary so we run the same code we're hosted in.
    """
    here = Path(__file__).resolve()
    venv_bin = here.parents[2] / ".venv" / "bin" / "cash-buyer-intel"
    if venv_bin.exists():
        return str(venv_bin)
    return shutil.which("cash-buyer-intel") or "cash-buyer-intel"


def _set_status(job_id: str, **fields: Any) -> None:
    """Patch dispense_jobs row by job_id."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with open_buyers() as conn:
        conn.execute(
            f"UPDATE dispense_jobs SET {cols} WHERE job_id = ?",
            (*fields.values(), job_id),
        )
        conn.commit()


def create_job(*, user_id: str, market: str, quantity: int,
               filters: dict | None = None) -> str:
    """Insert a 'queued' dispense_jobs row and return its job_id."""
    job_id = f"disp_{uuid.uuid4().hex[:12]}"
    request = {"market": market, "quantity": quantity, "filters": filters or {}}
    with open_buyers() as conn:
        conn.execute(
            """
            INSERT INTO dispense_jobs (job_id, user_id, request_json, status)
            VALUES (?, ?, ?, 'queued')
            """,
            (job_id, user_id, json.dumps(request)),
        )
        conn.commit()
    return job_id


def get_job(job_id: str) -> dict | None:
    """Return job row + the buyers dispensed under this job."""
    with open_buyers(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM dispense_jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        buyers = conn.execute(
            """
            SELECT d.dispensed_at, d.source, d.cost_usd,
                   be.entity_id, be.canonical_name, be.entity_type, be.primary_mailing,
                   bc.primary_phone, bc.primary_email
              FROM dispenses d
              JOIN buyer_entities be   ON be.entity_id = d.entity_id
         LEFT JOIN buyer_contacts bc ON bc.entity_id = d.entity_id
             WHERE d.job_id = ?
             ORDER BY d.dispensed_at
            """,
            (job_id,),
        ).fetchall()
        job["buyers"] = [dict(b) for b in buyers]
    return job


def _run_cli(*args: str, timeout: int = 1800) -> tuple[bool, str]:
    """Subprocess wrapper. Returns (ok, stderr_tail_or_stdout)."""
    try:
        proc = subprocess.run(
            [_cli_bin(), *args, "--agent"],
            check=True, capture_output=True, text=True, timeout=timeout,
        )
        return True, proc.stdout[:500]
    except FileNotFoundError as e:
        return False, f"cash-buyer-intel binary not found: {e}"
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or e.stdout or "")[-500:]
        return False, f"CLI failed ({e.returncode}): {tail}"
    except subprocess.TimeoutExpired as e:
        return False, f"CLI timed out after {timeout}s ({' '.join(args)})"


def _worker(job_id: str) -> None:
    """Background fulfillment for a single job. Long-running."""
    job = get_job(job_id)
    if not job:
        return
    request = json.loads(job["request_json"])
    user_id  = job["user_id"]
    market   = request["market"]
    quantity = int(request["quantity"])

    _set_status(job_id, status="running")

    # Step 1 — drain whatever cache is already there. (Usually 0 for a job
    # since the API drains cache *before* creating the job, but a race is
    # possible if a parallel fetch landed records mid-flight.)
    cache_first = dispense_from_cache(
        user_id=user_id, market=market, quantity=quantity, job_id=job_id,
    )
    shortfall = quantity - len(cache_first)
    _set_status(job_id, cached_count=len(cache_first))

    if shortfall <= 0:
        _set_status(
            job_id, status="complete",
            completed_at=datetime.utcnow().isoformat(timespec="seconds"),
        )
        return

    # Step 2 — over-fetch from BatchData to give dedup + enrichment headroom.
    # Historical yield: 22.7% of attempts produce a full-enriched record.
    fetch_n = max(shortfall * 5, 100)
    ok, msg = _run_cli("sync-batchdata", "--query", market,
                       "--market", market, "--limit", str(fetch_n))
    if not ok:
        _set_status(job_id, status="failed", error=f"sync-batchdata: {msg}",
                    completed_at=datetime.utcnow().isoformat(timespec="seconds"))
        return

    # Step 3 — dedup + score so new rows get entity_ids + buyer_scores.
    for cmd in (("dedup",), ("score",)):
        ok, msg = _run_cli(*cmd)
        if not ok:
            _set_status(job_id, status="failed", error=f"{cmd[0]}: {msg}",
                        completed_at=datetime.utcnow().isoformat(timespec="seconds"))
            return

    # Step 4 — enrich (skip-trace) up to a bit over the shortfall so we have
    # candidates with phone/email to actually dispense.
    enrich_n = max(shortfall * 5, 50)
    ok, msg = _run_cli("enrich-batchdata", "--limit", str(enrich_n))
    if not ok:
        _set_status(job_id, status="failed", error=f"enrich-batchdata: {msg}",
                    completed_at=datetime.utcnow().isoformat(timespec="seconds"))
        return

    # Step 5 — drain the now-richer cache. These rows came from a paid fetch,
    # so cost_usd = BATCHDATA_FULL_ENRICHMENT_COST per record (our raw cost).
    fetched = dispense_from_cache(
        user_id=user_id, market=market, quantity=shortfall, job_id=job_id,
    )
    if fetched:
        with open_buyers() as conn:
            conn.execute(
                """
                UPDATE dispenses SET source = 'fetch', cost_usd = ?
                 WHERE job_id = ? AND source = 'cache' AND entity_id IN ({})
                """.format(",".join("?" * len(fetched))),
                (BATCHDATA_FULL_ENRICHMENT_COST, job_id,
                 *[b["external_id"] for b in fetched]),
            )
            conn.commit()

    total_cost = round(BATCHDATA_FULL_ENRICHMENT_COST * len(fetched), 4)
    _set_status(
        job_id, status="complete",
        fetched_count=len(fetched),
        total_cost_usd=total_cost,
        completed_at=datetime.utcnow().isoformat(timespec="seconds"),
    )


def spawn_worker(job_id: str) -> None:
    """Fire-and-forget background thread for a job."""
    t = threading.Thread(target=_worker, args=(job_id,), daemon=True,
                         name=f"dispense-{job_id}")
    t.start()
