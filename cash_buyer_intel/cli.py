"""cash-buyer-intel CLI.

Same agent surface shape as the PP CLIs: every command supports --agent for
structured JSON output with the envelope {ok, data, errors, meta}.

The sync-* / enrich-* / push-* commands subprocess out to the PP binaries.
This repo never speaks HTTP to ATTOM, BatchData, or tranchi directly — the
PP CLIs own that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import click

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
@click.option("--market", required=True, help='e.g. "St. Louis MO"')
@click.option("--since", default="12m", help="duration window passed to attom-pp-cli sync")
@click.option("--state", default=None)
@click.option("--dry-run", is_flag=True)
@click.option("--agent", is_flag=True)
def cmd_sync_attom(market: str, since: str, state: str | None, dry_run: bool, agent: bool) -> None:
    """Tier A. Pull cash sales from attom-pp-cli saleshistory into cash_sales."""
    # The actual ATTOM sync is driven by `attom-pp-cli sync --since <since>` —
    # invoked here so the call site is one place. After sync, we project
    # cash-sale rows out of attom.attom_saleshistory.
    if not dry_run:
        try:
            subprocess.run(
                ["attom-pp-cli", "sync", "--since", since],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            _emit(agent, False, None, errors=["attom-pp-cli not found on PATH — install the PP CLI first"])
            return
        except subprocess.CalledProcessError as e:
            _emit(agent, False, None, errors=[f"attom-pp-cli sync failed: {e.stderr.decode(errors='ignore')}"])
            return

    inserted = _project_attom_to_cash_sales(market=market, state=state)
    _emit(agent, True, {"market": market, "inserted_or_updated": inserted},
          meta={"tier": "A", "source": "attom", "since": since, "dry_run": dry_run})


def _project_attom_to_cash_sales(market: str, state: str | None) -> int:
    """SELECT cash sales from attached attom DB → UPSERT into main.cash_sales."""
    n = 0
    with open_buyers() as conn:
        # Defensive: only run if attom is actually attached & has the expected table.
        attached = {r["name"] for r in conn.execute("SELECT name FROM pragma_database_list").fetchall()}
        if "attom" not in attached:
            return 0

        sql = """
        INSERT OR REPLACE INTO cash_sales
          (sale_id, property_address, property_address_norm, city, state, zip_code,
           market, property_type, sale_date, sale_price, mortgage_amount,
           buyer_name_raw, buyer_name_norm, buyer_mailing_addr, seller_name,
           source, source_record_id, entity_id)
        SELECT
          'attom:' || COALESCE(s.transaction_id, hex(randomblob(8)))    AS sale_id,
          p.address                                                      AS property_address,
          norm_addr(p.address)                                           AS property_address_norm,
          p.city, p.state, p.zip_code,
          ?                                                              AS market,
          p.property_type,
          s.sale_date,
          s.amount,
          s.mortgage_amount,
          COALESCE(s.buyer_name, '(unknown)')                            AS buyer_name_raw,
          norm_buyer(COALESCE(s.buyer_name, ''))                         AS buyer_name_norm,
          p.owner_address                                                AS buyer_mailing_addr,
          s.seller_name,
          'attom'                                                        AS source,
          s.transaction_id                                               AS source_record_id,
          NULL                                                           AS entity_id
        FROM attom.attom_saleshistory s
        JOIN attom.attom_property p ON p.attom_id = s.attom_id
        WHERE (s.mortgage_amount IS NULL OR s.mortgage_amount = 0)
          AND s.buyer_name IS NOT NULL
        """
        params: list = [market]
        if state:
            sql += " AND p.state = ?"
            params.append(state)
        cur = conn.execute(sql, params)
        n = cur.rowcount
        conn.commit()
    return n


@main.command("sync-batchdata")
@click.option("--market", required=True)
@click.option("--cash-only/--no-cash-only", default=True)
@click.option("--limit", type=int, default=500)
@click.option("--agent", is_flag=True)
def cmd_sync_batchdata(market: str, cash_only: bool, limit: int, agent: bool) -> None:
    """Tier A. Search BatchData for cash buyers; cache results locally."""
    # BatchData has no `sync`; we call `batchdata-pp-cli property search` with the
    # cash_buyer filter and UPSERT into batchdata_cache. The CLI returns JSON via
    # --agent.
    try:
        proc = subprocess.run(
            [
                "batchdata-pp-cli", "property", "search",
                "--market", market,
                "--cash-buyer", str(cash_only).lower(),
                "--limit", str(limit),
                "--agent",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        _emit(agent, False, None, errors=["batchdata-pp-cli not found on PATH"])
        return
    except subprocess.CalledProcessError as e:
        _emit(agent, False, None, errors=[f"batchdata-pp-cli search failed: {e.stderr}"])
        return

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _emit(agent, False, None, errors=[f"could not parse batchdata response: {e}"])
        return

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    cached, projected = _store_batchdata_results(rows, market=market)
    _emit(agent, True, {"cached": cached, "projected_to_cash_sales": projected},
          meta={"tier": "A", "source": "batchdata", "market": market, "limit": limit})


def _store_batchdata_results(rows: list[dict], market: str) -> tuple[int, int]:
    cached = 0
    projected = 0
    with open_buyers() as conn:
        for r in rows:
            addr = r.get("address") or ""
            if not addr:
                continue
            anorm = normalize_address(addr)
            conn.execute(
                """
                INSERT OR REPLACE INTO batchdata_cache
                  (address_norm, raw_response, primary_phone, is_cash_buyer,
                   owner_name, owner_state)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    anorm,
                    json.dumps(r),
                    r.get("primary_phone"),
                    1 if r.get("is_cash_buyer") else 0,
                    r.get("owner_name"),
                    r.get("owner_state"),
                ),
            )
            cached += 1

            if r.get("last_cash_sale_date") and r.get("owner_name"):
                sale_id = "batchdata:" + anorm + ":" + r["last_cash_sale_date"]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO cash_sales
                      (sale_id, property_address, property_address_norm, city, state, zip_code,
                       market, property_type, sale_date, sale_price, mortgage_amount,
                       buyer_name_raw, buyer_name_norm, buyer_mailing_addr, seller_name,
                       source, source_record_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, 'batchdata', ?)
                    """,
                    (
                        sale_id, addr, anorm,
                        r.get("city"), r.get("state"), r.get("zip_code"),
                        market, r.get("property_type"),
                        r.get("last_cash_sale_date"), r.get("last_cash_sale_price"),
                        r["owner_name"], normalize_buyer_name(r["owner_name"]),
                        r.get("owner_mailing_address"), r.get("batch_id"),
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
                ["batchdata-pp-cli", "property", "skip-trace", "--address", addr, "--agent"],
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
                    ["tranchi-pp-cli", "cash-buyers", "upload",
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
