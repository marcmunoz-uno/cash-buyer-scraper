"""SQLite layer for cash-buyer-intel.

Strategy mirrors SQLite-CLI-propertydb-mesh: each upstream PP CLI owns its own
database under ~/.local/share/<cli>-pp-cli/data.db. This repo keeps a *separate*
database (~/cash-buyer-intel/buyers.db) for data the PP CLIs don't produce —
the deduped buyer entities, scoring, outreach state, and the BatchData
owned-cache (BatchData's PP CLI is lookup-only and has no source DB).

At query time we ATTACH the PP databases as additional schemas. PP CLIs are
replaceable, their schemas can drift, and our query layer stays the single
place that knows how to JOIN across them.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

BUYERS_DB = Path.home() / "cash-buyer-intel" / "buyers.db"

# Default paths the PP CLIs use. Override via CASH_BUYER_INTEL_<CLI>_DB env vars
# if any have been redirected with --db.
#
# Roles:
#   attom         → data IN (saleshistory; the primary tier-A cash-sale source)
#   googlemaps    → data IN (geocode buyer mailing addresses for ZIP-cluster scoring)
#   blooio        → mixed (chat history JOIN: "have we already iMessaged this buyer?")
#   tranchi       → mixed (POST cash buyers; GET upload status — "already pushed?" filter)
#
# batchdata is lookup-only — no sync, no source DB. We use the owned-cache
# pattern (table `batchdata_cache` in main) populated by `enrich-batchdata`.
# telegram is action-only (alerts via `telegram-pp-cli send-message`) and not attached.
DEFAULT_SOURCES = {
    "attom":      Path.home() / ".local" / "share" / "attom-pp-cli"      / "data.db",
    "googlemaps": Path.home() / ".local" / "share" / "googlemaps-pp-cli" / "data.db",
    "blooio":     Path.home() / ".local" / "share" / "blooio-pp-cli"     / "data.db",
    "tranchi":    Path.home() / ".local" / "share" / "tranchi-pp-cli"    / "data.db",
    "census":     Path.home() / ".local" / "share" / "census-geocoder-pp-cli" / "data.db",
}


def resolve_source_path(name: str) -> Path:
    env = os.environ.get(f"CASH_BUYER_INTEL_{name.upper()}_DB")
    return Path(env) if env else DEFAULT_SOURCES[name]


def normalize_address(addr: str) -> str:
    """Lowercase, strip non-alphanumeric except spaces, collapse whitespace.

    Not USPS-grade — adequate as a JOIN key against BatchData/ATTOM canonical
    addresses for an MVP. The real canonical form comes from
    `batchdata-pp-cli address verify` when we need it.
    """
    s = re.sub(r"[^a-z0-9 ]", " ", addr.lower())
    return " ".join(s.split())


_LLC_SUFFIXES = re.compile(
    r"\b(l\.?l\.?c\.?|inc\.?|incorporated|corp\.?|corporation|trust|ltd\.?|llp|lp|co\.?|company)\b",
    re.IGNORECASE,
)


def normalize_buyer_name(name: str) -> str:
    """Normalize a buyer name into a stable comparison key.

    Uppercase, strip punctuation, drop entity suffixes (LLC/INC/TRUST/...),
    collapse whitespace. The full original string is preserved in
    cash_sales.buyer_name_raw; this is just the dedup key.
    """
    s = name.upper()
    s = _LLC_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(s.split())


@contextmanager
def open_buyers(read_only: bool = False, attach_sources: bool = True):
    """Open the buyers DB and ATTACH any source DBs that exist on disk."""
    BUYERS_DB.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{BUYERS_DB}?mode={'ro' if read_only else 'rwc'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("norm_addr", 1, normalize_address)
    conn.create_function("norm_buyer", 1, normalize_buyer_name)
    try:
        if attach_sources:
            for name in DEFAULT_SOURCES:
                p = resolve_source_path(name)
                if p.exists():
                    conn.execute(f"ATTACH DATABASE '{p}' AS {name}")
        yield conn
    finally:
        conn.close()


# Expected table + column names per source. PP CLIs name tables after the API
# resource and columns after the response fields. Override per-machine via env
# vars: CASH_BUYER_INTEL_<SOURCE>_<KEY>=<value>.
SOURCE_SCHEMA: dict[str, dict[str, str]] = {
    "attom": {
        "saleshistory_table": "attom_saleshistory",
        "property_table":     "attom_property",
        "address_col":        "address",
        "sale_amount_col":    "amount",
        "sale_date_col":      "sale_date",
        "mortgage_col":       "mortgage_amount",
        "buyer_col":          "buyer_name",
        "owner_address_col":  "owner_address",
    },
    # BatchData is point-lookup-only — the PP CLI has no `sync` command and
    # never builds its own SQLite. cash-buyer-intel owns the cache table
    # (`batchdata_cache` in main) populated by `enrich-batchdata`.
    "batchdata": {
        "schema":             "main",
        "property_table":     "batchdata_cache",
        "address_col":        "address_norm",
        "phone_col":          "primary_phone",
        "cash_buyer_col":     "is_cash_buyer",
    },
    "tranchi": {
        "leads_table":        "tranchi_leads",
        "external_id_col":    "external_id",
    },
    "blooio": {
        "messages_table":     "blooio_messages",
        "chats_table":        "blooio_chats",
    },
    "googlemaps": {
        "geocode_table":      "googlemaps_geocode",
    },
}


def schema_value(source: str, key: str) -> str:
    env_key = f"CASH_BUYER_INTEL_{source.upper()}_{key.upper()}"
    return os.environ.get(env_key) or SOURCE_SCHEMA[source][key]


def probe_source(conn, source: str) -> dict[str, Any]:
    """Inspect an attached source DB. Returns tables/columns + readiness."""
    out: dict[str, Any] = {"source": source, "attached": False, "tables": [], "columns": {}, "missing": []}
    if source not in SOURCE_SCHEMA:
        out["missing"].append(f"unknown source: {source}")
        return out

    attached = {r["name"] for r in conn.execute("SELECT name FROM pragma_database_list").fetchall()}
    schema = SOURCE_SCHEMA[source].get("schema", source)
    if schema not in attached:
        out["missing"].append("source DB not attached at runtime")
        return out
    out["attached"] = True

    tables = [r["name"] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    out["tables"] = tables

    spec = SOURCE_SCHEMA[source]
    expected_tables = {v for k, v in spec.items() if k.endswith("_table")}
    for tbl in expected_tables:
        if tbl not in tables:
            out["missing"].append(f"table missing: {tbl}")
            continue
        cols = [r["name"] for r in conn.execute(f"PRAGMA {schema}.table_info({tbl})").fetchall()]
        out["columns"][tbl] = cols

    out["ready"] = not out["missing"]
    return out


SCHEMA_SQL = """
-- Deed-level cash sales — one row per (property, date, buyer) observation.
-- Loaded from ATTOM (tier A), BatchData (tier A), or county portals (tier B).
CREATE TABLE IF NOT EXISTS cash_sales (
    sale_id                TEXT PRIMARY KEY,
    property_address       TEXT NOT NULL,
    property_address_norm  TEXT NOT NULL,
    city                   TEXT,
    state                  TEXT,
    zip_code               TEXT,
    market                 TEXT,
    property_type          TEXT,
    sale_date              TEXT NOT NULL,
    sale_price             INTEGER,
    mortgage_amount        INTEGER,
    buyer_name_raw         TEXT NOT NULL,
    buyer_name_norm        TEXT NOT NULL,
    buyer_mailing_addr     TEXT,
    seller_name            TEXT,
    source                 TEXT NOT NULL,
    source_record_id       TEXT,
    entity_id              TEXT,
    loaded_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS cash_sales_market    ON cash_sales(market);
CREATE INDEX IF NOT EXISTS cash_sales_state     ON cash_sales(state);
CREATE INDEX IF NOT EXISTS cash_sales_entity    ON cash_sales(entity_id);
CREATE INDEX IF NOT EXISTS cash_sales_sale_date ON cash_sales(sale_date);
CREATE INDEX IF NOT EXISTS cash_sales_buyer     ON cash_sales(buyer_name_norm);

-- One row per resolved buyer entity (after dedup across name aliases).
CREATE TABLE IF NOT EXISTS buyer_entities (
    entity_id         TEXT PRIMARY KEY,
    canonical_name    TEXT NOT NULL,
    entity_type       TEXT,
    primary_mailing   TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    total_sales       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS buyer_entity_aliases (
    entity_id         TEXT NOT NULL,
    alias_name_norm   TEXT NOT NULL,
    alias_name_raw    TEXT NOT NULL,
    source            TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias_name_norm)
);

CREATE TABLE IF NOT EXISTS buyer_contacts (
    entity_id            TEXT PRIMARY KEY,
    primary_phone        TEXT,
    primary_email        TEXT,
    llc_authorized_agent TEXT,
    skip_traced_at       TEXT,
    confidence           REAL
);

CREATE TABLE IF NOT EXISTS buyer_scores (
    entity_id                  TEXT PRIMARY KEY,
    velocity_12m               INTEGER NOT NULL,
    velocity_3m                INTEGER NOT NULL,
    median_purchase_price      INTEGER,
    p25_price                  INTEGER,
    p75_price                  INTEGER,
    property_type_mode         TEXT,
    zip_cluster_centroid_lat   REAL,
    zip_cluster_centroid_lon   REAL,
    zip_cluster_radius_miles   REAL,
    recency_score              REAL NOT NULL,
    activity_tier              TEXT NOT NULL,
    scored_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS buyer_outreach (
    outreach_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT NOT NULL,
    wholesaler_user_id  TEXT,
    channel             TEXT NOT NULL,
    direction           TEXT NOT NULL,
    summary             TEXT,
    response_status     TEXT,
    occurred_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS buyer_outreach_entity ON buyer_outreach(entity_id);

-- Motivated-seller leads from PropStream (Pre-Foreclosure, Vacant+Equity+Absentee, etc.).
-- Seller-side leads, semantically distinct from cash_sales (which are buyer-side).
-- One row per property × lead-list-type observation. Same property can appear in
-- multiple lists (e.g. Vacant AND Pre-Foreclosure) — primary key includes lead_type.
-- Stub added in v0.2; ingestion via `ingest-propstream-list`.
CREATE TABLE IF NOT EXISTS motivated_sellers (
    lead_id              TEXT PRIMARY KEY,            -- hash of (address_norm, lead_type, list_export_date)
    property_address     TEXT NOT NULL,
    property_address_norm TEXT NOT NULL,
    city                 TEXT,
    state                TEXT,
    zip_code             TEXT,
    market               TEXT,
    property_type        TEXT,
    lead_type            TEXT NOT NULL,               -- 'pre-foreclosure' | 'vacant' | 'absentee' | 'high-equity' | 'tired-landlord' | etc.
    distress_signals     TEXT,                        -- comma-separated quickList flags from PropStream
    foreclosure_factor   REAL,
    total_open_loans     INTEGER,
    est_remaining_balance INTEGER,
    est_value            INTEGER,
    est_equity           INTEGER,
    est_ltv              REAL,
    last_sale_date       TEXT,
    last_sale_amount     INTEGER,
    owner_name_raw       TEXT NOT NULL,
    owner_name_norm      TEXT NOT NULL,
    owner_mailing_addr   TEXT,
    owner_occupied       INTEGER,                     -- 0/1
    latitude             REAL,                        -- geocoded coords from source
    longitude            REAL,                        --   skip Census geocode round-trip in enrich-photos
    source               TEXT NOT NULL,               -- 'propstream' for now; future: 'county-portal-direct'
    source_record_id     TEXT,                        -- PropStream APN or other stable id
    loaded_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS motivated_sellers_market    ON motivated_sellers(market);
CREATE INDEX IF NOT EXISTS motivated_sellers_lead_type ON motivated_sellers(lead_type);
CREATE INDEX IF NOT EXISTS motivated_sellers_owner     ON motivated_sellers(owner_name_norm);

-- Photo enrichment results, keyed by address. Populated by `enrich-photos`.
CREATE TABLE IF NOT EXISTS property_photos (
    address_norm    TEXT PRIMARY KEY,
    image_urls      TEXT NOT NULL,            -- JSON array of URLs
    photo_count     INTEGER NOT NULL,
    source          TEXT NOT NULL,            -- 'zillow' | 'streetview' | etc.
    source_url      TEXT,                     -- where they were sourced from
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS property_photos_source ON property_photos(source);

-- Tranchi.ai push log — what we've shipped, when, and the response status.
CREATE TABLE IF NOT EXISTS tranchi_push_log (
    push_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address_norm     TEXT NOT NULL,
    source_table     TEXT NOT NULL,            -- 'motivated_sellers' | 'cash_sales'
    source_row_id    TEXT,
    payload          TEXT NOT NULL,            -- JSON we sent
    response_status  TEXT,
    response_body    TEXT,
    image_count      INTEGER,
    pushed_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS tranchi_push_log_addr ON tranchi_push_log(address_norm);

-- Owned-cache for BatchData (lookup-only PP CLI; no source SQLite of its own).
CREATE TABLE IF NOT EXISTS batchdata_cache (
    address_norm     TEXT PRIMARY KEY,
    raw_response     TEXT NOT NULL,
    primary_phone    TEXT,
    is_cash_buyer    INTEGER,
    owner_name       TEXT,
    owner_state      TEXT,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    """Create buyers.db and run the schema. Idempotent."""
    with open_buyers(attach_sources=False) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
