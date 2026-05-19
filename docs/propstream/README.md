# PropStream integration

Standard Operating Procedures for driving PropStream's web UI via [browser-harness](https://github.com/browser-use/browser-harness) and ingesting the results into `cash_buyer_intel`. Uses the account-holder's existing subscription — **$0 marginal API cost** unless skip-trace is explicitly enabled.

## SOPs

| SOP | Lead-list type | Status |
|---|---|---|
| [export-cash-buyers.md](export-cash-buyers.md) | Cash Buyer | ✅ validated 2026-05-18 (2,559 rows / ZIP 63116, $0) |

## Authoring conventions

Each SOP mirrors the [browser-harness `domain-skills/<site>/<flow>.md`](https://github.com/browser-use/browser-harness) shape:

- **URL patterns** — every page the flow touches.
- **Pre-flight** — daemon attach, login, modal dismissal, no-credential-typing rule.
- **Flow** — numbered steps, each with the selector strategy and default coordinates for a 1251×817 viewport.
- **Output** — the file path and column coverage.
- **Gotchas** — what tripped the initial crack so the next replay doesn't pay the same tax.

## Ingest

The validated `cash-buyer-intel ingest-propstream <xlsx>` command parses the 75-column export into `cash_sales` (source='propstream') with a defensive `Total Open Loans = 0` check that drops any false-positive cash-buyer rows. See the [main README](../../README.md#commands).
