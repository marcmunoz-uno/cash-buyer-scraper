"""cash-buyer-intel CLI.

Same agent surface shape as the PP CLIs: every command supports --agent for
structured JSON output with the envelope {ok, data, errors, meta}.

The sync-* / enrich-* / push-* commands subprocess out to the PP binaries.
This repo never speaks HTTP to ATTOM, BatchData, or tranchi directly — the
PP CLIs own that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click


def _pp_bin(name: str) -> str:
    """Resolve a PP CLI binary, checking $PATH then ~/go/bin (the standard
    location for go-installed binaries that's often missing from $PATH)."""
    return (
        shutil.which(name)
        or str(Path.home() / "go" / "bin" / name)
    )

from . import __version__
from .db import (
    BUYERS_DB,
    DEFAULT_SOURCES,
    SOURCE_SCHEMA,
    init_db,
    normalize_address,
    normalize_buyer_name,
    open_buyers,
    probe_source,
    resolve_source_path,
)
from .dedup import resolve_entities
from .scoring import score_all


def _emit(agent: bool, ok: bool, data: Any, errors: list | None = None, meta: dict | None = None) -> None:
    if agent:
        click.echo(json.dumps({"ok": ok, "data": data, "errors": errors or [], "meta": meta or {}}, default=str))
    else:
        if not ok:
            for e in errors or []:
                click.echo(f"error: {e}", err=True)
            sys.exit(1)
        if isinstance(data, (dict, list)):
            click.echo(json.dumps(data, indent=2, default=str))
        elif data is not None:
            click.echo(str(data))


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Cash-buyer discovery mesh — feeds qualified cash buyers to tranchi.ai."""


@main.command("init-db")
@click.option("--agent", is_flag=True)
def cmd_init_db(agent: bool) -> None:
    """Create ~/cash-buyer-intel/buyers.db with the full schema."""
    init_db()
    _emit(agent, True, {"path": str(BUYERS_DB)}, meta={"action": "init_db"})


@main.command("probe")
@click.option("--agent", is_flag=True)
def cmd_probe(agent: bool) -> None:
    """Report which PP source DBs are attached and which tables/columns are present."""
    with open_buyers(read_only=True) as conn:
        results = {name: probe_source(conn, name) for name in SOURCE_SCHEMA}
    _emit(agent, True, results, meta={"buyers_db": str(BUYERS_DB)})


@main.command("sync-attom")
@click.option("--agent", is_flag=True)
def cmd_sync_attom(agent: bool) -> None:
    """Tier A — ATTOM path. Blocked in v0.1: trial key returns 401 on /sale/snapshot.

    Wire-up details preserved for v0.2:
      attom-pp-cli sale snapshot --postalcode <zip>
        --startsalesearchdate YYYY-MM-DD --endsalesearchdate YYYY-MM-DD --agent
      → returns per-sale records; project the ones with mortgage_amount = 0
        into cash_sales (source='attom'). Bumps v0.1's buyer-coverage when
        a working ATTOM key is available.
    """
    _emit(
        agent, True,
        {"status": "skipped", "reason": "ATTOM trial key returns 401; awaits paid key in v0.2"},
        meta={"tier": "A", "source": "attom", "endpoint": "/sale/snapshot"},
    )


# BatchData API body shape that this CLI actually accepts (validated live):
#   {"searchCriteria": {"query": "<zip-or-text>",
#                       "quickLists": ["cash-buyer", ...]},
#    "options":        {"take": N, "skip": M}}
# The MCP-server flat params (owner_name, property_zip, ...) are translated by
# the MCP server, not the CLI; the CLI requires the API's native body shape.

@main.command("sync-batchdata")
@click.option("--query", required=True, help='e.g. "63116" or "Saint Louis MO" or "Cuyahoga County OH"')
@click.option("--market", default=None, help="freeform market label written into cash_sales.market (defaults to --query)")
@click.option("--quicklists", "quicklists_csv", default="cash-buyer",
              help='comma-separated quickLists (kebab-case); default "cash-buyer". '
                   'Use "cash-buyer,corporate-owned" to limit to LLCs.')
@click.option("--limit", type=int, default=200, help="total records to fetch (paged in batches of 100)")
@click.option("--page-size", type=int, default=100)
@click.option("--agent", is_flag=True)
def cmd_sync_batchdata(query: str, market: str | None, quicklists_csv: str,
                       limit: int, page_size: int, agent: bool) -> None:
    """Tier A. Pull cash buyers from BatchData → batchdata_cache + cash_sales."""
    market = market or query
    quicklists = [q.strip() for q in quicklists_csv.split(",") if q.strip()]
    page_size = min(page_size, 100)  # BatchData hard cap

    fetched: list[dict] = []
    skip = 0
    while len(fetched) < limit:
        take = min(page_size, limit - len(fetched))
        body = {
            "searchCriteria": {"query": query, "quickLists": quicklists},
            "options":        {"take": take, "skip": skip},
        }
        try:
            proc = subprocess.run(
                [_pp_bin("batchdata-pp-cli"), "property", "search",
                 "--search-criteria", json.dumps(body["searchCriteria"]),
                 "--options",         json.dumps(body["options"]),
                 "--agent"],
                check=True, capture_output=True, text=True, timeout=180,
            )
        except FileNotFoundError:
            _emit(agent, False, None,
                  errors=["batchdata-pp-cli not found on PATH (check ~/go/bin)"])
            return
        except subprocess.CalledProcessError as e:
            _emit(agent, False, None,
                  errors=[f"batchdata-pp-cli search failed (skip={skip}): {e.stderr[:300]}"])
            return

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            _emit(agent, False, None,
                  errors=[f"could not parse batchdata response: {e}; head={proc.stdout[:200]}"])
            return

        results = (payload.get("data") or {}).get("results") or {}
        props = results.get("properties") or []
        if not props:
            break
        fetched.extend(props)
        meta = (results.get("meta") or {}).get("results") or {}
        # Stop early if we've exhausted the result set.
        if skip + len(props) >= int(meta.get("resultsFound", 0)):
            break
        skip += len(props)

    cached, projected = _store_batchdata_results(fetched, market=market)
    _emit(agent, True,
          {"cached": cached, "projected_to_cash_sales": projected, "fetched": len(fetched)},
          meta={"tier": "A", "source": "batchdata", "query": query,
                "quicklists": quicklists, "limit": limit})


def _store_batchdata_results(rows: list[dict], market: str) -> tuple[int, int]:
    """Insert BatchData property records into batchdata_cache + cash_sales.

    Mapping (BatchData core dataset → cash_sales):
      address.street/city/state/zip → property_address / city / state / zip_code
      owner.fullName                → buyer_name_raw
      owner.mailingAddress.{...}    → buyer_mailing_addr (joined string)
      quickLists.cashBuyer          → must be True to insert (defensive)
      openLien.totalOpenLienCount==0 → free-and-clear validation
      sale.lastSaleDate/Price       → sale_date / sale_price (often empty in core)
      mortgage history              → mortgage_amount = 0 if list empty
    """
    cached = 0
    projected = 0
    with open_buyers() as conn:
        for p in rows:
            addr = p.get("address") or {}
            street = addr.get("street")
            if not street:
                continue
            full_addr = ", ".join(filter(None, [
                street,
                addr.get("city"),
                addr.get("state"),
                addr.get("zip"),
            ]))
            anorm = normalize_address(full_addr)

            ql = p.get("quickLists") or {}
            owner = p.get("owner") or {}
            owner_name = owner.get("fullName")
            owner_mail = owner.get("mailingAddress") or {}
            owner_mail_str = ", ".join(filter(None, [
                owner_mail.get("street"),
                owner_mail.get("city"),
                owner_mail.get("state"),
                owner_mail.get("zip"),
            ])) or None
            phones = p.get("phoneNumbers") or []
            primary_phone = phones[0].get("number") if phones and isinstance(phones[0], dict) else None

            sale = p.get("sale")
            if isinstance(sale, list):
                sale = sale[0] if sale else {}
            elif not isinstance(sale, dict):
                sale = {}

            is_cash = bool(ql.get("cashBuyer"))
            open_lien_count = ((p.get("openLien") or {}).get("totalOpenLienCount") or 0)

            conn.execute(
                """
                INSERT OR REPLACE INTO batchdata_cache
                  (address_norm, raw_response, primary_phone, is_cash_buyer,
                   owner_name, owner_state)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (anorm, json.dumps(p), primary_phone,
                 1 if is_cash else 0,
                 owner_name, owner_mail.get("state")),
            )
            cached += 1

            # Only project as a cash_sale if the buyer name is present AND the
            # cash-buyer signal is true. (We trust BatchData's flag over the
            # mortgage check — they sometimes co-exist if an old lien remained.)
            if not (is_cash and owner_name):
                continue

            sale_date = sale.get("lastSaleDate") or ""
            sale_price = sale.get("lastSalePrice")
            sale_id = f"batchdata:{anorm}:{sale_date or 'unknown'}"

            conn.execute(
                """
                INSERT OR REPLACE INTO cash_sales
                  (sale_id, property_address, property_address_norm, city, state, zip_code,
                   market, property_type, sale_date, sale_price, mortgage_amount,
                   buyer_name_raw, buyer_name_norm, buyer_mailing_addr, seller_name,
                   source, source_record_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'batchdata', ?)
                """,
                (
                    sale_id, full_addr, anorm,
                    addr.get("city"), addr.get("state"), addr.get("zip"),
                    market,
                    (p.get("building") or {}).get("propertyType")
                        or (p.get("listing") or {}).get("propertyTypeDetail"),
                    sale_date[:10] if sale_date else "unknown",
                    sale_price,
                    0 if open_lien_count == 0 else None,
                    owner_name, normalize_buyer_name(owner_name),
                    owner_mail_str,
                    p.get("_id"),
                ),
            )
            projected += 1
        conn.commit()
    return cached, projected


