#!/bin/zsh
set -euo pipefail

label="com.drover.mac-ollama-embeddings"
host="127.0.0.1"
port="11435"
url="http://${host}:${port}/api/ps"

if ! /usr/bin/nc -z "${host}" "${port}" >/dev/null 2>&1; then
  exit 0
fi

payload="$(/usr/bin/curl -fsS --max-time 2 "${url}" 2>/dev/null || true)"
if [[ -z "${payload}" ]]; then
  exit 0
fi

if /usr/bin/python3 - "${payload}" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)

models = payload.get("models")
sys.exit(0 if isinstance(models, list) and not models else 1)
PY
then
  /bin/launchctl kill TERM "gui/$(/usr/bin/id -u)/${label}" >/dev/null 2>&1 || true
fi
