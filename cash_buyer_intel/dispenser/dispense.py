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

    Mirrors `cli.cmd_push_tranchi.build_record()` so callers see the same
    fields whether they pull from the dispenser API or the push-tranchi flow.
    """
    markets_csv = r["markets_csv"] or ""
    markets = [m.strip() for m in markets_csv.split(",") if m.strip()]
    rec = {
        "external_id":     r["entity_id"],
        "name":            r["canonical_name"],
        "phone":           r["primary_phone"],
        "email":           r["primary_email"],
        "markets":         markets,
        "source":          "cash-buyer-scraper",
    }
    if r["primary_mailing"]:        rec["mailing_address"] = r["primary_mailing"]
    if r["entity_type"]:            rec["entity_type"] = r["entity_type"]
    if r["velocity_12m"] is not None: rec["velocity_12m"] = r["velocity_12m"]
    if r["median_purchase_price"] is not None:
        rec["median_purchase_price"] = r["median_purchase_price"]
    if r["property_type_mode"]:     rec["property_type"] = r["property_type_mode"]
    if r["activity_tier"]:          rec["activity_tier"] = r["activity_tier"]
    if r["confidence"] is not None: rec["skip_trace_confidence"] = r["confidence"]
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
           bs.velocity_12m, bs.median_purchase_price, bs.property_type_mode,
           bs.activity_tier, bs.recency_score,
           bc.primary_phone, bc.primary_email, bc.confidence,
           (SELECT GROUP_CONCAT(DISTINCT cs.market)
              FROM cash_sales cs
             WHERE cs.entity_id = be.entity_id AND cs.market IS NOT NULL) AS markets_csv
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
