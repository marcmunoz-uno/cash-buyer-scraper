# Cash Buyer Pool Upload API — Documentation

## Overview

The Cash Buyer Pool API allows the `cash-buyer-scraper` pipeline to push qualified cash buyers into Tranchi's buyer pool. Users who click "Get Cash Buyers" on a property are matched against this pool by market, state, price range, and contact availability.

**Base URL:** `https://tranchi.ai`

---

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/cash_buyers` | Bearer token | Upload buyers to the pool (upsert) |
| `GET` | `/api/cash_buyers/schema` | None | Self-documenting schema reference |
| `GET` | `/api/cash_buyers/stats` | Bearer token | Pool statistics |

---

## Authentication

All write endpoints require a Bearer token in the `Authorization` header:

```
Authorization: Bearer <LEADS_API_KEY>
```

The `LEADS_API_KEY` is the same key used by the leads pipeline. It is configured as an environment variable on Tranchi and must be set in `tranchi-pp-cli` via `tranchi-pp-cli auth set-token`.

---

## POST /api/cash_buyers

Upload one or more qualified cash buyers to the pool. **Idempotent on `external_id`** — sending the same buyer again updates their record rather than creating a duplicate.

### Request

```http
POST /api/cash_buyers HTTP/1.1
Host: tranchi.ai
Authorization: Bearer <LEADS_API_KEY>
Content-Type: application/json
```

### Accepted Body Formats

The endpoint accepts three formats:

**Format 1 — Array wrapper (recommended for batches):**
```json
{
  "buyers": [
    { "external_id": "...", "name": "...", ... },
    { "external_id": "...", "name": "...", ... }
  ]
}
```

**Format 2 — Raw array:**
```json
[
  { "external_id": "...", "name": "...", ... },
  { "external_id": "...", "name": "...", ... }
]
```

**Format 3 — Single object:**
```json
{ "external_id": "...", "name": "...", ... }
```

### Limits

- Maximum **500 buyers per request**
- Rate limit: **100 requests per 15 minutes**

---

## Field Reference

### Required Fields

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `external_id` | string | Unique entity ID from the scraper. Used for idempotent upserts — same ID = update, new ID = insert. | `"mo-jackson-guardian-fund-llc"` |
| `name` | string | Canonical buyer name (individual or entity). | `"GUARDIAN FUND LLC"` |

### Critical for Matching

These fields determine whether a buyer is surfaced to users. **Without `market`/`state` AND at least one of `phone`/`email`, the buyer is invisible.**

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `market` | string | City/metro where buyer's transactions occurred. **PRIMARY matching field** — matched via SQL `LIKE '%city%'` against the property's city. Use consistent naming. | `"Kansas City"`, `"Memphis"`, `"St. Louis MO"` |
| `state` | string (2-char) | State code. Used as fallback when city match yields < 5 buyers. | `"MO"`, `"TN"`, `"OH"` |
| `phone` | string | Skip-traced phone number. Buyers without phone OR email are **excluded** from all matching. | `"+13145551234"` |
| `email` | string | Skip-traced email address. | `"deals@guardianfund.com"` |

### Important for Ranking

These fields determine the **order** in which buyers are shown to users. Higher velocity and recency = shown first.

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `velocity_12m` | integer | Cash purchases in trailing 12 months. Primary sort key. | `12` |
| `recency_score` | number (0–1) | How recently the buyer was active. Secondary sort key. | `0.95` |
| `activity_tier` | enum | Activity classification: `"hot"`, `"warm"`, `"cold"`, `"dormant"`. Hot/warm prioritized. | `"hot"` |
| `p25_price` | integer | 25th percentile purchase price — low end of buy box. | `80000` |
| `p75_price` | integer | 75th percentile purchase price — high end of buy box. Property price must fall within p25–p75 (±30% tolerance). | `250000` |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `entity_type` | enum | `"unknown"` | One of: `"individual"`, `"llc"`, `"trust"`, `"corp"`, `"unknown"` |
| `mailing_address` | string | null | Primary mailing address |
| `llc_agent` | string | null | LLC authorized agent name |
| `total_sales` | integer | 0 | Total cash purchases on record (all time) |
| `velocity_3m` | integer | 0 | Purchases in trailing 3 months |
| `median_price` | integer | null | Median purchase price across all sales |
| `property_type_mode` | string | null | Most common property type: `"single_family"`, `"multifamily"`, `"duplex"`, etc. |
| `zip_cluster_lat` | number | null | Latitude centroid of purchase footprint |
| `zip_cluster_lon` | number | null | Longitude centroid of purchase footprint |
| `zip_cluster_radius` | number | null | Radius in miles of buying area |
| `confidence` | number (0–1) | null | Skip-trace confidence score |
| `source` | string | `"cash-buyer-scraper"` | Pipeline identifier for auditing |

---

## Matching Algorithm

When a user clicks "Get Cash Buyers" for a property, the pool is queried using this logic:

1. **City match:** `pool.market LIKE '%propertyCity%'`
2. **State fallback:** If city yields < 5 new buyers → `pool.state = propertyState`
3. **Price filter:** Property price within buyer's `p25_price` – `p75_price` range (±30% tolerance)
4. **Contact filter:** Must have `phone` OR `email` (non-null, non-empty)
5. **Dedup:** Exclude buyers already dispensed to this user (any property)
6. **Rank:** Order by `velocity_12m DESC`, `recency_score DESC`
7. **Limit:** Take top 25

---

## Response Format

### Success (200)

```json
{
  "ok": true,
  "data": {
    "created": 45,
    "updated": 3,
    "errors": 2
  },
  "meta": {
    "total": 50,
    "errors": [
      { "index": 12, "error": "external_id is required and must be a string" },
      { "index": 37, "error": "name is required and must be a string" }
    ]
  }
}
```

### Error Responses

| Status | Condition | Body |
|--------|-----------|------|
| 401 | Missing or invalid Bearer token | `{"ok": false, "error": "Unauthorized — invalid or missing API key"}` |
| 400 | Empty payload | `{"ok": false, "error": "No buyers provided"}` |
| 400 | Exceeds batch limit | `{"ok": false, "error": "Maximum 500 buyers per request"}` |
| 500 | Server error | `{"ok": false, "error": "<message>"}` |

---

## Full Example

### Request

```bash
curl -X POST https://tranchi.ai/api/cash_buyers \
  -H "Authorization: Bearer sk_leads_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "buyers": [
      {
        "external_id": "mo-jackson-guardian-fund-llc",
        "name": "GUARDIAN FUND LLC",
        "entity_type": "llc",
        "phone": "+13145551234",
        "email": "deals@guardianfund.com",
        "llc_agent": "John Smith",
        "market": "Kansas City",
        "state": "MO",
        "total_sales": 47,
        "velocity_12m": 12,
        "velocity_3m": 4,
        "median_price": 165000,
        "p25_price": 95000,
        "p75_price": 280000,
        "property_type_mode": "single_family",
        "recency_score": 0.95,
        "activity_tier": "hot",
        "confidence": 0.88,
        "source": "cash-buyer-scraper"
      },
      {
        "external_id": "mo-stlouis-loren-ramsey",
        "name": "LOREN RAMSEY",
        "entity_type": "individual",
        "phone": "+13145559876",
        "email": null,
        "market": "St. Louis MO",
        "state": "MO",
        "total_sales": 5,
        "velocity_12m": 3,
        "velocity_3m": 1,
        "median_price": 92000,
        "p25_price": 65000,
        "p75_price": 140000,
        "property_type_mode": "single_family",
        "recency_score": 0.72,
        "activity_tier": "warm",
        "confidence": 0.91,
        "source": "cash-buyer-scraper"
      }
    ]
  }'
