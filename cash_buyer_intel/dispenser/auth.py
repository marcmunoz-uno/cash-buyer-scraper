"""Bearer-token auth for the dispenser.

Token sources (first wins):
  1. $DISPENSER_TOKEN env var
  2. DISPENSER_TOKEN=... line in ~/.openclaw/.env

Multiple tokens can be comma-separated for per-tenant identification later;
v0 just checks set membership.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Header, HTTPException, status


def _load_tokens() -> set[str]:
    raw = os.environ.get("DISPENSER_TOKEN")
    if not raw:
        env_file = Path.home() / ".openclaw" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("DISPENSER_TOKEN="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not raw:
        return set()
    return {t.strip() for t in raw.split(",") if t.strip()}


def require_bearer(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency — raises 401 if Authorization header is missing or unknown."""
    tokens = _load_tokens()
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DISPENSER_TOKEN is not configured on the server",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <token> required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(" ", 1)[1].strip()
    if presented not in tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented
