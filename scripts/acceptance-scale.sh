#!/usr/bin/env bash
# Run scale acceptance tests (A3, A4) against the 5M synthetic lake on this Mac Studio.
# Never run on GitHub Actions runners.
set -euo pipefail

if [[ "${CI:-}" == "true" || "${GITHUB_ACTIONS:-}" == "true" ]]; then
  echo "Scale acceptance is Studio-only; refusing CI execution" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

export DROVER_LAKE_SCALE="5m"
export DROVER_ACCEPTANCE_CACHE="${DROVER_ACCEPTANCE_CACHE:-$HOME/.drover-tmp/v2/acceptance-cache}"
mkdir -p "$DROVER_ACCEPTANCE_CACHE"

PYTEST_BIN="pytest"
if [[ -x ".venv/bin/pytest" ]]; then
  PYTEST_BIN=".venv/bin/pytest"
fi

exec "$PYTEST_BIN" tests/acceptance -m acceptance_scale -v "$@"