```

### Response

```json
{
  "ok": true,
  "data": { "created": 2, "updated": 0, "errors": 0 },
  "meta": { "total": 2, "errors": [] }
}
```

---

## Tips for Maximum Match Quality

1. **Always populate `market`** — it's the most important field for matching. Use the city/metro where the buyer's most recent transactions occurred. Without it, the buyer only matches on state-level fallback.

2. **Include skip-traced `phone` AND `email`** when available — buyers without either are completely invisible to users (filtered out at step 4).

3. **Higher `velocity_12m` and `recency_score` = shown first.** Prioritize pushing active buyers over dormant ones.

4. **Use consistent market naming** — always `"Kansas City"` not sometimes `"KC"` or `"KCMO"`. The match is a substring LIKE, so `"Kansas City"` will match properties in Kansas City.

5. **`p25_price` and `p75_price` define the buy box.** Without them, the buyer matches ALL price ranges (no price filtering applied). Setting them improves relevance.

6. **`external_id` enables safe re-runs.** If you re-push the same buyers with updated scores/contact info, they'll be updated in place rather than duplicated. Use a stable ID like `"{state}-{county}-{normalized_name}"`.

7. **Batch for efficiency** — send up to 500 buyers per request rather than one at a time. The endpoint processes them sequentially but returns a single summary.

---

## GET /api/cash_buyers/schema

Returns this documentation as a JSON object. No authentication required. Useful for programmatic discovery.

```bash
curl https://tranchi.ai/api/cash_buyers/schema
```

---

## GET /api/cash_buyers/stats

Returns current pool statistics. Requires Bearer auth.

```bash
curl https://tranchi.ai/api/cash_buyers/stats \
  -H "Authorization: Bearer sk_leads_abc123..."
