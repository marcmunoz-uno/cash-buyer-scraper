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


@main.command("ingest-propstream")
@click.argument("xlsx_path", type=click.Path(exists=True))
@click.option("--market", default=None, help="market label (default: inferred from filename)")
@click.option("--agent", is_flag=True)
def cmd_ingest_propstream(xlsx_path: str, market: str | None, agent: bool) -> None:
    """Ingest a PropStream cash-buyer export (XLSX) into cash_sales.

    PropStream's saved-list Export produces `Property Export <name>.xlsx` with
    75 columns. We use: Address/City/State/Zip/County/APN, Owner 1/2 names,
    Mailing Address (joined), Last Sale Recording Date, Last Sale Amount,
    Property Type, Total Open Loans (must be 0 for a true cash buyer).

    100% sale-date coverage, ~79% sale-amount coverage on the validated 63116
    sample. Costs $0 above the user's existing PropStream subscription.
    """
    try:
        import pandas as pd
    except ImportError:
        _emit(agent, False, None,
              errors=["pandas not installed — `pip install pandas openpyxl` in the venv"])
        return

    df = pd.read_excel(xlsx_path)
    market = market or Path(xlsx_path).stem.replace("Property Export ", "").strip() or "propstream"

    def col(row, name, default=None):
        v = row.get(name, default)
        return None if (v is None or (isinstance(v, float) and v != v)) else v

    inserted = 0
    skipped_with_loans = 0
    with open_buyers() as conn:
        for _, r in df.iterrows():
            street = col(r, "Address")
            if not street:
                continue
            # Defensive cash-buyer check: PropStream's Cash Buyers filter
            # already applied during export, but verify Total Open Loans = 0
            # so we never ingest a financed property as a cash sale.
            open_loans = col(r, "Total Open Loans")
            if open_loans is not None and float(open_loans) > 0:
                skipped_with_loans += 1
                continue

            full_addr = ", ".join(filter(None, [
                str(street),
                str(col(r, "City") or ""),
                str(col(r, "State") or ""),
                str(col(r, "Zip") or ""),
            ]))
            anorm = normalize_address(full_addr)

            owner_parts = [col(r, "Owner 1 First Name"), col(r, "Owner 1 Last Name")]
            owner_name = " ".join(p for p in owner_parts if p) or col(r, "Owner 1 Last Name") or "(unknown)"
            # Joint-ownership case
            o2 = " ".join(p for p in (col(r, "Owner 2 First Name"), col(r, "Owner 2 Last Name")) if p)
            if o2:
                owner_name = f"{owner_name} & {o2}"

            mailing = ", ".join(filter(None, [
                str(col(r, "Mailing Address") or ""),
                str(col(r, "Mailing City") or ""),
                str(col(r, "Mailing State") or ""),
                str(col(r, "Mailing Zip") or ""),
            ])) or None

            sale_date = col(r, "Last Sale Recording Date")
            if sale_date is not None:
                sale_date = str(sale_date)[:10]
            sale_price = col(r, "Last Sale Amount")
            if sale_price is not None:
                sale_price = int(float(sale_price))

            sale_id = f"propstream:{anorm}:{sale_date or 'unknown'}"

            conn.execute(
                """
                INSERT OR REPLACE INTO cash_sales
                  (sale_id, property_address, property_address_norm, city, state, zip_code,
                   market, property_type, sale_date, sale_price, mortgage_amount,
                   buyer_name_raw, buyer_name_norm, buyer_mailing_addr, seller_name,
                   source, source_record_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, 'propstream', ?)
                """,
                (sale_id, full_addr, anorm,
                 col(r, "City"), col(r, "State"), str(col(r, "Zip") or ""),
                 market, col(r, "Property Type"),
                 sale_date or "unknown", sale_price,
                 owner_name, normalize_buyer_name(owner_name),
                 mailing, col(r, "APN")),
            )
            inserted += 1
        conn.commit()

    _emit(agent, True,
          {"inserted": inserted, "skipped_with_loans": skipped_with_loans,
           "rows_in_xlsx": len(df), "market": market},
          meta={"source": "propstream", "xlsx": xlsx_path})


@main.command("ingest-propstream-list")
@click.argument("xlsx_path", type=click.Path(exists=True))
@click.option("--lead-type", required=True,
              type=click.Choice(["pre-foreclosure", "vacant", "absentee", "high-equity",
                                 "tired-landlord", "probate", "tax-default",
                                 "vacant-equity-absentee",
                                 "failed-listing", "canceled-listing", "expired-listing",
                                 "recently-sold", "active-listing", "pending-listing",
                                 "other"]),
              help="which PropStream lead-list this export came from")
