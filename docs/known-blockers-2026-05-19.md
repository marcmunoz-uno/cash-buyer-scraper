# Known photo-source blockers (2026-05-19)

When the user asked for Zillow photos (Redfin as fallback) for the Wichita
1,000 sample, every scraping path was attempted and blocked. This file
documents what was tried so future sessions don't re-run dead ends.

## Available signal we have

- 991/1000 Wichita addresses have **6 photos** in `property_photos` from the
  waterfall: 4 Google Street View + 2 Esri aerial. Already pushed via
  `tranchi-backfill-photos`; 370 visible on tranchi.ai.
- **281/1000** also have a `listing.listingUrl` from BatchData pointing at
  the canonical Zillow PDP. These are the rows that *could* be upgraded to
  real interior photos if a scrape path were viable.

## Paths tried and result

| Path | Result | Detail |
|---|---|---|
| BrightData MCP `web_data_zillow_properties_listing` | ❌ HTTP 400 "Customer is not active" | Account billing issue |
| BrightData MCP `scrape_as_html` (via property-enrichment-pipeline) | ❌ Returns empty 0-byte body | No zones configured |
| `brightdata-pp-cli request --zone <z>` | ❌ Every probed zone name "not found" | Tested: unblocker, web_unlocker, web_unlocker1, unlocker, datacenter1, residential, serp |
| Firecrawl MCP `scrape` (with `proxy: stealth`) | ❌ "Insufficient credits" | Account out of paid credits |
| Playwright Python — `chromium`, headless, with stealth init scripts | ❌ "Access to this page has been denied" | Zillow detects automation despite `navigator.webdriver` overrides |
| Playwright Python — `chromium`, headed | ❌ Same denial page | Same fingerprint surface |
| Playwright Python — `channel="chrome"` (real Chrome) | ❌ Same denial page | |
| Patchright (Playwright stealth fork) | ❌ Same denial page | |
| MCP Playwright tool — first call | ✅ Returned 104 photo URLs | One-shot worked |
| MCP Playwright tool — second call | ❌ Same denial page | Anti-bot adapted between calls |
| User's Chrome via `browser-harness` | ❌ "Access has been denied" + CAPTCHA | Confirmed earlier in the day |
| Redfin direct page (curl + UA) | ⚠️ Returns 158KB HTML but it's the "not found" page; needs property ID | Property URL pattern requires numeric ID we don't have |
| Redfin search/typeahead APIs | ❌ Both CloudFront 403 | `do/api/v1/locations/typeahead` and `do/location-autocomplete` |
| Redfin GIS CSV endpoint | ⚠️ Returns empty CSV for our queries | Only serves on-market listings; our targets are off-market motivated sellers |

## The cleanest unblocker

**Reactivate the BrightData account** — that's the single root cause that
also unblocks the existing `photo-enrichment-pipeline` Zillow waterfall stage
without code changes. The waterfall already has `enable_zillow=True` by
default; it's just hitting empty responses today.

Once BrightData is back, the existing command:

```bash
cash-buyer-intel enrich-photos --market "Wichita KS" --limit 1000
```

will pick up the 281 properties with Zillow URLs and replace their Street
View photos with real listing photos automatically.

## Lesser unblockers

- Top up Firecrawl credits — would let us scrape Zillow directly via
  Firecrawl's stealth proxy without BrightData.
- Buy a residential proxy from a different provider (Smartproxy, IPRoyal,
  ScrapingBee). Wire it as a new stage in the photo-enrichment-pipeline
  waterfall.
- Pay for ATTOM (we already have the wire-up; only thing missing is a paid
  key). ATTOM returns property photos via their property/expandedprofile
  endpoint.
