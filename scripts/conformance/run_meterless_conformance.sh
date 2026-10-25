#!/usr/bin/env bash
# Run Meterless's own Markovian conformance suite against Casefile's Python engine.
#   bash scripts/conformance/run_meterless_conformance.sh
# Needs Node 18+ (npx) and network access to GitHub and npm the first time.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${MARKOVIAN_SPEC_DIR:-$HERE/.meterless}"
if [ ! -f "$WORK/engines/markovian/conformance/runner.ts" ]; then
  git clone --depth 1 --filter=blob:none --sparse https://github.com/meterless/meterless "$WORK"
  git -C "$WORK" sparse-checkout set engines/markovian/conformance
fi
cd "$WORK/engines/markovian/conformance"
MARKOVIAN_IMPL="$HERE/markovian_bridge.ts" npx -y tsx@4.19.2 runner.ts
