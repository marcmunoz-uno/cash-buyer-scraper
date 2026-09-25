"""push-tranchi-leads candidate selection.

Two bugs covered:
  1. The push-log filter excluded any address with ANY prior push row, so a
     FAILED push permanently suppressed a retry.
  2. The shared market clause hardcoded alias `m`, so
     --source-table cash_sales --market X raised "no such column: m.market".
"""

from __future__ import annotations

import pytest

from cash_buyer_intel.cli import _select_push_lead_candidates


def _seed_seller(conn, *, addr_norm="10 elm st cleveland oh 44101", photos=6, market="Cleveland OH"):
    conn.execute(
        "INSERT INTO motivated_sellers (lead_id, property_address, property_address_norm,"
        " market, lead_type, owner_name_raw, owner_name_norm, source, est_value)"
        " VALUES ('lead1', '10 Elm St, Cleveland, OH, 44101', ?, ?, 'vacant', 'OWNER', 'OWNER', 'propstream', 120000)",
        (addr_norm, market),
    )
    conn.execute(
        "INSERT INTO property_photos (address_norm, image_urls, photo_count, source)"
        " VALUES (?, '[\"https://photos.zillowstatic.com/fp/a.jpg\"]', ?, 'zillow')",
        (addr_norm, photos),
    )
    conn.commit()
    return addr_norm


def _log_push(conn, addr_norm, status):
    conn.execute(
        "INSERT INTO tranchi_push_log (address_norm, source_table, source_row_id, payload, response_status)"
        " VALUES (?, 'motivated_sellers', 'lead1', '{}', ?)",
        (addr_norm, status),
    )
    conn.commit()


def test_failed_push_is_still_a_candidate(buyers_db):
    addr = _seed_seller(buyers_db)
    _log_push(buyers_db, addr, "error")
    rows = _select_push_lead_candidates(
        buyers_db, source_table="motivated_sellers", market=None, min_photos=5, limit=10)
    # Regression: a failed push used to drop this address forever.
    assert [r["address_norm"] for r in rows] == [addr]


def test_successful_push_is_excluded(buyers_db):
    addr = _seed_seller(buyers_db)
    _log_push(buyers_db, addr, "ok")
    rows = _select_push_lead_candidates(
        buyers_db, source_table="motivated_sellers", market=None, min_photos=5, limit=10)
    assert rows == []


def test_cash_sales_market_filter_does_not_crash(buyers_db):
    # Regression: the cash_sales branch aliases the table `cs`, but the market
    # clause said `m.market`, raising OperationalError. Just running it is the test.
    buyers_db.execute(
        "INSERT INTO cash_sales (sale_id, property_address, property_address_norm, market, state,"
        " sale_date, buyer_name_raw, buyer_name_norm, source)"
        " VALUES ('s1', '5 Oak Ave, Akron, OH, 44301', '5 oak ave akron oh 44301', 'Akron OH', 'OH',"
        " '2026-05-01', 'ACME', 'ACME', 'propstream')")
    buyers_db.execute(
        "INSERT INTO property_photos (address_norm, image_urls, photo_count, source)"
        " VALUES ('5 oak ave akron oh 44301', '[\"https://photos.zillowstatic.com/fp/b.jpg\"]', 6, 'zillow')")
    buyers_db.commit()

    rows = _select_push_lead_candidates(
        buyers_db, source_table="cash_sales", market="Akron OH", min_photos=5, limit=10)
    assert [r["address_norm"] for r in rows] == ["5 oak ave akron oh 44301"]
