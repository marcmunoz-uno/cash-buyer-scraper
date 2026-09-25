"""Shared fixtures. Redirects the buyers DB to a temp file so tests never
touch ~/cash-buyer-intel/buyers.db."""

from __future__ import annotations

from pathlib import Path

import pytest

import cash_buyer_intel.db as db


@pytest.fixture()
def buyers_db(tmp_path, monkeypatch):
    """Point BUYERS_DB at a temp file, create the schema, yield an open conn."""
    path = tmp_path / "buyers.db"
    monkeypatch.setattr(db, "BUYERS_DB", path)
    db.init_db()
    with db.open_buyers(attach_sources=False) as conn:
        yield conn
