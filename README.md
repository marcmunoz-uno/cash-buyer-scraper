# cash-buyer-scraper

**A local-first, agent-first cash-buyer discovery mesh for wholesalers on tranchi.ai.**

A small Python query layer (`cash-buyer-intel`) sits on top of a fleet of Printing-Press-generated CLIs, sources all-cash settlements from public records, deduplicates buyer entities across properties, and pushes qualified cash buyers into the Tranchi production pipeline. Modeled on [`SQLite-CLI-propertydb-mesh`](https://github.com/marcmunoz-uno/SQLite-CLI-propertydb-mesh) — same three-layer pattern, different domain.

```
                    "give me every active cash buyer in St. Louis MO
                     who's bought >=3 SFHs under $200k in the last 12
                     months and isn't already in my outreach log"

                                       │
                              ┌────────▼────────┐
                              │ cash-buyer-intel│
                              │     buyers      │
                              └────────┬────────┘
                                       │
                       ATTACH-time JOIN across the mesh
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
   ┌────▼────────┐               ┌─────▼─────┐                ┌───────▼──────┐
   │  buyers DB  │               │ ATTOM DB  │                │ BatchData DB │
   │ cash_sales  │               │ sale +    │                │ search +     │
   │ + entities  │               │ assessment│                │ skip-trace   │
   │ + outreach  │               └───────────┘                └──────────────┘
   └─────────────┘                attom-pp-cli                 batchdata-pp-cli
   (this repo)                    (Printing Press)             (Printing Press)
```

---

## Table of contents

- [Why this exists](#why-this-exists)
- [The two-tier sourcing strategy](#the-two-tier-sourcing-strategy)
- [Mesh architecture](#mesh-architecture)
- [What's owned by this repo](#whats-owned-by-this-repo)
- [Quick start](#quick-start)
- [Command reference](#command-reference)
- [Buyer scoring](#buyer-scoring)
- [Entity dedup](#entity-dedup)
- [Pushing to tranchi.ai](#pushing-to-tranchiai)
- [Schema](#schema)
- [Companion repos](#companion-repos)
- [Status and roadmap](#status-and-roadmap)
- [Privacy, secrets, and licensing](#privacy-secrets-and-licensing)

---

## Why this exists

Tranchi wholesalers need a steady supply of qualified **cash buyers** to assign contracts to. Today the `cash_buyers` table in production grows from manual entry, ad-hoc lists, and whatever leaks in through investor signups. There is no autonomous pipeline that:

1. Watches county recorder feeds for cash-sale deed recordings (mortgage amount = 0).
2. Deduplicates the same buyer across N properties ("Smith Holdings LLC" vs "Smith Holdings, LLC" vs the LLC's authorized agent).
3. Skip-traces the buyer to a real phone/email.
4. Scores buyer **velocity** (purchases / 12 months), **buy-box** (median price, ZIP cluster, property type), and **recency**.
5. Hands the qualified buyer to tranchi.ai for wholesaler matching.

That is what this repo does. The data sources to *find* cash sales already exist in the PP-generated CLI mesh — ATTOM's sale history, BatchData's search filters, the [county-portal-scraper](https://github.com/marcmunoz-uno/SQLite-CLI-propertydb-mesh) for markets where API coverage is thin. This repo is the domain layer that turns those into cash-buyer entities and pushes them through to production.

---

## The two-tier sourcing strategy

| Tier | Source | What it gives us | Cost | Coverage |
|---|---|---|---|---|
| **A — API** | ATTOM `saleshistory` + BatchData `search_properties` with `cash_buyer` flag | National coverage, structured, refreshed monthly–weekly. Cash flag already present. | $ / lookup | National |
| **B — County portals** | `county-portal-scraper` (the 71 portal SOPs already cracked across 32 markets) | Free, near-real-time, catches deeds the APIs miss | $0 | 32 markets today |

Tier A goes in first — it's the thin wrapper over two existing PP CLIs, deployable in a weekend. Tier B layers in behind it as the cheap-coverage path for the markets we already have cracked. We did not include Zillow-sold reverse-derivation: the cash flag is unreliable there and ATTOM/BatchData already cover the same surface with explicit fields.

Both tiers write to the same `cash_sales` table. Dedup, scoring, and outreach are tier-agnostic — they only see the union.

---

## Mesh architecture

This is **layer 3** of the same three-layer stack the property-intel mesh uses:

1. **Layer 1** — [`cli-printing-press`](https://github.com/mvanhorn/cli-printing-press) (Go generator). Not vendored, not forked. Clean upstream.
2. **Layer 2** — per-API PP CLIs at `~/printing-press/library/<name>/` with binaries on `$PATH`. The CLIs this repo depends on:

   | PP CLI | Role in this repo |
   |---|---|
   | `attom-pp-cli` | Tier A source — `saleshistory` records (cash sale = `mortgage_amount IS NULL OR mortgage_amount = 0`) |
   | `batchdata-pp-cli` | Tier A source — `search_properties` with `cash_buyer=true` filter; skip-trace for buyer phone/email |
   | `googlemaps-pp-cli` | Geocode buyer mailing addresses (for market clustering) |
   | `tranchi-pp-cli` | Push qualified buyers to `tranchi.ai/api/cash_buyers` (needs upload resource — see [Pushing to tranchi.ai](#pushing-to-tranchiai)) |
   | `blooio-pp-cli` | Outreach side-effect — iMessage the buyer once a wholesaler is matched |
   | `telegram-pp-cli` | Operator alerts when a new high-velocity buyer is discovered |

3. **Layer 3** — this repo. Owns `~/cash-buyer-intel/buyers.db`. ATTACHes the PP DBs at query time. Exposes one CLI (`cash-buyer-intel`) with `--agent` JSON mode matching the PP shape.

### Storage model

```
~/.local/share/attom-pp-cli/data.db            ← owned by attom-pp-cli (read-only here)
~/.local/share/batchdata-pp-cli/data.db        ← BatchData is lookup-only; no sync, no source DB.
                                                  We use the OWNED-CACHE pattern (see below).
~/.local/share/googlemaps-pp-cli/data.db       ← owned by googlemaps-pp-cli
~/.local/share/blooio-pp-cli/data.db           ← owned by blooio-pp-cli (read for "already messaged?" JOIN)
~/.local/share/tranchi-pp-cli/data.db          ← owned by tranchi-pp-cli (read for "already pushed?" JOIN)

~/cash-buyer-intel/buyers.db                   ← owned by THIS repo
   ├─ cash_sales            (deed-level: who bought what, when, for how much, $0 mortgage)
   ├─ buyer_entities        (deduped — one row per real buyer across N sales)
   ├─ buyer_entity_aliases  (the name variants that collapse into each entity)
   ├─ buyer_contacts        (skip-trace results: phone, email, LLC officer)
   ├─ buyer_scores          (velocity, buy-box, recency — recomputed nightly)
   ├─ buyer_outreach        (which wholesaler reached out, when, via what channel, response)
   └─ batchdata_cache       (owned-cache; BatchData has no sync, see below)
```

### Owned-cache pattern (BatchData)

BatchData's PP CLI is **point-lookup-only** — no `sync` command, no source SQLite. This repo handles it the same way `SQLite-CLI-propertydb-mesh` does: a local `batchdata_cache` table inside `buyers.db`, populated by `cash-buyer-intel enrich-batchdata`, which subprocesses out to `batchdata-pp-cli` for unenriched rows and UPSERTs the parsed result locally. JOINs target `main.batchdata_cache`, not an attached external DB.

### Process model

- `cash-buyer-intel` — Python click CLI. Owns the buyers DB. ATTACHes the PP DBs. The agent surface.
- `attom-pp-cli`, `batchdata-pp-cli`, `tranchi-pp-cli`, etc. — independent Go binaries, independent auth, independent sync schedules. Not managed by this repo.
- An optional MCP server (`cash-buyer-intel-pp-mcp`, post-MVP) would expose the query surface as MCP tools, matching the pattern in property-intel v0.3.0.

---

## What's owned by this repo

The domain logic that doesn't live in any PP CLI:

1. **Deed → cash_sale extraction.** Tier A: pull from `attom.attom_saleshistory` where `mortgage_amount` is null/zero AND `transaction_type IN ('cash', 'unknown')`. Tier B: parse county-portal deed feeds the same way.
2. **Buyer entity dedup.** Same buyer appears as `JOHN A SMITH`, `Smith, John`, `Smith Holdings LLC`, `SMITH HOLDINGS, L.L.C.`. Tokenize, normalize, fuzzy-match. LLC registered-agent lookup for the harder cases (post-MVP, via Secretary of State scrapers).
3. **Velocity & buy-box scoring.** Count cash sales / 12 months. Median price. Property-type mode. ZIP-cluster centroid + radius. Recency-weighted activity score (purchase 3 months ago counts more than 11 months ago).
4. **Tranchi push contract.** What fields the production `cash_buyers` table needs, validation, dedup against already-pushed buyers, retry on failure.
5. **Outreach state.** `buyer_outreach` is the only writable cross-source table — every iMessage send / call / email logs here so subsequent scoring runs can `--no-recent-outreach` filter them out.

---

## Quick start

```bash
git clone git@github.com:marcmunoz-uno/cash-buyer-scraper.git
cd cash-buyer-scraper
pip install -e .

# initialize the local buyers DB
cash-buyer-intel init-db

# pull cash sales from ATTOM for a market (tier A)
cash-buyer-intel sync-attom --market "St. Louis MO" --since 12m

# pull from BatchData via the owned-cache (tier A)
cash-buyer-intel sync-batchdata --market "St. Louis MO" --cash-only

# dedup buyer entities across all loaded sales
cash-buyer-intel dedup

# score every buyer
cash-buyer-intel score

# query qualified buyers (agent-ready JSON)
cash-buyer-intel buyers \
  --market "St. Louis MO" \
  --min-velocity 3 \
  --max-median-price 200000 \
  --property-type single_family \
  --no-recent-outreach 30d \
  --agent

# push the top 20 to tranchi.ai
cash-buyer-intel push-tranchi --top 20 --market "St. Louis MO"
```

Output of `buyers --agent` matches the PP CLI `--agent` JSON shape (envelope: `{ok, data, errors, meta}`) so any agent or MCP wrapper that already speaks PP can consume it without changes.

---

## Command reference

```
cash-buyer-intel init-db
   Create ~/cash-buyer-intel/buyers.db with the full schema.

cash-buyer-intel sync-attom --market <name> [--since <duration>] [--state <ST>]
   Tier A. Subprocess out to attom-pp-cli to list saleshistory in the market.
   Filter to cash sales (mortgage_amount = 0 OR transaction_type = cash).
   UPSERT into cash_sales.

cash-buyer-intel sync-batchdata --market <name> [--cash-only] [--limit N]
   Tier A. Subprocess out to batchdata-pp-cli search_properties with the
   cash_buyer filter. Results land in batchdata_cache (owned-cache pattern),
   then projected into cash_sales.

cash-buyer-intel sync-county --market <name> --portal <slug>
   Tier B. Read county-portal-scraper output for a market, find deeds with
   no concurrent mortgage record, UPSERT into cash_sales.

cash-buyer-intel enrich-batchdata [--limit N]
   For every cash_sale where buyer_contacts is empty, look up the buyer
   address via batchdata-pp-cli property lookup → skip-trace. UPSERT into
   buyer_contacts.

cash-buyer-intel dedup [--threshold 0.85]
   Run entity resolution across all cash_sales. Produces buyer_entities +
   buyer_entity_aliases.

cash-buyer-intel score [--window 12m]
   Recompute buyer_scores for every entity.

cash-buyer-intel buyers [filters] [--agent]
   The main query. Filters: --market, --state, --min-velocity, --max-median-price,
   --property-type, --zip-cluster, --no-recent-outreach <duration>, --has-phone,
   --has-email, --already-pushed (exclude buyers already in tranchi).

cash-buyer-intel push-tranchi [--top N] [--dry-run]
   POST qualified buyers to tranchi.ai/api/cash_buyers via tranchi-pp-cli.

cash-buyer-intel outreach-log --buyer-id <id> --channel <imessage|email|call> \
                              --wholesaler <user_id> --response <text>
   Record an outreach attempt so subsequent --no-recent-outreach filters work.

cash-buyer-intel probe
   Inspect attached PP DBs and report which tables/columns are present so a
   first-time user knows what's wired vs what's missing.
```

All commands accept `--agent` for structured JSON output.

---

## Buyer scoring

`buyer_scores` is computed by `cash-buyer-intel score`. A buyer's record:

| Field | Definition |
|---|---|
| `velocity_12m` | Count of cash sales in the trailing 12 months. |
| `velocity_3m` | Same, trailing 3 months. The hot-vs-cold split. |
| `median_purchase_price` | Median price across all sales. |
| `p25_price`, `p75_price` | The buy-box price band. |
| `property_type_mode` | SFH, multi, condo, land — the most common type they buy. |
| `zip_cluster_centroid` | Lat/lon centroid of their purchase footprint. |
| `zip_cluster_radius_miles` | 90th-percentile distance from centroid — how tight their market is. |
| `recency_score` | Sum over sales of `exp(-months_since_sale / 6)`. Recent activity dominates. |
| `activity_tier` | `hot` (velocity_3m ≥ 1 AND velocity_12m ≥ 3), `warm` (velocity_12m ≥ 3), `cold` (velocity_12m ≥ 1), `dormant` (else). |

Default `buyers --agent` query returns hot + warm tiers only.

---

## Entity dedup

Same buyer, different name strings. Resolution strategy:

1. **Normalize.** Uppercase, strip punctuation, expand `L.L.C.` → `LLC`, collapse whitespace, drop trailing `LLC`/`INC`/`TRUST` for the comparison key.
2. **Exact match** on normalized form → same entity.
3. **Fuzzy match** above `--threshold` (default 0.85, token-set ratio) AND same mailing address (from ATTOM `owner_address`) → same entity.
4. **LLC officer lookup** (post-MVP) — Secretary of State filings to collapse `Smith Holdings LLC` and `Smith Properties LLC` when they share an authorized agent.

Every alias is preserved in `buyer_entity_aliases` so we can audit a merge and unwind it if it turns out to be wrong.

---

## Pushing to tranchi.ai

The existing `tranchi-pp-cli` exposes a `leads` resource (4 MCP tools: upload, get, refresh, stats). **It does not currently expose `cash_buyers`.** Two paths to close that gap:

1. **Production-side (recommended).** Add a `POST /api/cash_buyers` route on tranchi.ai matching the existing leads-upload contract (Bearer auth, idempotent on `external_id`). Then regenerate `tranchi-pp-cli` from the updated spec and the resource appears automatically.
2. **Bridge route on tranchi-deal-flow-agents.** Add `POST /api/buyers/import` to the Flask sidecar, which then writes to production's `cash_buyers` via its existing `tranchi_client.py`. Avoids touching `TRANCHI.PRODUCTION.CODEBASE` directly.

Path 2 is the lower-risk option given the production-codebase no-touch rule. Either way, this repo's `push-tranchi` subcommand stays a thin subprocess over `tranchi-pp-cli` once the resource exists.

---

## Schema

```sql
CREATE TABLE cash_sales (
    sale_id            TEXT PRIMARY KEY,         -- hash of (property_address_norm, sale_date, buyer_name_norm)
    property_address   TEXT NOT NULL,
    property_address_norm TEXT NOT NULL,         -- lowercase, alphanumeric, single-spaced
    city               TEXT,
    state              TEXT,
    zip_code           TEXT,
    market             TEXT,                     -- "St. Louis MO", "Detroit MI", etc.
    property_type      TEXT,                     -- single_family / multi / condo / land
    sale_date          TEXT NOT NULL,
    sale_price         INTEGER,
    mortgage_amount    INTEGER,                  -- expected 0 / NULL for cash sales
    buyer_name_raw     TEXT NOT NULL,
    buyer_name_norm    TEXT NOT NULL,
    buyer_mailing_addr TEXT,
    seller_name        TEXT,
    source             TEXT NOT NULL,            -- 'attom' | 'batchdata' | 'county_portal'
    source_record_id   TEXT,
    entity_id          TEXT,                     -- FK → buyer_entities.entity_id (NULL until dedup runs)
    loaded_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX cash_sales_market    ON cash_sales(market);
CREATE INDEX cash_sales_entity    ON cash_sales(entity_id);
CREATE INDEX cash_sales_sale_date ON cash_sales(sale_date);

CREATE TABLE buyer_entities (
    entity_id          TEXT PRIMARY KEY,         -- hash of canonical name + mailing addr
    canonical_name     TEXT NOT NULL,
    entity_type        TEXT,                     -- 'individual' | 'llc' | 'trust' | 'corp' | 'unknown'
    primary_mailing    TEXT,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    total_sales        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE buyer_entity_aliases (
    entity_id          TEXT NOT NULL,
    alias_name_norm    TEXT NOT NULL,
    alias_name_raw     TEXT NOT NULL,
    source             TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias_name_norm)
);

CREATE TABLE buyer_contacts (
    entity_id          TEXT PRIMARY KEY,
    primary_phone      TEXT,
    primary_email      TEXT,
    llc_authorized_agent TEXT,
    skip_traced_at     TEXT,
    confidence         REAL                      -- 0-1 from BatchData skip-trace
);

CREATE TABLE buyer_scores (
    entity_id          TEXT PRIMARY KEY,
    velocity_12m       INTEGER NOT NULL,
    velocity_3m        INTEGER NOT NULL,
    median_purchase_price INTEGER,
    p25_price          INTEGER,
    p75_price          INTEGER,
    property_type_mode TEXT,
    zip_cluster_centroid_lat REAL,
    zip_cluster_centroid_lon REAL,
    zip_cluster_radius_miles REAL,
    recency_score      REAL NOT NULL,
    activity_tier      TEXT NOT NULL,            -- hot | warm | cold | dormant
    scored_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE buyer_outreach (
    outreach_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id          TEXT NOT NULL,
    wholesaler_user_id TEXT,                     -- tranchi.ai user_id
    channel            TEXT NOT NULL,            -- imessage | email | call | direct_mail
    direction          TEXT NOT NULL,            -- out | in
    summary            TEXT,
    response_status    TEXT,                     -- none | replied | bounced | unsubscribed
    occurred_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX buyer_outreach_entity ON buyer_outreach(entity_id);

-- Owned-cache for BatchData (lookup-only PP CLI has no source DB).
CREATE TABLE batchdata_cache (
    address_norm       TEXT PRIMARY KEY,
    raw_response       TEXT NOT NULL,            -- JSON
    primary_phone      TEXT,
    is_cash_buyer      INTEGER,
    owner_name         TEXT,
    owner_state        TEXT,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

## Companion repos

| Repo | Role |
|---|---|
| [`SQLite-CLI-propertydb-mesh`](https://github.com/marcmunoz-uno/SQLite-CLI-propertydb-mesh) | Sister mesh — same pattern, property-side. Shares the PP CLI fleet. |
| [`cli-printing-press`](https://github.com/mvanhorn/cli-printing-press) | Layer 1 generator. Not vendored. |
| `county-portal-scraper` | Tier B source — feeds deed records for the 32 markets / 71 portals already cracked. |
| [`tranchi-deal-flow-agents`](https://github.com/marcmunoz-uno/tranchi-deal-flow-agents) | The bridge for production push (path 2 in [Pushing to tranchi.ai](#pushing-to-tranchiai)). |
| `TRANCHI.PRODUCTION.CODEBASE` | The user-facing tranchi.ai app. **This repo never touches it directly.** All writes go via `tranchi-pp-cli` or the deal-flow-agents bridge. |

---

## Status and roadmap

**v0.1 — Tier A only (current scaffold).**
- ATTOM sync, BatchData owned-cache, dedup, score, query, push.
- Single-market end-to-end run.

**v0.2 — Tier B.**
- Wire `sync-county` to `county-portal-scraper` output for the 32 markets already cracked.
- Cross-tier dedup: same buyer found in ATTOM AND county portal collapses cleanly.

**v0.3 — Outreach loop.**
- `cash-buyer-intel match` matches wholesaler buy-boxes to discovered buyers.
- Automatic blooio iMessage drafting (review-required, not auto-send).
- Reply-handling and `buyer_outreach` ingest.

**v0.4 — LLC officer resolution.**
- Secretary-of-State scrapers for the top 10 states.
- Collapse `<Person> Holdings LLC` and `<Person> Properties LLC` under one entity when they share an authorized agent.

**v0.5 — MCP server.**
- Expose the query surface as `cash-buyer-intel-pp-mcp` for direct Claude/OpenClaw consumption.

---

## Privacy, secrets, and licensing

- **No PII in version control.** All scraped data lives in `~/cash-buyer-intel/buyers.db` — never committed (see `.gitignore`).
- **API tokens** belong to the PP CLIs (`attom-pp-cli auth set-token`, `batchdata-pp-cli auth set-token`, etc.), not this repo.
- **DNC / TCPA compliance** for outreach is enforced via `batchdata-pp-cli phone check-dnc` and `phone check-tcpa` before any `buyer_outreach` row gets written.
- **License:** Apache-2.0. See `LICENSE`.