@click.option("--market", default=None, help="market label (defaults to filename stem)")
@click.option("--agent", is_flag=True)
def cmd_ingest_propstream_list(xlsx_path: str, lead_type: str, market: str | None, agent: bool) -> None:
    """Ingest a PropStream motivated-seller XLSX export into motivated_sellers.

    Same 75-column shape as the cash-buyers export; semantic difference is
    the filter that produced it. Each export becomes one or more rows tagged
    with the lead_type. v0.2 scaffold — wires the SOP outputs into a queryable
    table without bloating cash_sales (which stays buyer-side only).
    """
    try:
        import pandas as pd
    except ImportError:
        _emit(agent, False, None,
              errors=["pandas not installed — `pip install pandas openpyxl` in the venv"])
        return

    df = pd.read_excel(xlsx_path)
    market = market or Path(xlsx_path).stem.replace("Property Export ", "").strip() or "propstream"

    def col(row, name, default=None):
        v = row.get(name, default)
        return None if (v is None or (isinstance(v, float) and v != v)) else v

    inserted = 0
    import hashlib
    from datetime import datetime
    export_date = datetime.utcnow().strftime("%Y-%m-%d")

    with open_buyers() as conn:
        for _, r in df.iterrows():
            street = col(r, "Address")
            if not street:
                continue
            full_addr = ", ".join(filter(None, [
                str(street), str(col(r, "City") or ""), str(col(r, "State") or ""), str(col(r, "Zip") or ""),
            ]))
            anorm = normalize_address(full_addr)

            owner_parts = [col(r, "Owner 1 First Name"), col(r, "Owner 1 Last Name")]
            owner_name = " ".join(p for p in owner_parts if p) or col(r, "Owner 1 Last Name") or "(unknown)"

            mailing = ", ".join(filter(None, [
                str(col(r, "Mailing Address") or ""),
                str(col(r, "Mailing City") or ""),
                str(col(r, "Mailing State") or ""),
                str(col(r, "Mailing Zip") or ""),
            ])) or None

            sale_date = col(r, "Last Sale Recording Date")
            if sale_date is not None:
                sale_date = str(sale_date)[:10]
            sale_amount = col(r, "Last Sale Amount")
            sale_amount = int(float(sale_amount)) if sale_amount is not None else None

            lead_id = "ps_" + hashlib.sha1(f"{anorm}|{lead_type}|{export_date}".encode()).hexdigest()[:16]

            def to_int(v):
                if v is None: return None
                try: return int(float(v))
                except (ValueError, TypeError): return None

            def to_float(v):
                if v is None: return None
                try: return float(v)
                except (ValueError, TypeError): return None

            owner_occ_raw = col(r, "Owner Occupied")
            owner_occ = 1 if str(owner_occ_raw).strip().lower() in ("yes", "y", "true", "1") else (
                       0 if str(owner_occ_raw).strip().lower() in ("no", "n", "false", "0") else None)

            conn.execute(
                """
                INSERT OR REPLACE INTO motivated_sellers
                  (lead_id, property_address, property_address_norm, city, state, zip_code,
                   market, property_type, lead_type, distress_signals,
                   foreclosure_factor, total_open_loans, est_remaining_balance, est_value,
                   est_equity, est_ltv, last_sale_date, last_sale_amount,
                   owner_name_raw, owner_name_norm, owner_mailing_addr, owner_occupied,
                   source, source_record_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'propstream', ?)
                """,
                (lead_id, full_addr, anorm,
                 col(r, "City"), col(r, "State"), str(col(r, "Zip") or ""),
                 market, col(r, "Property Type"),
                 lead_type,
                 None,  # distress_signals — could derive from quickList columns if customized
                 to_float(col(r, "Foreclosure Factor")),
                 to_int(col(r, "Total Open Loans")),
                 to_int(col(r, "Est. Remaining balance of Open Loans")),
                 to_int(col(r, "Est. Value")),
                 to_int(col(r, "Est. Equity")),
                 to_float(col(r, "Est. Loan-to-Value")),
                 sale_date, sale_amount,
                 owner_name, normalize_buyer_name(owner_name),
                 mailing, owner_occ,
                 col(r, "APN")),
            )
            inserted += 1
        conn.commit()

    _emit(agent, True,
          {"inserted": inserted, "rows_in_xlsx": len(df), "lead_type": lead_type, "market": market},
          meta={"source": "propstream", "xlsx": xlsx_path})


@main.command("ingest-batchdata-sellers")
@click.argument("json_glob", type=str)
@click.option("--lead-type", required=True,
              type=click.Choice(["pre-foreclosure", "vacant", "absentee", "high-equity",
                                 "tired-landlord", "probate", "tax-default",
                                 "vacant-equity-absentee",
                                 "failed-listing", "canceled-listing", "expired-listing",
                                 "recently-sold", "active-listing", "pending-listing",
                                 "other"]))
@click.option("--market", required=True)
@click.option("--agent", is_flag=True)
def cmd_ingest_batchdata_sellers(json_glob: str, lead_type: str, market: str, agent: bool) -> None:
    """Ingest batchdata-pp-cli property search JSON output(s) into motivated_sellers.

    Accepts a glob of JSON files (one per paginated batchdata response).
    Same parsing pattern as sync-batchdata but writes to motivated_sellers
    instead of cash_sales (since these are seller-side leads).
    """
    import glob as _glob
    import hashlib
    files = sorted(_glob.glob(json_glob))
    if not files:
        _emit(agent, False, None, errors=[f"no files match: {json_glob}"]); return

    inserted = 0
    skipped = 0
    seen: set[str] = set()

    with open_buyers() as conn:
        for f in files:
            payload = json.load(open(f))
            props = ((payload.get("data") or {}).get("results") or {}).get("properties") or []
            for p in props:
                addr = p.get("address") or {}
                street = addr.get("street")
                if not street:
                    skipped += 1; continue
                full_addr = ", ".join(filter(None, [
                    street, addr.get("city"), addr.get("state"), addr.get("zip"),
                ]))
                anorm = normalize_address(full_addr)
                if anorm in seen:
                    skipped += 1; continue
                seen.add(anorm)

                owner = p.get("owner") or {}
                owner_name = owner.get("fullName") or "(unknown)"
                owner_mail = owner.get("mailingAddress") or {}
                mailing = ", ".join(filter(None, [
                    owner_mail.get("street"), owner_mail.get("city"),
                    owner_mail.get("state"), owner_mail.get("zip"),
                ])) or None

                ql = p.get("quickLists") or {}
                distress = ",".join(k for k, v in ql.items() if v is True)

                open_lien = p.get("openLien") or {}
                sale = p.get("sale")
                if isinstance(sale, list):
                    sale = sale[0] if sale else {}
                elif not isinstance(sale, dict):
                    sale = {}

                # BatchData's `listing` block carries price/beds/baths/sqft/etc. even
                # for off-market vacancies (it's the last-known listing snapshot).
                listing = p.get("listing") or {}
                # Best available price: current price → last max-list → mortgage loan amount
                price = (listing.get("price")
                         or listing.get("maxListPrice")
                         or sale.get("lastSalePrice")
                         or ((p.get("mortgageHistory") or [{}])[0].get("loanAmount") if p.get("mortgageHistory") else None))
                last_sold = listing.get("soldDate") or sale.get("lastSaleDate")
                property_type_raw = listing.get("propertyType")  # 'SINGLE_FAMILY' etc.
                # Zillow PDP URL — enables the Zillow stage of the photo waterfall
                listing_url_zillow = listing.get("listingUrl") or None
                if listing_url_zillow and "zillow.com/homedetails" not in listing_url_zillow:
                    listing_url_zillow = None

                lead_id = "bd_" + hashlib.sha1(f"{anorm}|{lead_type}".encode()).hexdigest()[:16]

                conn.execute(
                    """
                    INSERT OR REPLACE INTO motivated_sellers
                      (lead_id, property_address, property_address_norm, city, state, zip_code,
                       market, property_type, lead_type, distress_signals,
                       total_open_loans, est_remaining_balance, est_value,
                       last_sale_date, last_sale_amount,
                       owner_name_raw, owner_name_norm, owner_mailing_addr, owner_occupied,
                       latitude, longitude, listing_url,
                       source, source_record_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'batchdata', ?)
                    """,
                    (lead_id, full_addr, anorm,
                     addr.get("city"), addr.get("state"), addr.get("zip"),
                     market, property_type_raw,
                     lead_type, distress,
                     int(open_lien.get("totalOpenLienCount") or 0) or None,
                     int(open_lien.get("totalOpenLienBalance") or 0) or None,
                     int(price) if price else None,
                     (last_sold or "")[:10] or None,
                     int(sale.get("lastSalePrice")) if sale.get("lastSalePrice") else None,
                     owner_name, normalize_buyer_name(owner_name),
                     mailing, 1 if ql.get("ownerOccupied") else (0 if "ownerOccupied" in ql else None),
                     addr.get("latitude"), addr.get("longitude"), listing_url_zillow,
                     p.get("_id")),
                )
                inserted += 1
        conn.commit()

    _emit(agent, True,
          {"inserted": inserted, "skipped": skipped, "files": len(files), "market": market, "lead_type": lead_type},
          meta={"source": "batchdata"})