```

### Response

```json
{
  "ok": true,
  "data": {
    "total": 4347,
    "hot": 312,
    "warm": 891,
    "cold": 2104,
    "dormant": 1040,
    "withPhone": 3201,
    "withEmail": 2876
  }
}
```

---

## Mapping from `cash-buyer-intel` to API Fields

For the `push-tranchi` command in the `cash-buyer-scraper` repo, here's how the local SQLite schema maps to the API fields:

| cash-buyer-intel field | API field | Source table |
|------------------------|-----------|--------------|
| `buyer_entities.entity_id` | `external_id` | `buyer_entities` |
| `buyer_entities.canonical_name` | `name` | `buyer_entities` |
| `buyer_entities.entity_type` | `entity_type` | `buyer_entities` |
| `buyer_entities.primary_mailing` | `mailing_address` | `buyer_entities` |
| `buyer_contacts.primary_phone` | `phone` | `buyer_contacts` |
| `buyer_contacts.primary_email` | `email` | `buyer_contacts` |
| `buyer_contacts.llc_authorized_agent` | `llc_agent` | `buyer_contacts` |
| `cash_sales.market` (most recent) | `market` | `cash_sales` |
| `cash_sales.state` (most recent) | `state` | `cash_sales` |
| `buyer_entities.total_sales` | `total_sales` | `buyer_entities` |
| `buyer_scores.velocity_12m` | `velocity_12m` | `buyer_scores` |
| `buyer_scores.velocity_3m` | `velocity_3m` | `buyer_scores` |
| `buyer_scores.median_purchase_price` | `median_price` | `buyer_scores` |
| `buyer_scores.p25_price` | `p25_price` | `buyer_scores` |
| `buyer_scores.p75_price` | `p75_price` | `buyer_scores` |
| `buyer_scores.property_type_mode` | `property_type_mode` | `buyer_scores` |
| `buyer_scores.zip_cluster_centroid_lat` | `zip_cluster_lat` | `buyer_scores` |
| `buyer_scores.zip_cluster_centroid_lon` | `zip_cluster_lon` | `buyer_scores` |
| `buyer_scores.zip_cluster_radius_miles` | `zip_cluster_radius` | `buyer_scores` |
| `buyer_scores.recency_score` | `recency_score` | `buyer_scores` |
| `buyer_scores.activity_tier` | `activity_tier` | `buyer_scores` |
| `buyer_contacts.confidence` | `confidence` | `buyer_contacts` |
| `"cash-buyer-scraper"` (hardcoded) | `source` | — |

---

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| `POST /api/cash_buyers` | 100 requests per 15 minutes |
| `GET /api/cash_buyers/stats` | 60 requests per 15 minutes |
| `GET /api/cash_buyers/schema` | No limit (public) |

If rate-limited, the server returns HTTP 429 with a `Retry-After` header.