@main.command("sync-county")
@click.option("--market", required=True)
@click.option("--portal", required=True, help="county portal slug from county-portal-scraper")
@click.option("--csv-path", type=click.Path(exists=True), default=None,
              help="explicit CSV from county-portal-scraper; default reads its standard output dir")
@click.option("--agent", is_flag=True)
def cmd_sync_county(market: str, portal: str, csv_path: str | None, agent: bool) -> None:
    """Tier B. Read a county-portal-scraper output and ingest deeds with no concurrent mortgage."""
    # Implementation lands in v0.2; for v0.1 the contract is documented so
    # callers can stub it. county-portal-scraper writes one CSV per (market, portal)
    # under ~/county-portal-scraper/output/.
    _emit(
        agent, True,
        {"market": market, "portal": portal, "inserted_or_updated": 0,
         "note": "tier-B sync lands in v0.2 — see ROADMAP in README"},
        meta={"tier": "B", "source": "county_portal", "csv_path": csv_path},
    )


@main.command("enrich-sales")
@click.option("--min-velocity", type=int, default=2,
              help="only enrich entities with this velocity_12m or higher (saves API credits)")
@click.option("--batch-size", type=int, default=50, help="BatchData accepts up to 100 per lookup call")
@click.option("--limit", type=int, default=200, help="max addresses to enrich this run")
@click.option("--agent", is_flag=True)
def cmd_enrich_sales(min_velocity: int, batch_size: int, limit: int, agent: bool) -> None:
    """Pull sale_date + sale_price for qualifying cash_sales via batchdata property lookup.

    BatchData's search endpoint omits sale dates from the `core` dataset; the
    per-property `lookup` endpoint includes `listing.soldDate`, `listing.bedroomCount`,
    bath / year built, and `sale.priorSale.mortgages` (which confirms a paid-off
    mortgage at the cash-sale point). Costs 1 credit per address.

    Defaults to enriching only multi-property entities (velocity_12m >= 2) so we
    aren't paying credits to confirm dates on one-off cash buyers.
    """
    batch_size = min(batch_size, 100)

    with open_buyers(read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT cs.sale_id, cs.property_address,
                   cs.zip_code, cs.city, cs.state,
                   SUBSTR(cs.property_address, 1, INSTR(cs.property_address, ',') - 1) AS street
              FROM cash_sales cs
              JOIN buyer_scores bs ON bs.entity_id = cs.entity_id
             WHERE bs.velocity_12m >= ?
               AND (cs.sale_date IS NULL OR cs.sale_date = 'unknown')
             LIMIT ?
            """,
            (min_velocity, limit),
        ).fetchall()
        targets = [dict(r) for r in rows]

    if not targets:
        _emit(agent, True, {"enriched": 0, "skipped": "no candidates"},
              meta={"min_velocity": min_velocity})
        return

    enriched = 0
    api_calls = 0
    errors: list[str] = []

    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        body = {"requests": [
            {"address": {
                "street": t["street"],
                "city":   t["city"],
                "state":  t["state"],
                "zip":    t["zip_code"],
            }}
            for t in batch if t["street"]
        ]}
        if not body["requests"]:
            continue

        try:
            proc = subprocess.run(
                [_pp_bin("batchdata-pp-cli"), "property", "lookup", "--stdin", "--agent"],
                input=json.dumps(body), check=True, capture_output=True, text=True, timeout=180,
            )
        except FileNotFoundError:
            _emit(agent, False, None, errors=["batchdata-pp-cli not found"]); return
        except subprocess.CalledProcessError as e:
            errors.append(f"lookup batch failed: {e.stderr[:200]}")
            continue

        api_calls += 1
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            errors.append(f"parse error: {e}")
            continue

        props = ((payload.get("data") or {}).get("results") or {}).get("properties") or []
        enriched += _apply_lookup_results(props, batch)

    _emit(agent, True,
          {"enriched": enriched, "candidates": len(targets), "api_calls": api_calls,
           "estimated_credits_used": enriched},
          errors=errors, meta={"min_velocity": min_velocity, "batch_size": batch_size})


def _apply_lookup_results(props: list[dict], batch: list[dict]) -> int:
    """Match property-lookup responses back to the requesting cash_sales rows and
    update sale_date / sale_price / property_type."""
    # Index responses by normalized property address for matching.
    by_norm: dict[str, dict] = {}
    for p in props:
        addr = p.get("address") or {}
        full = ", ".join(filter(None, [
            addr.get("street"), addr.get("city"), addr.get("state"), addr.get("zip"),
        ]))
        by_norm[normalize_address(full)] = p

    updated = 0
    with open_buyers() as conn:
        for t in batch:
            anorm = normalize_address(t["property_address"])
            p = by_norm.get(anorm)
            if not p:
                continue
            listing = p.get("listing") or {}
            sold = listing.get("soldDate")
            sale = p.get("sale")
            if isinstance(sale, list):
                sale = sale[0] if sale else {}
            elif not isinstance(sale, dict):
                sale = {}
            prior_mortgages = ((sale.get("priorSale") or {}).get("mortgages") or [])
            # If no listing.soldDate, fall back to prior-sale recordingDate (when present).
            sale_date = (sold or
                         (prior_mortgages[0].get("recordingDate") if prior_mortgages else None))
            if not sale_date:
                continue
            sale_price = listing.get("soldPrice") or listing.get("listPrice")
            property_type = listing.get("propertyTypeDimension") or listing.get("homeType")

            conn.execute(
                """
                UPDATE cash_sales
                   SET sale_date     = ?,
                       sale_price    = COALESCE(?, sale_price),
                       property_type = COALESCE(?, property_type)
                 WHERE sale_id = ?
                """,
                (sale_date[:10], sale_price, property_type, t["sale_id"]),
            )
            updated += 1
        conn.commit()
    return updated


@main.command("enrich-batchdata")
@click.option("--limit", type=int, default=200)
@click.option("--agent", is_flag=True)
def cmd_enrich_batchdata(limit: int, agent: bool) -> None:
    """Skip-trace any cash_sales buyer that has no buyer_contacts row yet."""
    enriched = 0
    with open_buyers() as conn:
        targets = conn.execute(
            """
            SELECT DISTINCT cs.entity_id, cs.buyer_mailing_addr
              FROM cash_sales cs
         LEFT JOIN buyer_contacts bc ON bc.entity_id = cs.entity_id
             WHERE cs.entity_id IS NOT NULL
               AND bc.entity_id IS NULL
               AND cs.buyer_mailing_addr IS NOT NULL
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

    for t in targets:
        addr = t["buyer_mailing_addr"]
        try:
            proc = subprocess.run(
                [_pp_bin("batchdata-pp-cli"), "property", "skip-trace", "--address", addr, "--agent"],
                check=True, capture_output=True, text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue

        data = (payload or {}).get("data") or {}
        with open_buyers() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO buyer_contacts
                  (entity_id, primary_phone, primary_email, llc_authorized_agent,
                   skip_traced_at, confidence)
                VALUES (?, ?, ?, ?, datetime('now'), ?)
                """,
                (
                    t["entity_id"],
                    data.get("primary_phone"),
                    data.get("primary_email"),
                    data.get("llc_authorized_agent"),
                    data.get("confidence"),
                ),
            )
            conn.commit()
        enriched += 1

    _emit(agent, True, {"enriched": enriched}, meta={"limit": limit})


@main.command("dedup")
@click.option("--threshold", type=float, default=0.85)
@click.option("--agent", is_flag=True)
def cmd_dedup(threshold: float, agent: bool) -> None:
    """Run entity resolution across all cash_sales rows."""
    summary = resolve_entities(threshold=threshold)
    _emit(agent, True, summary, meta={"action": "dedup"})


@main.command("score")
@click.option("--window", "window", default="12m", help="velocity window — only '12m' supported in v0.1")
@click.option("--agent", is_flag=True)
def cmd_score(window: str, agent: bool) -> None:
    """Recompute buyer_scores for every entity."""
    months = 12 if window == "12m" else int(window.rstrip("m"))
    summary = score_all(window_months=months)
    _emit(agent, True, summary, meta={"action": "score"})


@main.command("buyers")
@click.option("--market", default=None)
@click.option("--state", default=None)
@click.option("--min-velocity", type=int, default=3, help="minimum velocity_12m")
@click.option("--max-median-price", type=int, default=None)
@click.option("--property-type", default=None)
@click.option("--has-phone/--no-has-phone", default=False)
@click.option("--no-recent-outreach", default=None,
              help='exclude buyers contacted within this window, e.g. "30d"')
@click.option("--limit", type=int, default=50)
@click.option("--agent", is_flag=True)
def cmd_buyers(market, state, min_velocity, max_median_price, property_type,
               has_phone, no_recent_outreach, limit, agent) -> None:
    """The main query — qualified buyers ready for wholesaler outreach."""
    where = ["bs.velocity_12m >= ?"]
    params: list = [min_velocity]

    if max_median_price is not None:
        where.append("(bs.median_purchase_price IS NULL OR bs.median_purchase_price <= ?)")
        params.append(max_median_price)
    if property_type:
        where.append("bs.property_type_mode = ?")
        params.append(property_type)
    if has_phone:
        where.append("bc.primary_phone IS NOT NULL")

    market_filter = ""
    if market:
        market_filter = """
          AND EXISTS (
            SELECT 1 FROM cash_sales cs
             WHERE cs.entity_id = be.entity_id AND cs.market = ?
          )
        """
        params.append(market)
    state_filter = ""
    if state:
        state_filter = """
          AND EXISTS (
            SELECT 1 FROM cash_sales cs
             WHERE cs.entity_id = be.entity_id AND cs.state = ?
          )
        """
        params.append(state)
    recent_filter = ""
    if no_recent_outreach:
        days = int(no_recent_outreach.rstrip("d"))
        recent_filter = f"""
          AND NOT EXISTS (
            SELECT 1 FROM buyer_outreach bo
             WHERE bo.entity_id = be.entity_id
               AND bo.occurred_at > datetime('now', '-{days} day')
          )
        """

    sql = f"""
    SELECT
      be.entity_id, be.canonical_name, be.entity_type, be.primary_mailing,
      be.first_seen, be.last_seen, be.total_sales,
      bs.velocity_12m, bs.velocity_3m, bs.median_purchase_price,
      bs.p25_price, bs.p75_price, bs.property_type_mode,
      bs.recency_score, bs.activity_tier,
      bc.primary_phone, bc.primary_email
    FROM buyer_entities be
    JOIN buyer_scores  bs ON bs.entity_id = be.entity_id
    LEFT JOIN buyer_contacts bc ON bc.entity_id = be.entity_id
    WHERE {' AND '.join(where)}
    {market_filter}
    {state_filter}
    {recent_filter}
    ORDER BY bs.activity_tier = 'hot' DESC, bs.recency_score DESC
    LIMIT ?
    """
    params.append(limit)

    with open_buyers(read_only=True) as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    _emit(agent, True, rows, meta={
        "count": len(rows), "filters": {
            "market": market, "state": state, "min_velocity": min_velocity,
            "max_median_price": max_median_price, "property_type": property_type,
            "has_phone": has_phone, "no_recent_outreach": no_recent_outreach,
        },
    })


@main.command("push-tranchi")
@click.option("--top", type=int, default=20)
@click.option("--market", default=None)
@click.option("--dry-run", is_flag=True)
@click.option("--agent", is_flag=True)
def cmd_push_tranchi(top: int, market: str | None, dry_run: bool, agent: bool) -> None:
    """POST qualified buyers to tranchi.ai via tranchi-pp-cli.

    See README "Pushing to tranchi.ai" — the tranchi-pp-cli `cash_buyers`
    resource is not yet generated. Until then this command stubs the call
    and writes to buyer_outreach with channel='tranchi_push' for accounting.
    """
    where = ["bs.activity_tier IN ('hot', 'warm')"]
    params: list = []
    if market:
        where.append("""EXISTS (SELECT 1 FROM cash_sales cs WHERE cs.entity_id = be.entity_id AND cs.market = ?)""")
        params.append(market)
    params.append(top)

    sql = f"""
    SELECT be.entity_id, be.canonical_name, be.entity_type, be.primary_mailing,
           bs.velocity_12m, bs.median_purchase_price, bs.property_type_mode,
           bc.primary_phone, bc.primary_email
      FROM buyer_entities be
      JOIN buyer_scores bs ON bs.entity_id = be.entity_id
 LEFT JOIN buyer_contacts bc ON bc.entity_id = be.entity_id
     WHERE {' AND '.join(where)}
     ORDER BY bs.recency_score DESC
     LIMIT ?
    """

    with open_buyers() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        if dry_run:
            _emit(agent, True, rows, meta={"dry_run": True, "would_push": len(rows)})
            return

        pushed = 0
        errors: list[str] = []
        for r in rows:
            try:
                subprocess.run(
                    [_pp_bin("tranchi-pp-cli"), "cash-buyers", "upload",
                     "--external-id", r["entity_id"],
                     "--name", r["canonical_name"],
                     "--phone", r.get("primary_phone") or "",
                     "--email", r.get("primary_email") or "",
                     "--velocity-12m", str(r["velocity_12m"]),
                     "--median-price", str(r.get("median_purchase_price") or 0),
                     "--property-type", r.get("property_type_mode") or "",
                     "--agent"],
                    check=True, capture_output=True, text=True,
                )
            except FileNotFoundError:
                errors.append("tranchi-pp-cli not found on PATH — see README 'Pushing to tranchi.ai'")
                break
            except subprocess.CalledProcessError as e:
                errors.append(f"push failed for {r['entity_id']}: {e.stderr[:200]}")
                continue

            conn.execute(
                """
                INSERT INTO buyer_outreach
                  (entity_id, channel, direction, summary, response_status)
                VALUES (?, 'tranchi_push', 'out', 'pushed via tranchi-pp-cli', 'pending')
                """,
                (r["entity_id"],),
            )
            pushed += 1
        conn.commit()

    _emit(agent, not errors or pushed > 0,
          {"pushed": pushed, "candidates": len(rows)},
          errors=errors, meta={"top": top, "market": market})


@main.command("outreach-log")
@click.option("--buyer-id", required=True)
@click.option("--channel", type=click.Choice(["imessage", "email", "call", "direct_mail", "tranchi_push"]),
              required=True)
@click.option("--direction", type=click.Choice(["out", "in"]), default="out")
@click.option("--wholesaler", default=None, help="tranchi.ai user_id of the wholesaler")
@click.option("--summary", default=None)
@click.option("--response", default=None)
@click.option("--agent", is_flag=True)
def cmd_outreach_log(buyer_id, channel, direction, wholesaler, summary, response, agent) -> None:
    """Record an outreach event so subsequent --no-recent-outreach filters work."""
    with open_buyers() as conn:
        cur = conn.execute(
            """
            INSERT INTO buyer_outreach
              (entity_id, wholesaler_user_id, channel, direction, summary, response_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (buyer_id, wholesaler, channel, direction, summary, response),
        )
        conn.commit()
        outreach_id = cur.lastrowid
    _emit(agent, True, {"outreach_id": outreach_id}, meta={"buyer_id": buyer_id})


if __name__ == "__main__":
    main()