@main.command("enrich-photos")
@click.option("--source-table", type=click.Choice(["motivated_sellers", "cash_sales"]),
              default="motivated_sellers")
@click.option("--market", default=None, help="filter to a single market")
@click.option("--limit", type=int, default=250, help="max addresses to enrich this run")
@click.option("--min-photos", type=int, default=5, help="reject results with fewer than this many photo URLs")
@click.option("--target-photos", type=int, default=10, help="aim for this many photos per property")
@click.option("--no-zillow", is_flag=True, help="skip BrightData/Zillow stage (use when BrightData zone is broken)")
@click.option("--no-street-view", is_flag=True)
@click.option("--no-esri", is_flag=True)
@click.option("--workers", type=int, default=4,
              help="concurrent waterfall runs (default 4; 8 is safe with BrightData Web Unlocker, "
                   "drop to 1 for debugging)")
@click.option("--agent", is_flag=True)
def cmd_enrich_photos(source_table: str, market: str | None, limit: int, min_photos: int,
                       target_photos: int, no_zillow: bool, no_street_view: bool,
                       no_esri: bool, workers: int, agent: bool) -> None:
    """Generate property photo URLs via the photo-enrichment-pipeline waterfall.

    Waterfall order:
      1. Zillow listing photos (BrightData Web Unlocker on Zillow PDPs)
      2. Google Street View (4 cardinal headings, free per-API-call)
      3. Esri World Imagery (aerial, free, always returns something)

    Pulls credentials from environment / ~/.openclaw/:
      - BRIGHTDATA_TOKEN (~/.openclaw/.env) → Zillow stage
      - GOOGLE_MAPS_KEY (~/.openclaw/.google_maps_api_key) → Street View stage

    Skipping a stage when its credentials are missing is graceful; the
    waterfall falls through. Esri requires only lat/lon (geocoded via
    free Census API).
    """
    try:
        from photo_enrichment import fetch_photos_waterfall
    except ImportError:
        _emit(agent, False, None,
              errors=["photo-enrichment-pipeline not installed: pip install -e ~/photo-enrichment-pipeline"]); return

    # Load credentials from openclaw locations if not in env
    if not os.environ.get("BRIGHTDATA_TOKEN"):
        env_file = Path.home() / ".openclaw" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("BRIGHTDATA_TOKEN="):
                    os.environ["BRIGHTDATA_TOKEN"] = line.split("=", 1)[1].strip()
                    break
    if not os.environ.get("GOOGLE_MAPS_KEY"):
        gmaps_key_file = Path.home() / ".openclaw" / ".google_maps_api_key"
        if gmaps_key_file.exists():
            os.environ["GOOGLE_MAPS_KEY"] = gmaps_key_file.read_text().strip()

    where = "1=1"
    params: list = []
    if market:
        where += " AND market = ?"
        params.append(market)

    with open_buyers(read_only=True) as conn:
        if source_table == "motivated_sellers":
            sql = f"""
                SELECT m.property_address_norm AS address_norm, m.property_address AS full_addr,
                       m.city, m.state, m.zip_code, m.latitude, m.longitude, m.listing_url
                  FROM motivated_sellers m
             LEFT JOIN property_photos pp ON pp.address_norm = m.property_address_norm
                 WHERE pp.address_norm IS NULL AND {where}
                 LIMIT ?
            """
        else:
            sql = f"""
                SELECT cs.property_address_norm AS address_norm, cs.property_address AS full_addr,
                       cs.city, cs.state, cs.zip_code,
                       NULL AS latitude, NULL AS longitude, NULL AS listing_url
                  FROM cash_sales cs
             LEFT JOIN property_photos pp ON pp.address_norm = cs.property_address_norm
                 WHERE pp.address_norm IS NULL AND {where}
                 LIMIT ?
            """
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        _emit(agent, True, {"enriched": 0, "skipped": "no candidates"}); return

    enriched = 0
    insufficient = 0
    failed = 0
    errors: list[str] = []
    source_stats_total: dict[str, int] = {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    db_lock = threading.Lock()

    def process(row):
        street = row["full_addr"].split(",")[0].strip()
        result = fetch_photos_waterfall(
            address=street,
            city=row.get("city") or "",
            state=row.get("state") or "",
            zip_code=str(row.get("zip_code") or ""),
            lat=row.get("latitude"),
            lon=row.get("longitude"),
            listing_url=row.get("listing_url"),
            target_photos=target_photos,
            enable_zillow=not no_zillow,
            enable_street_view=not no_street_view,
            enable_esri=not no_esri,
        )
        return row, result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(process, r) for r in rows]
        for fut in as_completed(futures):
            row, result = fut.result()
            if not result.get("ok"):
                failed += 1
                for e in (result.get("errors") or []):
                    errors.append(str(e)[:120])
                continue
            photos = result.get("photo_urls") or []
            if len(photos) < min_photos:
                insufficient += 1
                continue
            ss = result.get("source_stats") or {}
            with db_lock:
                for k, v in ss.items():
                    source_stats_total[k] = source_stats_total.get(k, 0) + v
            primary = max(ss, key=ss.get) if ss else "unknown"
            with db_lock:
                with open_buyers() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO property_photos
                          (address_norm, image_urls, photo_count, source, source_url)
                        VALUES (?, ?, ?, ?, NULL)
                        """,
                        (row["address_norm"], json.dumps(photos), len(photos), primary),
                    )
                    conn.commit()
            enriched += 1

    _emit(agent, True,
          {"enriched": enriched, "insufficient_photos": insufficient, "fetch_failed": failed,
           "min_photos": min_photos, "candidates": len(rows),
           "source_breakdown": source_stats_total},
          errors=errors[:10],
          meta={"source_table": source_table, "market": market, "target_photos": target_photos})


@main.command("push-tranchi-leads")
@click.option("--source-table", type=click.Choice(["motivated_sellers", "cash_sales"]),
              default="motivated_sellers")
@click.option("--market", default=None)
@click.option("--limit", type=int, default=250)
@click.option("--min-photos", type=int, default=5)
@click.option("--dry-run", is_flag=True)
@click.option("--agent", is_flag=True)
def cmd_push_tranchi_leads(source_table: str, market: str | None, limit: int,
                            min_photos: int, dry_run: bool, agent: bool) -> None:
    """Build the tranchi-pp-cli leads payload from motivated_sellers (or cash_sales)
    joined with property_photos, then POST via `tranchi-pp-cli leads upload`.

    Skips rows without ≥min-photos photos (Tranchi silently drops short image
    arrays). Writes the response to tranchi_push_log.
    """
    # Photos optional when min_photos == 0 (LEFT JOIN); required (INNER) otherwise.
    photo_join = "LEFT JOIN" if min_photos == 0 else "JOIN"
    where = "1=1" if min_photos == 0 else "COALESCE(p.photo_count, 0) >= ?"
    params: list = [] if min_photos == 0 else [min_photos]
    if market:
        where += " AND m.market = ?"
        params.append(market)

    with open_buyers() as conn:
        if source_table == "motivated_sellers":
            sql = f"""
                SELECT m.lead_id AS row_id, m.property_address AS address, m.city, m.state,
                       m.zip_code AS zip, m.lead_type AS deal_type,
                       m.owner_name_raw, m.owner_mailing_addr,
                       m.est_value, m.last_sale_amount,
                       p.image_urls, COALESCE(p.photo_count, 0) AS photo_count,
                       m.property_address_norm AS address_norm
                  FROM motivated_sellers m
            {photo_join} property_photos p ON p.address_norm = m.property_address_norm
             LEFT JOIN tranchi_push_log tpl ON tpl.address_norm = m.property_address_norm
                 WHERE {where} AND tpl.address_norm IS NULL
                 LIMIT ?
            """
        else:
            sql = f"""
                SELECT cs.sale_id AS row_id, cs.property_address AS address, cs.city, cs.state,
                       cs.zip_code AS zip, 'cash_buyer' AS deal_type,
                       cs.buyer_name_raw AS owner_name_raw, cs.buyer_mailing_addr AS owner_mailing_addr,
                       NULL AS est_value, cs.sale_price AS last_sale_amount,
                       p.image_urls, COALESCE(p.photo_count, 0) AS photo_count,
                       cs.property_address_norm AS address_norm
                  FROM cash_sales cs
            {photo_join} property_photos p ON p.address_norm = cs.property_address_norm
             LEFT JOIN tranchi_push_log tpl ON tpl.address_norm = cs.property_address_norm
                 WHERE {where} AND tpl.address_norm IS NULL
                 LIMIT ?
            """
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        _emit(agent, True, {"pushed": 0, "skipped": "no candidates with ≥5 photos"}); return

    # Build tranchi leads payload — one lead per row.
    # Tranchi.ai requires `price` (rejects with "price field is null or empty").
    leads = []
    skipped_no_price = 0
    # Tranchi's on-market QC requires photos sourced directly from the listing
    # site (Zillow, Realtor, Redfin, auction). Generated URLs (Street View,
    # Esri aerial, Google Maps static) are explicitly rejected as "fabricated"
    # — filter them out here. ≥2 listing photos required; ≥5 preferred.
    LISTING_HOSTS = ("photos.zillowstatic.com", "ssl.cdn-redfin.com",
                     "ap.rdcpix.com", "rdcpix.com", "img.realtor.com",
                     "cdn-redfin.com")
    skipped_no_listing_photos = 0
    for r in rows:
        price = r.get("est_value") or r.get("last_sale_amount")
        if not price:
            skipped_no_price += 1
            continue
        all_urls = json.loads(r["image_urls"]) if r["image_urls"] else []
        # Only keep photos from real listing CDNs
        listing_photos = [u for u in all_urls
                          if any(h in u for h in LISTING_HOSTS)]
        if len(listing_photos) < 2:
            skipped_no_listing_photos += 1
            continue
        leads.append({
            "address":        r["address"],
            "city":           r["city"],
            "state":          r["state"],
            "zip_code":       str(r["zip"] or ""),
            "price":          int(price),
            "deal_type":      r["deal_type"],
            "owner_name":     r["owner_name_raw"],
            "owner_mailing":  r["owner_mailing_addr"],
            "estimated_value":r["est_value"],
            "last_sale_amount": r["last_sale_amount"],
            # Tranchi's required field name is `photos` (not `image_urls`).
            "photos":         listing_photos,
            "source":         "cash-buyer-scraper",
            "external_id":    r["row_id"],
        })

    if dry_run:
        _emit(agent, True, {"would_push": len(leads), "sample": leads[:2]},
              meta={"dry_run": True})
        return

    # POST via tranchi-pp-cli leads upload --stdin
    body = json.dumps({"leads": leads})
    try:
        proc = subprocess.run(
            [_pp_bin("tranchi-pp-cli"), "leads", "upload", "--stdin", "--agent"],
            input=body, capture_output=True, text=True, timeout=120, check=False,
        )
    except FileNotFoundError:
        _emit(agent, False, None, errors=["tranchi-pp-cli not found"]); return

    # Log every push, success or fail
    response_status = "ok" if proc.returncode == 0 else "error"
    response_body = (proc.stdout or "") + (proc.stderr or "")
    with open_buyers() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO tranchi_push_log
                  (address_norm, source_table, source_row_id, payload, response_status, response_body, image_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (r["address_norm"], source_table, r["row_id"], json.dumps({"address": r["address"]}),
                 response_status, response_body[:2000], r["photo_count"]),
            )
        conn.commit()

    _emit(agent, response_status == "ok",
          {"pushed": len(leads), "skipped_no_price": skipped_no_price,
           "skipped_no_listing_photos": skipped_no_listing_photos,
           "exit_code": proc.returncode, "response_head": response_body[:500]},
          errors=[] if response_status == "ok" else [response_body[:500]],
          meta={"source_table": source_table, "market": market})


@main.command("enrich-zestimate")
@click.option("--source-table", type=click.Choice(["motivated_sellers", "cash_sales"]),
              default="motivated_sellers")
@click.option("--market", default=None)
@click.option("--limit", type=int, default=1000)
@click.option("--workers", type=int, default=8,
              help="concurrent BrightData scrapes (8 is safe on Web Unlocker)")
@click.option("--min-price", type=int, default=2000,
              help="ignore Zestimates below this (matches tranchi's minimum)")
@click.option("--agent", is_flag=True)
def cmd_enrich_zestimate(source_table: str, market: str | None, limit: int,
                          workers: int, min_price: int, agent: bool) -> None:
    """Backfill est_value via Zillow Zestimate scrape (~$0.001 per address).

    For rows with no price, scrape the Zillow PDP via BrightData Web Unlocker
    and regex out the Zestimate. Falls back to address-redirect URL
    (https://www.zillow.com/homes/<addr>_rb/) when listing_url isn't set.

    Cheaper than BatchData property lookup ($0.05/credit) by ~50x; trades
    coverage (not every address has a Zillow Zestimate) for cost.
    """
    import re as _re
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    import urllib.parse

    token = os.environ.get("BRIGHTDATA_TOKEN")
    if not token:
        env = Path.home() / ".openclaw" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("BRIGHTDATA_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    if not token:
        _emit(agent, False, None, errors=["BRIGHTDATA_TOKEN not set"]); return

    try:
        from property_enrichment.brightdata import BrightDataMCPClient
    except ImportError:
        _emit(agent, False, None, errors=["property_enrichment not installed"]); return

    # Zillow surfaces Zestimate in plain text "Zestimate ... is $124,000"
    # Be tolerant: any "Zestimate ... is $N" pattern in the page.
    ZEST_RE = _re.compile(r"Zestimate[^$<\"]{0,80}is\s*\$([\d,]+)", _re.IGNORECASE)
    # Backup: structured JSON in the page often has "price": 124000
    JSON_PRICE_RE = _re.compile(r'"zestimate"\s*:\s*(\d+)')

    where = "1=1"
    params: list = []
    if market:
        where += f" AND m.market = ?"
        params.append(market)
    table_join = (
        "motivated_sellers m" if source_table == "motivated_sellers"
        else "cash_sales m")
    # cash_sales uses sale_price; we backfill est_value-equivalent into NULL price column
    target_col = "est_value" if source_table == "motivated_sellers" else "sale_price"

    with open_buyers(read_only=True) as conn:
        sql = f"""
            SELECT m.property_address_norm AS address_norm,
                   m.property_address AS address,
                   m.city, m.state, m.zip_code, m.{('listing_url' if source_table=='motivated_sellers' else 'NULL listing_url')}
              FROM {table_join}
             WHERE m.{target_col} IS NULL
               AND {where}
             LIMIT ?
        """
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        _emit(agent, True, {"updated": 0, "skipped": "no candidates with NULL price"}); return

    db_lock = threading.Lock()
    updated = 0
    no_zestimate = 0
    too_low = 0
    fetch_failed = 0
    errors: list[str] = []

    # Thread-local BrightData client (each worker gets its own session)
    tls = threading.local()
    def client():
        if not hasattr(tls, "c"):
            tls.c = BrightDataMCPClient(token)
        return tls.c

    def process(row):
        # Pick best URL — saved listing_url if any, else address-redirect
        url = row.get("listing_url")
        if not url:
            slug = urllib.parse.quote(", ".join(filter(None, [
                row["address"].split(",")[0].strip(),
                row.get("city") or "", row.get("state") or "",
                str(row.get("zip_code") or "")
            ])))
            url = f"https://www.zillow.com/homes/{slug}_rb/"
        try:
            html = client().call_tool("scrape_as_html", {"url": url}, timeout=60)
        except Exception as e:
            return row, None, f"scrape failed: {e}"[:120]
        if not html or not isinstance(html, str):
            return row, None, "empty html"
        m = ZEST_RE.search(html) or JSON_PRICE_RE.search(html)
        if not m:
            return row, None, "no Zestimate in page"
        try:
            value = int(m.group(1).replace(",", ""))
        except ValueError:
            return row, None, f"unparseable: {m.group(1)}"
        return row, value, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(process, r) for r in rows]
        for fut in as_completed(futures):
            row, value, err = fut.result()
            if err == "no Zestimate in page":
                no_zestimate += 1
                continue
            if err:
                fetch_failed += 1
                if len(errors) < 5:
                    errors.append(err)
                continue
            if value < min_price:
                too_low += 1
                continue
            with db_lock:
                with open_buyers() as conn:
                    if source_table == "motivated_sellers":
                        conn.execute("UPDATE motivated_sellers SET est_value = ? WHERE property_address_norm = ?",
                                     (value, row["address_norm"]))
                    else:
                        conn.execute("UPDATE cash_sales SET sale_price = ? WHERE property_address_norm = ?",
                                     (value, row["address_norm"]))
                    conn.commit()
            updated += 1

    _emit(agent, True,
          {"updated": updated, "no_zestimate": no_zestimate,
           "below_min_price": too_low, "fetch_failed": fetch_failed,
           "candidates": len(rows)},
          errors=errors[:5], meta={"source_table": source_table, "market": market, "min_price": min_price})


@main.command("upgrade-photos-zillow")
@click.option("--source-table", type=click.Choice(["motivated_sellers", "cash_sales"]),
              default="motivated_sellers")
@click.option("--market", default=None)
@click.option("--limit", type=int, default=5000)
@click.option("--workers", type=int, default=8,
              help="concurrent BrightData scrapes (8 is safe on Web Unlocker)")
@click.option("--min-photos", type=int, default=2,
              help="require at least this many listing-CDN URLs to overwrite the row")
@click.option("--agent", is_flag=True)
def cmd_upgrade_photos_zillow(source_table: str, market: str | None, limit: int,
                               workers: int, min_photos: int, agent: bool) -> None:
    """Re-scrape Zillow PDPs to swap Street View / Esri fallbacks for real listing photos.

    Targets rows whose property_photos.image_urls contains zero listing-CDN
    URLs (i.e. only Street View, Esri, or Maps Static fallbacks). On hit, the
    row is overwritten with ONLY the listing-CDN photos pulled from Zillow's
    PDP — making the record eligible for tranchi's on-market QC.

    Misses are left alone so the existing fallback photos stay available for
    any other downstream consumer.
    """
    import re as _re
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    import urllib.parse

    token = os.environ.get("BRIGHTDATA_TOKEN")
    if not token:
        env = Path.home() / ".openclaw" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("BRIGHTDATA_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    if not token:
        _emit(agent, False, None, errors=["BRIGHTDATA_TOKEN not set"]); return

    try:
        from property_enrichment.brightdata import BrightDataMCPClient
    except ImportError:
        _emit(agent, False, None, errors=["property_enrichment not installed"]); return

    # Zillow gallery photos are served from photos.zillowstatic.com/fp/<hash>
    # in several size variants (cc_ft_192/384/576/768/1536, .webp and .jpg).
    # Capture just the hex hash so we dedupe per slot.
    PHOTO_RE = _re.compile(r'photos\.zillowstatic\.com/fp/([a-f0-9]{16,})', _re.IGNORECASE)
    # SQL: rows with photos but no listing-CDN URLs. Photo JSON is a TEXT
    # column so substring match works.
    where = ("p.image_urls NOT LIKE '%photos.zillowstatic.com%'"
             " AND p.image_urls NOT LIKE '%ssl.cdn-redfin.com%'"
             " AND p.image_urls NOT LIKE '%ap.rdcpix.com%'"
             " AND p.image_urls NOT LIKE '%img.realtor.com%'")
    params: list = []
    if market:
        where += " AND m.market = ?"
        params.append(market)
    table = "motivated_sellers" if source_table == "motivated_sellers" else "cash_sales"
    listing_col = "m.listing_url" if source_table == "motivated_sellers" else "NULL"

    with open_buyers(read_only=True) as conn:
        sql = f"""
            SELECT m.property_address_norm AS address_norm,
                   m.property_address AS address,
                   m.city, m.state, m.zip_code,
                   {listing_col} AS listing_url
              FROM {table} m
              JOIN property_photos p ON p.address_norm = m.property_address_norm
             WHERE {where}
             LIMIT ?
        """
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        _emit(agent, True, {"updated": 0, "skipped": "no candidates"}); return

    db_lock = threading.Lock()
    upgraded = 0
    no_photos_found = 0
    fetch_failed = 0
    errors: list[str] = []

    tls = threading.local()
    def client():
        if not hasattr(tls, "c"):
            tls.c = BrightDataMCPClient(token)
        return tls.c

    def process(row):
        url = row.get("listing_url")
        if not url:
            slug = urllib.parse.quote(", ".join(filter(None, [
                row["address"].split(",")[0].strip(),
                row.get("city") or "", row.get("state") or "",
                str(row.get("zip_code") or "")
            ])))
            url = f"https://www.zillow.com/homes/{slug}_rb/"
        try:
            html = client().call_tool("scrape_as_html", {"url": url}, timeout=60)
        except Exception as e:
            return row, None, f"scrape failed: {e}"[:120]
        if not html or not isinstance(html, str):
            return row, None, "empty html"
        # Dedupe by photo hash; request the 960px jpg variant.
        seen: set[str] = set()
        photos = []
        for m in PHOTO_RE.finditer(html):
            h = m.group(1).lower()
            if h in seen:
                continue
            seen.add(h)
            photos.append(f"https://photos.zillowstatic.com/fp/{h}-cc_ft_960.jpg")
        return row, photos, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(process, r) for r in rows]
        for fut in as_completed(futures):
            row, photos, err = fut.result()
            if err:
                fetch_failed += 1
                if len(errors) < 5:
                    errors.append(err)
                continue
            if len(photos) < min_photos:
                no_photos_found += 1
                continue
            with db_lock:
                with open_buyers() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO property_photos
                          (address_norm, image_urls, photo_count, source, source_url)
                        VALUES (?, ?, ?, 'zillow', NULL)
                        """,
                        (row["address_norm"], json.dumps(photos), len(photos)),
                    )
                    conn.commit()
            upgraded += 1

    _emit(agent, True,
          {"upgraded": upgraded, "no_listing_photos": no_photos_found,
           "fetch_failed": fetch_failed, "candidates": len(rows)},
          errors=errors[:5],
          meta={"source_table": source_table, "market": market, "min_photos": min_photos})


