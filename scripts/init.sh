#!/usr/bin/env bash
# One-shot bootstrap: install the package and create the local buyers DB.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v pip >/dev/null 2>&1; then
  echo "pip not found — install Python 3.10+ first" >&2
  exit 1
fi

pip install -e .
cash-buyer-intel init-db
cash-buyer-intel probe
