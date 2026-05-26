# On-demand cash-buyer dispenser

The dispenser is a small HTTP service that lets callers (Tranchi's
front-end, today; any other consumer tomorrow) request **N cash buyers for
a given market on behalf of a specific user**, with cost passed back to the
caller so end users pay through Tranchi's existing billing rather than us
eating the BatchData spend.

Every dispensed row carries a real phone or email pulled from BatchData
skip-trace. We do not fabricate contact data — entities without a real
match are **not** dispensable.

---

## Why this exists

Today, sourcing a fresh batch of cash buyers is a manual operator workflow:
`sync-batchdata` → `dedup` → `score` → `enrich-batchdata` → `push-tranchi`.
That works for bulk loads, but it doesn't fit the "Tranchi user clicks
*Get 50 cash buyers in Cleveland*" flow where:

- The request is **per-user** (the same buyer should never be dispensed twice
  to the same user).
- The request is **bounded** (the user picks a quantity; we don't ship a
  whole metro at once).
- The **cost lands on the user**, not on us.
- The fulfillment is **best-effort fast** — serve from cache when we have
  it; only hit BatchData when cache runs short.

The dispenser is the HTTP front door for that workflow.

---

## Endpoints

All protected endpoints require `Authorization: Bearer <DISPENSER_TOKEN>`.
Token is read from `$DISPENSER_TOKEN` or `~/.openclaw/.env`
(`DISPENSER_TOKEN=...`). Multiple tokens can be comma-separated for
per-tenant identification later; v0 just checks set membership.

Response envelope mirrors the rest of cash-buyer-intel:
`{"ok": bool, "data": ..., "errors": [...], "meta": {...}}`.

### `GET /healthz`

Liveness probe. No auth.

```json
{"ok": true, "data": {"status": "ok"}, "errors": [], "meta": {}}
```

### `GET /api/dispense/stock?market=<m>&user_id=<u>`

How many cache-bearing, never-dispensed-to-this-user buyers we could
serve right now. Use this to render an estimate before the user commits
to a quantity.

```json
{"ok": true,
 "data": {"market": "Cleveland OH", "user_id": "u_123", "stock": 326}}
```

### `POST /api/dispense`

The main endpoint. Body:

```jsonc
{
  "user_id":        "u_123",          // required — stable per recipient
  "market":         "Cleveland OH",   // required — must match cash_sales.market
  "quantity":       50,               // required, 1-500
  "fetch_if_short": true              // optional, default true
}
```

Response (cache fulfills the entire request):

```jsonc
{
  "ok": true,
  "data": {
    "dispensed":          [<buyer record>, ...],   // up to `quantity`
    "dispensed_count":    50,
    "shortfall":          0,
    "estimated_cost_usd": 0.0,
    "job_id":             null
  },
  "meta": {"market": "Cleveland OH", "user_id": "u_123", "quantity": 50}
}
```

Response (cache short — async fetch kicked off):

```jsonc
{
  "ok": true,
  "data": {
    "dispensed":          [<32 cache hits>],
    "dispensed_count":    32,
    "shortfall":          18,
    "estimated_cost_usd": 1.386,          // 18 × $0.077 (our raw BatchData cost)
    "job_id":             "disp_a1b2c3d4e5f6"
  }
}
```

`estimated_cost_usd` is **our** raw cost — caller applies their markup.

#### Buyer record shape

Matches the **Cash Buyer Pool Upload API** spec — the same record format `push-tranchi` sends to `POST /api/cash_buyers`. Required fields are always present (may be null); optional fields are only included when we have a non-null value.

```jsonc
{
  "external_id":     "ent_363b96f48566100c",      // required
  "name":            "ALAIA HOLDINGS",            // required
  "source":          "cash-buyer-scraper",
  // Critical for matching — always present, may be null
  "phone":           "2163039406",                // from BatchData skip-trace
  "email":           "greeneteague1114@gmail.com",
  "market":          "Cleveland OH",              // singular string, not array
  "state":           "OH",
  // Optional — only included when we have data
  "mailing_address": "13940 Cedar Rd, Cleveland, OH, 44118",
  "entity_type":     "llc",
  "llc_agent":       "...",
  "total_sales":     3,
  "velocity_12m":    3,
  "velocity_3m":     0,
  "median_price":    185000,
  "p25_price":       95000,
  "p75_price":       280000,
  "property_type_mode": "single_family",
  "zip_cluster_lat":    41.50,
  "zip_cluster_lon":   -81.69,
  "zip_cluster_radius": 12.0,
  "recency_score":   0.95,
  "activity_tier":   "warm",
  "confidence":      0.9
}
```

### `GET /api/dispense/jobs/{job_id}`

Poll an async fulfillment job. Status transitions:
`queued` → `running` → `complete` | `failed`.