@main.command("tranchi-backfill-photos")
@click.option("--source-table", type=click.Choice(["motivated_sellers", "cash_sales"]),
              default="motivated_sellers")
@click.option("--market", default=None)
@click.option("--limit", type=int, default=10000)
@click.option("--min-photos", type=int, default=5)
@click.option("--dry-run", is_flag=True)
@click.option("--agent", is_flag=True)
def cmd_tranchi_backfill_photos(source_table: str, market: str | None, limit: int,
                                 min_photos: int, dry_run: bool, agent: bool) -> None:
    """Push image_urls to /api/leads/enrich for properties already in tranchi.

    Use this when push-tranchi-leads reports many addresses as "duplicate"
    (tranchi already has the lead — they just lack photos). This decorates
    the existing leads with image_urls so the wholesaler-facing app can
    render them.

    Driven by the photo-enrichment-pipeline's `tranchi-backfill` CLI.
    """
    where = "1=1"
    params: list = []
    if market:
        where += " AND m.market = ?"
        params.append(market)
    table = "motivated_sellers" if source_table == "motivated_sellers" else "cash_sales"
    addr_col = "property_address" if source_table == "motivated_sellers" else "property_address"

    with open_buyers(read_only=True) as conn:
        if source_table == "motivated_sellers":
            sql = f"""
                SELECT m.property_address AS address, p.image_urls
                  FROM motivated_sellers m
                  JOIN property_photos p ON p.address_norm = m.property_address_norm
                 WHERE {where}
                 LIMIT ?
            """
        else:
            sql = f"""
                SELECT cs.property_address AS address, p.image_urls
                  FROM cash_sales cs
                  JOIN property_photos p ON p.address_norm = cs.property_address_norm
                 WHERE 1=1 {('AND cs.market = ?' if market else '')}
                 LIMIT ?
            """
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if not rows:
        _emit(agent, True, {"posted": 0, "skipped": "no candidates"}); return

    # Only forward photos from real listing CDNs (Zillow, Realtor, Redfin).
    # Tranchi's QC explicitly rejects "fabricated" URLs — Street View, Esri,
    # Google Static Maps. Filter those out before posting to /api/leads/enrich.
    LISTING_HOSTS = ("photos.zillowstatic.com", "ssl.cdn-redfin.com",
                     "ap.rdcpix.com", "rdcpix.com", "img.realtor.com",
                     "cdn-redfin.com")
    items = []
    dropped_no_listing = 0
    for r in rows:
        urls = [u for u in json.loads(r["image_urls"])
                if any(h in u for h in LISTING_HOSTS)]
        if len(urls) < 2:
            dropped_no_listing += 1
            continue
        items.append({"address": r["address"], "image_urls": urls})
    if not items:
        _emit(agent, True, {"posted": 0, "dropped_no_listing": dropped_no_listing}); return

    # write to a temp file and shell out to the library CLI
    import tempfile
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(items, tf); tf.close()

    # Load TRANCHI_TOKEN from config.toml if not in env
    if not os.environ.get("TRANCHI_TOKEN"):
        cfg = Path.home() / ".config" / "tranchi-pp-cli" / "config.toml"
        if cfg.exists():
            for line in cfg.read_text().splitlines():
                if line.startswith("access_token"):
                    tok = line.split("=", 1)[1].strip().strip("'\"")
                    if tok:
                        os.environ["TRANCHI_TOKEN"] = tok
                        break

    # Resolve the photo-enrichment CLI, including the running interpreter's
    # bin/ (which is where pip install -e drops the entry point).
    photo_cli = shutil.which("photo-enrichment") or str(
        Path(sys.executable).parent / "photo-enrichment")
    cmd = [photo_cli, "tranchi-backfill", tf.name, "--min-photos", str(min_photos)]
    if dry_run:
        cmd.append("--dry-run")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                             env={**os.environ})
    except FileNotFoundError:
        _emit(agent, False, None,
              errors=["photo-enrichment CLI not found — pip install -e ~/photo-enrichment-pipeline"]); return

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {"raw_stdout": proc.stdout[:500], "raw_stderr": proc.stderr[:500]}

    _emit(agent, proc.returncode == 0, result,
          meta={"source_table": source_table, "market": market, "items_file": tf.name})


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


