#!/usr/bin/env bash
# Smoke test — exercises init-db, dedup on an empty DB, probe, and the empty buyers query.
# Does NOT call ATTOM / BatchData / tranchi; those require live PP CLIs.
set -euo pipefail

cash-buyer-intel init-db --agent
cash-buyer-intel probe --agent | head -c 400; echo
cash-buyer-intel dedup --agent
cash-buyer-intel score --agent
cash-buyer-intel buyers --min-velocity 1 --limit 5 --agent
echo "smoke test passed"
