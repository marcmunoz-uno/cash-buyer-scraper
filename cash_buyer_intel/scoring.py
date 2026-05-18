"""Buyer velocity, buy-box, and recency scoring.

Recomputed by `cash-buyer-intel score`. Writes one row per entity into
`buyer_scores`. The activity_tier field is what `buyers --agent` filters on
by default.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime

from .db import open_buyers


def _months_since(iso_date: str, ref: datetime) -> float:
    try:
        d = datetime.fromisoformat(iso_date[:10])
    except ValueError:
        return math.inf
    return (ref - d).days / 30.4375


def _tier(velocity_3m: int, velocity_12m: int) -> str:
    if velocity_3m >= 1 and velocity_12m >= 3:
        return "hot"
    if velocity_12m >= 3:
        return "warm"
    if velocity_12m >= 1:
        return "cold"
    return "dormant"


def score_all(window_months: int = 12) -> dict:
    """Recompute buyer_scores for every entity that has at least one cash_sale."""
    ref = datetime.utcnow()
    written = 0

    with open_buyers(attach_sources=False) as conn:
        entity_ids = [r["entity_id"] for r in conn.execute(
            "SELECT DISTINCT entity_id FROM cash_sales WHERE entity_id IS NOT NULL"
        ).fetchall()]

        for eid in entity_ids:
            sales = conn.execute(
                """
                SELECT sale_date, sale_price, property_type, zip_code
                  FROM cash_sales
                 WHERE entity_id = ?
                """,
                (eid,),
            ).fetchall()

            v12 = 0
            v3 = 0
            prices: list[int] = []
            types: list[str] = []
            zips: list[str] = []
            recency = 0.0

            for s in sales:
                m = _months_since(s["sale_date"], ref)
                if m <= window_months:
                    v12 += 1
                if m <= 3:
                    v3 += 1
                if s["sale_price"]:
                    prices.append(int(s["sale_price"]))
                if s["property_type"]:
                    types.append(s["property_type"])
                if s["zip_code"]:
                    zips.append(s["zip_code"])
                recency += math.exp(-m / 6.0)

            median = int(statistics.median(prices)) if prices else None
            p25 = int(statistics.quantiles(prices, n=4)[0]) if len(prices) >= 4 else None
            p75 = int(statistics.quantiles(prices, n=4)[2]) if len(prices) >= 4 else None
            ptype = Counter(types).most_common(1)[0][0] if types else None
            tier = _tier(v3, v12)

            conn.execute(
                """
                INSERT OR REPLACE INTO buyer_scores
                  (entity_id, velocity_12m, velocity_3m,
                   median_purchase_price, p25_price, p75_price,
                   property_type_mode,
                   zip_cluster_centroid_lat, zip_cluster_centroid_lon,
                   zip_cluster_radius_miles,
                   recency_score, activity_tier, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, datetime('now'))
                """,
                (eid, v12, v3, median, p25, p75, ptype, recency, tier),
            )
            written += 1

        conn.commit()

    return {"scored": written, "window_months": window_months}