TRANCHI_CASH_BUYERS_URL = "https://tranchi.ai/api/cash_buyers"


def _load_tranchi_token() -> str | None:
    """Return the tranchi bearer token from env or ~/.openclaw/.env."""
    tok = os.environ.get("TRANCHI_TOKEN")
    if tok:
        return tok
    env_file = Path.home() / ".openclaw" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("TRANCHI_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                if tok:
                    os.environ["TRANCHI_TOKEN"] = tok
                    return tok
    return None


def _post_tranchi_cash_buyers(payload: list[dict], token: str, timeout: float = 30.0) -> dict:
    """POST the cash-buyer batch to tranchi.ai and return the parsed envelope.

    Endpoint accepts a JSON array (or single object). Response is the standard
    {ok, data:{created,updated,errors}, meta:{total,errors:[{index,error}]}}
    envelope; per-record validation errors come back in meta.errors[].
    Raises urllib.error.URLError / HTTPError on transport failures.
    """
    import urllib.request
    import urllib.error

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TRANCHI_CASH_BUYERS_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "cash-buyer-scraper/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@main.command("push-tranchi")
@click.option("--top", type=int, default=20,
              help="max candidates to consider (after activity-tier filter)")
@click.option("--market", default=None,
              help="restrict to buyers with at least one sale in this market")
@click.option("--batch-size", type=int, default=50,
              help="records per POST (server accepts arrays)")
@click.option("--include-cold", is_flag=True,
              help="also include cold/dormant tiers (default: hot+warm only)")
@click.option("--dry-run", is_flag=True,
              help="build the payload but skip the POST; prints the first batch")
@click.option("--agent", is_flag=True)
def cmd_push_tranchi(top: int, market: str | None, batch_size: int,
                     include_cold: bool, dry_run: bool, agent: bool) -> None:
    """POST qualified buyers to tranchi.ai/api/cash_buyers.

    Direct HTTPS POST with Bearer auth (TRANCHI_TOKEN from env or
    ~/.openclaw/.env). Batches into arrays of --batch-size; the endpoint
    is UPSERT-by-external_id so re-runs are idempotent.

    Endpoint schema discovered 2026-05-19:
      Required: external_id (string), name (string)
      Optional: phone, email, mailing_address, entity_type, velocity_12m,
                median_purchase_price, property_type, activity_tier,
                markets (array), source
    """
    token = None if dry_run else _load_tranchi_token()
    if not dry_run and not token:
        _emit(agent, False, None,
              errors=["TRANCHI_TOKEN not set — add to ~/.openclaw/.env or export it"])
        return

    where = ["bs.activity_tier IN ('hot', 'warm')"] if not include_cold else ["1=1"]
    params: list = []
    if market:
        where.append("EXISTS (SELECT 1 FROM cash_sales cs WHERE cs.entity_id = be.entity_id AND cs.market = ?)")
        params.append(market)
    params.append(top)

    sql = f"""
    SELECT be.entity_id, be.canonical_name, be.entity_type, be.primary_mailing,
           bs.velocity_12m, bs.median_purchase_price, bs.property_type_mode,
           bs.activity_tier, bs.recency_score,
           bc.primary_phone, bc.primary_email,
           (SELECT GROUP_CONCAT(DISTINCT cs.market)
              FROM cash_sales cs
             WHERE cs.entity_id = be.entity_id AND cs.market IS NOT NULL) AS markets_csv
      FROM buyer_entities be
      JOIN buyer_scores bs ON bs.entity_id = be.entity_id
 LEFT JOIN buyer_contacts bc ON bc.entity_id = be.entity_id
     WHERE {' AND '.join(where)}
     ORDER BY bs.recency_score DESC
     LIMIT ?
    """

    with open_buyers() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        def build_record(r: dict) -> dict:
            rec = {
                "external_id":  r["entity_id"],
                "name":         r["canonical_name"],
                "source":       "cash-buyer-scraper",
            }
            if r.get("primary_phone"):           rec["phone"] = r["primary_phone"]
            if r.get("primary_email"):           rec["email"] = r["primary_email"]
            if r.get("primary_mailing"):         rec["mailing_address"] = r["primary_mailing"]
            if r.get("entity_type"):             rec["entity_type"] = r["entity_type"]
            if r.get("velocity_12m") is not None: rec["velocity_12m"] = r["velocity_12m"]
            if r.get("median_purchase_price"):   rec["median_purchase_price"] = r["median_purchase_price"]
            if r.get("property_type_mode"):      rec["property_type"] = r["property_type_mode"]
            if r.get("activity_tier"):           rec["activity_tier"] = r["activity_tier"]
            if r.get("markets_csv"):
                rec["markets"] = [m.strip() for m in r["markets_csv"].split(",") if m.strip()]
            return rec

        payload = [build_record(r) for r in rows]

        if dry_run:
            _emit(agent, True,
                  {"candidates": len(payload),
                   "first_batch_preview": payload[:batch_size]},
                  meta={"dry_run": True, "batch_size": batch_size,
                        "url": TRANCHI_CASH_BUYERS_URL})
            return

        if not payload:
            _emit(agent, True, {"pushed": 0, "candidates": 0},
                  meta={"note": "no buyers match the filter"})
            return

        # Batch POST and accumulate results.
        import urllib.error
        created = updated = err_count = 0
        per_record_errors: list[dict] = []
        transport_errors: list[str] = []

        for start in range(0, len(payload), batch_size):
            batch = payload[start:start + batch_size]
            try:
                resp = _post_tranchi_cash_buyers(batch, token)
            except urllib.error.HTTPError as e:
                transport_errors.append(f"batch {start}-{start+len(batch)}: HTTP {e.code} — {e.read()[:200].decode('utf-8', 'replace')}")
                continue
            except (urllib.error.URLError, OSError) as e:
                transport_errors.append(f"batch {start}-{start+len(batch)}: transport error — {e}")
                continue

            data = resp.get("data") or {}
            created   += int(data.get("created") or 0)
            updated   += int(data.get("updated") or 0)
            err_count += int(data.get("errors")  or 0)

            # The server reports per-record errors against the *batch* index.
            # Translate back to the global index + the actual external_id.
            for er in (resp.get("meta") or {}).get("errors", []):
                bi = er.get("index", -1)
                eid = batch[bi]["external_id"] if 0 <= bi < len(batch) else None
                per_record_errors.append({"external_id": eid, "error": er.get("error")})

            # Log successful pushes to buyer_outreach so subsequent runs can
            # exclude them with --no-recent-outreach (existing pattern).
            err_indices = {er.get("index") for er in (resp.get("meta") or {}).get("errors", [])}
            for i, rec in enumerate(batch):
                if i in err_indices:
                    continue
                conn.execute(
                    """
                    INSERT INTO buyer_outreach
                      (entity_id, channel, direction, summary, response_status)
                    VALUES (?, 'tranchi_push', 'out',
                            'POST /api/cash_buyers (direct)', 'pushed')
                    """,
                    (rec["external_id"],),
                )
        conn.commit()

    ok = not transport_errors and (created + updated) > 0
    _emit(agent, ok,
          {"created": created, "updated": updated, "errors": err_count,
           "candidates": len(payload), "per_record_errors": per_record_errors},
          errors=transport_errors,
          meta={"top": top, "market": market, "batch_size": batch_size,
                "url": TRANCHI_CASH_BUYERS_URL})


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
