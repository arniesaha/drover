#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

npx --yes @moona3k/excalidraw-export@0.2.1 \
  "$repo_root/docs/drover-architecture.excalidraw" \
  --output "$repo_root/docs/drover-architecture.png" \
  --scale 2
