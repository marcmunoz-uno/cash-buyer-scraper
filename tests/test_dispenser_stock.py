"""cache_stock must report exactly what dispense_from_cache can deliver.

They diverged because dispense_from_cache INNER JOINs buyer_scores (it needs
recency_score to rank + ship) while cache_stock did not, so /api/dispense/stock
promised buyers that could never be dispensed."""

from __future__ import annotations

from cash_buyer_intel.dispenser.dispense import cache_stock, dispense_from_cache


def _seed_entity(conn, *, entity_id, market="Cleveland OH", with_score):
    conn.execute(
        "INSERT INTO buyer_entities (entity_id, canonical_name, first_seen, last_seen, total_sales)"
        " VALUES (?, ?, '2026-01-01', '2026-06-01', 3)",
        (entity_id, entity_id.upper()),
    )
    conn.execute(
        "INSERT INTO buyer_contacts (entity_id, primary_phone) VALUES (?, '555-0100')",
        (entity_id,),
    )
    conn.execute(
        "INSERT INTO cash_sales (sale_id, property_address, property_address_norm, market, state,"
        " sale_date, buyer_name_raw, buyer_name_norm, source, entity_id)"
        " VALUES (?, '1 A St', '1 a st', ?, 'OH', '2026-06-01', 'X', 'X', 'propstream', ?)",
        (f"sale_{entity_id}", market, entity_id),
    )
    if with_score:
        conn.execute(
            "INSERT INTO buyer_scores (entity_id, velocity_12m, velocity_3m, recency_score, activity_tier)"
            " VALUES (?, 3, 1, 2.5, 'hot')",
            (entity_id,),
        )
    conn.commit()


def test_stock_matches_dispensable_count(buyers_db):
    # Scored entity IS dispensable; unscored entity is NOT (no recency_score to rank).
    _seed_entity(buyers_db, entity_id="ent_scored", with_score=True)
    _seed_entity(buyers_db, entity_id="ent_unscored", with_score=False)

    stock = cache_stock(market="Cleveland OH", user_id="u1")
    dispensed = dispense_from_cache(user_id="u1", market="Cleveland OH", quantity=50)

    # Regression: stock used to report 2 while only 1 could be dispensed.
    assert stock == 1
    assert len(dispensed) == 1
    assert stock == len(dispensed)
