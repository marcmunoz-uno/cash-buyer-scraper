"""Buyer entity resolution.

Same human/LLC appears across deed records as `JOHN A SMITH`, `Smith, John`,
`Smith Holdings LLC`, `SMITH HOLDINGS, L.L.C.`. Collapse them into one
`buyer_entities` row.

Strategy:
  1. Normalize name (handled by db.normalize_buyer_name — drops LLC/INC/TRUST
     suffixes, uppercases, strips punctuation).
  2. Exact match on normalized form → same entity.
  3. Fuzzy match (token-set ratio) above threshold AND same primary mailing
     address → same entity.
  4. LLC officer lookup (v0.4) — Secretary of State scrapers to collapse
     `<Person> Holdings LLC` and `<Person> Properties LLC` when they share
     a registered agent. Not implemented in v0.1.

Every alias is preserved in `buyer_entity_aliases` so a merge is auditable.
"""

from __future__ import annotations

import hashlib
from difflib import SequenceMatcher

from .db import normalize_buyer_name, open_buyers


def _entity_id(canonical_name: str, mailing: str | None) -> str:
    key = f"{canonical_name}|{(mailing or '').strip().lower()}"
    return "ent_" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _token_set_ratio(a: str, b: str) -> float:
    """Cheap fuzzy-match without a thefuzz / rapidfuzz dependency.

    Sort tokens in both strings, run SequenceMatcher on the joined form.
    Good enough for "John A Smith" vs "Smith John A" vs "John Smith".
    """
    ta = " ".join(sorted(set(a.split())))
    tb = " ".join(sorted(set(b.split())))
    return SequenceMatcher(None, ta, tb).ratio()


def resolve_entities(threshold: float = 0.85) -> dict:
    """Run dedup over every cash_sales row.

    For each unique buyer_name_norm, find or create a buyer_entity. Returns
    a summary dict for the CLI to print/serialize.
    """
    created = 0
    aliases_added = 0
    sales_linked = 0

    with open_buyers(attach_sources=False) as conn:
        rows = conn.execute("""
            SELECT
              buyer_name_norm,
              buyer_name_raw,
              buyer_mailing_addr,
              source,
              MIN(sale_date) AS first_seen,
              MAX(sale_date) AS last_seen,
              COUNT(*)       AS sale_count
            FROM cash_sales
            GROUP BY buyer_name_norm, COALESCE(buyer_mailing_addr, '')
        """).fetchall()

        # First pass: exact-match consolidation by normalized name.
        # We do not collapse across different mailing addresses unless fuzzy
        # match agrees on both axes — that's the second pass.
        existing: list[tuple[str, str, str | None]] = []  # (entity_id, canonical, mailing)
        for r in rows:
            norm = r["buyer_name_norm"]
            mailing = r["buyer_mailing_addr"]
            raw = r["buyer_name_raw"]
            source = r["source"]

            matched_id: str | None = None
            for eid, ecanon, emailing in existing:
                if ecanon == norm and (emailing or "") == (mailing or ""):
                    matched_id = eid
                    break
                if _token_set_ratio(ecanon, norm) >= threshold and (emailing or "") == (mailing or ""):
                    matched_id = eid
                    break

            if matched_id is None:
                matched_id = _entity_id(norm, mailing)
                existing.append((matched_id, norm, mailing))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO buyer_entities
                      (entity_id, canonical_name, entity_type, primary_mailing,
                       first_seen, last_seen, total_sales)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        matched_id,
                        norm,
                        _guess_entity_type(raw),
                        mailing,
                        r["first_seen"],
                        r["last_seen"],
                        r["sale_count"],
                    ),
                )
                created += 1
            else:
                conn.execute(
                    """
                    UPDATE buyer_entities
                       SET last_seen   = MAX(last_seen, ?),
                           first_seen  = MIN(first_seen, ?),
                           total_sales = total_sales + ?
                     WHERE entity_id = ?
                    """,
                    (r["last_seen"], r["first_seen"], r["sale_count"], matched_id),
                )

            conn.execute(
                """
                INSERT OR IGNORE INTO buyer_entity_aliases
                  (entity_id, alias_name_norm, alias_name_raw, source)
                VALUES (?, ?, ?, ?)
                """,
                (matched_id, norm, raw, source),
            )
            aliases_added += 1

            cur = conn.execute(
                "UPDATE cash_sales SET entity_id = ? WHERE buyer_name_norm = ? AND COALESCE(buyer_mailing_addr,'') = COALESCE(?,'')",
                (matched_id, norm, mailing),
            )
            sales_linked += cur.rowcount

        conn.commit()

    return {
        "entities_created": created,
        "aliases_added":    aliases_added,
        "sales_linked":     sales_linked,
        "threshold":        threshold,
    }


def _guess_entity_type(raw_name: str) -> str:
    s = raw_name.upper()
    if any(tag in s for tag in (" LLC", "L.L.C", " L L C")):
        return "llc"
    if "TRUST" in s:
        return "trust"
    if any(tag in s for tag in (" INC", "INCORPORATED", "CORP", "CORPORATION")):
        return "corp"
    if " LTD" in s or " LP" in s or " LLP" in s:
        return "llc"
    # Heuristic: 2 tokens, all alpha, no entity tags → likely individual.
    parts = [p for p in s.replace(",", " ").split() if p.isalpha()]
    if 2 <= len(parts) <= 4:
        return "individual"
    return "unknown"