```jsonc
{
  "ok": true,
  "data": {
    "job_id":         "disp_a1b2c3d4e5f6",
    "user_id":        "u_123",
    "status":         "complete",
    "cached_count":   0,
    "fetched_count":  18,
    "total_cost_usd": 1.386,
    "request_json":   "{\"market\":\"Cleveland OH\",\"quantity\":18,...}",
    "created_at":     "2026-05-26 23:42:01",
    "completed_at":   "2026-05-26 23:43:55",
    "error":          null,
    "buyers":         [<the 18 newly-fetched buyer records>]
  }
}
```

When `status="failed"`, the `error` field carries the upstream message
(usually a CLI subprocess error tail).

### `GET /api/dispense/history?user_id=<u>&limit=N`

The most recent buyers dispensed to a user. Useful for showing the user's
"already received" list in the front-end UI.

```jsonc
{
  "ok": true,
  "data": {
    "user_id": "u_123",
    "count":   62,
    "buyers":  [{
      "entity_id":     "ent_...",
      "canonical_name": "...",
      "primary_phone": "...",
      "primary_email": "...",
      "source":        "cache" | "fetch",
      "cost_usd":      0.0 | 0.077,
      "dispensed_at":  "2026-05-26 19:42:11",
      ...
    }, ...]
  }
}
```

---

## Cost model

We report our raw BatchData cost; the caller (Tranchi) decides markup
and bills the end user through their existing Stripe flow.

| Source | Per-record cost (USD) | When |
|---|---|---|
| `cache` | 0.0 | Buyer was already enriched on a prior run; reusing free. |
| `fetch` | ~0.077 | New BatchData skip-trace, observed yield on 2026-05-26: $50 → 652 fully-enriched records. |

A request that needs 50 buyers split 32-cache / 18-fetch costs us about
**$1.39**. The caller can charge the user any markup over that.

---

## Running the service

```bash
cd cash-buyer-scraper
.venv/bin/pip install -e ".[dispenser]"

# Generate or set the token first.
export DISPENSER_TOKEN="<random-token>"          # or add DISPENSER_TOKEN=... to ~/.openclaw/.env

cash-buyer-intel init-db                          # creates the dispenses + dispense_jobs tables
cash-buyer-intel serve --host 0.0.0.0 --port 8765
```

Smoke test:

```bash
curl http://127.0.0.1:8765/healthz

curl -H "Authorization: Bearer $DISPENSER_TOKEN" \
  "http://127.0.0.1:8765/api/dispense/stock?market=Cleveland%20OH&user_id=u_test_1"

curl -X POST -H "Authorization: Bearer $DISPENSER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"user_id":"u_test_1","market":"Cleveland OH","quantity":3,"fetch_if_short":false}' \
     http://127.0.0.1:8765/api/dispense
```

OpenAPI / Swagger UI is auto-generated at `http://127.0.0.1:8765/docs`.

---

## Tranchi front-end integration sketch

The intended end-to-end flow:

1. User clicks **Get cash buyers** on the Tranchi UI; modal shows market
   dropdown + quantity slider.
2. Tranchi front-end calls Tranchi's backend with the request.
3. Tranchi backend calls **us**:
   ```http
   POST https://<dispenser-host>/api/dispense
   Authorization: Bearer <DISPENSER_TOKEN-for-tranchi>
   {"user_id": "<tranchi user id>", "market": "...", "quantity": N}
   ```
4. Dispenser returns either:
   - Synchronous fulfillment (cache covers it) → Tranchi attaches buyers
     to the user's account, charges via Stripe at their markup, done.
   - `job_id` → Tranchi shows the user a "fetching N more buyers, ETA ~Xs"
     spinner, polls `/api/dispense/jobs/{id}` every 5-10s, and finalises
     when the job is `complete`.
5. Buyers land in the user's Tranchi cash-buyer list with phone + email
   ready for outreach.

The dispenser does **not** post buyers to `tranchi.ai/api/cash_buyers`
itself. That endpoint is for the *bulk operator workflow* documented in
the [Obsidian Tranchi API endpoints note](../../ObsidianVault/Openclaw/Reference%20—%20Tranchi%20API%20Endpoints%20+%20CLI%20Commands.md).
The dispenser is the *per-user, on-demand* path; Tranchi's backend
decides what to do with the returned buyers (attach to user, push to
their internal cash_buyers table, etc).

---

## Known v0 limitations

- **In-process job queue.** If the dispenser process restarts mid-job,
  the row stays in `running` forever. A future `cash-buyer-intel
  reap-stale-jobs --older-than 30m` cron would mark them `failed`.
- **No per-user rate limits.** Bearer-token check is binary today
  (valid / invalid). Per-tenant quotas (max requests/hour, max
  quantity/day) belong here before this is production-exposed.
- **No webhook callbacks.** Caller must poll `/api/dispense/jobs/{id}`.
  A `callback_url` field on `DispenseRequest` is the next obvious add.
- **Subprocess fan-out for fetch jobs.** `_worker` shells out to
  `cash-buyer-intel sync-batchdata` / `dedup` / `score` /
  `enrich-batchdata`. That's intentional — it keeps the side-effects
  behind the existing tested command boundary — but it does mean each
  job spawns several processes.
- **No multi-market dispense.** One job, one market. If the user wants
  buyers across 3 markets they make 3 requests.
