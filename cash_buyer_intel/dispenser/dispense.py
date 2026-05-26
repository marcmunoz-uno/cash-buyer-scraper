"""Core dispense logic — cache-first selection from buyers.db.

Selection rules:
  1. Entity must have a buyer_contacts row with phone OR email (we only
     dispense rows with real skip-traced contact info; no fabricated data).
  2. Entity must have at least one cash_sale in the requested market.
  3. Entity must not have been previously dispensed to this user_id (the
     `dispenses` UNIQUE constraint enforces this, but we also WHERE-filter
     so we don't waste a slot on an already-dispensed row).
  4. Sort by buyer_scores.recency_score DESC — top-quality first.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import open_buyers


# Our observed raw cost per fully-enriched skip-traced record (BatchData), from
# the 2026-05-26 burn-down: $50 → 652 records with both phone + email.
# The dispenser reports cost_usd; the caller applies their markup.
BATCHDATA_FULL_ENRICHMENT_COST = 0.077


def _row_to_buyer(r: sqlite3.Row) -> dict[str, Any]:
    """Shape a buyer_entities row into the dispense payload.

    Mirrors `cli.cmd_push_tranchi.build_record()` and the field reference in
    `Cash Buyer Pool Upload API — Documentation.md` so callers see exactly the
    same record format whether they pull from the dispenser API or get one
    pushed via the bulk operator workflow.
    """
    rec: dict[str, Any] = {
        "external_id": r["entity_id"],
        "name":        r["canonical_name"],
        "source":      "cash-buyer-scraper",
        "phone":       r["primary_phone"],
        "email":       r["primary_email"],
        "market":      r["market"],
        "state":       r["state"],
    }
    for k_src, k_api in (
        ("primary_mailing",              "mailing_address"),
        ("entity_type",                  "entity_type"),
        ("llc_authorized_agent",         "llc_agent"),
        ("total_sales",                  "total_sales"),
        ("velocity_12m",                 "velocity_12m"),
        ("velocity_3m",                  "velocity_3m"),
        ("median_purchase_price",        "median_price"),
        ("p25_price",                    "p25_price"),
        ("p75_price",                    "p75_price"),
        ("property_type_mode",           "property_type_mode"),
        ("zip_cluster_centroid_lat",     "zip_cluster_lat"),
        ("zip_cluster_centroid_lon",     "zip_cluster_lon"),
        ("zip_cluster_radius_miles",     "zip_cluster_radius"),
        ("recency_score",                "recency_score"),
        ("activity_tier",                "activity_tier"),
        ("confidence",                   "confidence"),
    ):
        v = r[k_src] if k_src in r.keys() else None
        if v is not None and v != "":
            rec[k_api] = v
    return rec


def cache_stock(market: str, user_id: str) -> int:
    """How many contact-bearing buyers we could dispense to this user right
    now from cache for this market. Used by the API to set expectations."""
    sql = """
    SELECT COUNT(*) FROM buyer_entities be
      JOIN buyer_contacts bc ON bc.entity_id = be.entity_id
     WHERE (bc.primary_phone IS NOT NULL OR bc.primary_email IS NOT NULL)
       AND EXISTS (SELECT 1 FROM cash_sales cs
                    WHERE cs.entity_id = be.entity_id AND cs.market = ?)
       AND NOT EXISTS (SELECT 1 FROM dispenses d
                        WHERE d.user_id = ? AND d.entity_id = be.entity_id)
    """
    with open_buyers(read_only=True) as conn:
        row = conn.execute(sql, (market, user_id)).fetchone()
    return int(row[0] or 0)


def dispense_from_cache(
    *,
    user_id: str,
    market: str,
    quantity: int,
    job_id: str | None = None,
) -> list[dict[str, Any]]:
    """Dispense up to `quantity` cache-bearing buyers to `user_id`.

    Returns the dispensed buyer records. Logs each one to the `dispenses`
    table (cost_usd = 0.0 for cache hits — we already paid the skip-trace
    cost on a previous enrichment run).
    """
    if quantity <= 0:
        return []

    sql = """
    SELECT be.entity_id, be.canonical_name, be.entity_type, be.primary_mailing,
           be.total_sales,
           bs.velocity_12m, bs.velocity_3m,
           bs.median_purchase_price, bs.p25_price, bs.p75_price,
           bs.property_type_mode,
           bs.zip_cluster_centroid_lat, bs.zip_cluster_centroid_lon,
           bs.zip_cluster_radius_miles,
           bs.activity_tier, bs.recency_score,
           bc.primary_phone, bc.primary_email,
           bc.llc_authorized_agent, bc.confidence,
           (SELECT cs.market FROM cash_sales cs
             WHERE cs.entity_id = be.entity_id AND cs.market IS NOT NULL
             ORDER BY cs.sale_date DESC LIMIT 1) AS market,
           (SELECT cs.state  FROM cash_sales cs
             WHERE cs.entity_id = be.entity_id AND cs.state  IS NOT NULL
             ORDER BY cs.sale_date DESC LIMIT 1) AS state
      FROM buyer_entities be
      JOIN buyer_scores bs   ON bs.entity_id = be.entity_id
      JOIN buyer_contacts bc ON bc.entity_id = be.entity_id
     WHERE (bc.primary_phone IS NOT NULL OR bc.primary_email IS NOT NULL)
       AND EXISTS (SELECT 1 FROM cash_sales cs
                    WHERE cs.entity_id = be.entity_id AND cs.market = ?)
       AND NOT EXISTS (SELECT 1 FROM dispenses d
                        WHERE d.user_id = ? AND d.entity_id = be.entity_id)
     ORDER BY bs.recency_score DESC
     LIMIT ?
    """

    dispensed: list[dict[str, Any]] = []
    with open_buyers() as conn:
        rows = conn.execute(sql, (market, user_id, quantity)).fetchall()
        for r in rows:
            buyer = _row_to_buyer(r)
            try:
                conn.execute(
                    """
                    INSERT INTO dispenses (user_id, entity_id, job_id, source, cost_usd)
                    VALUES (?, ?, ?, 'cache', 0.0)
                    """,
                    (user_id, buyer["external_id"], job_id),
                )
            except sqlite3.IntegrityError:
                # UNIQUE(user_id, entity_id) — concurrent dispense raced us. Skip.
                continue
            dispensed.append(buyer)
        conn.commit()
    return dispensed


def already_dispensed(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recently-dispensed buyers for this user. Useful for
    'show me what I've already received' UIs."""
    sql = """
    SELECT d.dispensed_at, d.source, d.cost_usd, d.job_id,
           be.entity_id, be.canonical_name, be.entity_type, be.primary_mailing,
           bc.primary_phone, bc.primary_email
      FROM dispenses d
      JOIN buyer_entities be   ON be.entity_id = d.entity_id
 LEFT JOIN buyer_contacts bc ON bc.entity_id = d.entity_id
     WHERE d.user_id = ?
     ORDER BY d.dispensed_at DESC
     LIMIT ?
    """
    with open_buyers(read_only=True) as conn:
        rows = conn.execute(sql, (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
