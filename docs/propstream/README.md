# PropStream integration

Standard Operating Procedures for driving PropStream's web UI via [browser-harness](https://github.com/browser-use/browser-harness) and ingesting the results into `cash_buyer_intel`. Uses the account-holder's existing subscription — **$0 marginal API cost** unless skip-trace is explicitly enabled.

## SOPs

| SOP | Lead-list type | Status |
|---|---|---|
| [export-cash-buyers.md](export-cash-buyers.md) | Cash Buyer | ✅ validated 2026-05-18 (2,559 rows / ZIP 63116, $0) |
| [export-preforeclosure.md](export-preforeclosure.md) | Pre-Foreclosure / NOD | ✅ validated 2026-05-18 (15 rows / ZIP 63116, $0) |
| [export-vacant-equity-absentee.md](export-vacant-equity-absentee.md) | Vacant + High-Equity + Absentee (multi-filter) | ⚠️ partial 2026-05-18 — Vacant tile validated (1,437 rows); High-Equity tile state-sticky; Absentee Owner Location requires extra scrolling. Mechanics work; needs deterministic Clear-All-first sequence. |
| [cdp-list-id-capture.md](cdp-list-id-capture.md) | Virtual-scroll fix for the saved-list step | 📋 documented 2026-05-19 — replaces the sidebar-click step when the account has many lists. Uses CDP Network domain to capture the POST response and parse the new list's ID, then navigates directly to `/property/group/<id>`. |

## Authoring conventions

Each SOP mirrors the [browser-harness `domain-skills/<site>/<flow>.md`](https://github.com/browser-use/browser-harness) shape:

- **URL patterns** — every page the flow touches.
- **Pre-flight** — daemon attach, login, modal dismissal, no-credential-typing rule.
- **Flow** — numbered steps, each with the selector strategy and default coordinates for a 1251×817 viewport.
- **Output** — the file path and column coverage.
- **Gotchas** — what tripped the initial crack so the next replay doesn't pay the same tax.

## Ingest

The validated `cash-buyer-intel ingest-propstream <xlsx>` command parses the 75-column export into `cash_sales` (source='propstream') with a defensive `Total Open Loans = 0` check that drops any false-positive cash-buyer rows. See the [main README](../../README.md#commands).
