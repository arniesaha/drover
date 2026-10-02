#!/usr/bin/env bash
# Disposable local reproduction of the mandatory CI gate. Never use a live DSN.
set -euo pipefail
cd "$(dirname "$0")/.."
image='pgvector/pgvector:0.8.6-pg17-bookworm@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f'
container="drover-pgvector-test-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
cleanup() { docker rm -fv "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run --detach --name "$container" \
  -e POSTGRES_DB=drover -e POSTGRES_USER=drover \
  -e POSTGRES_PASSWORD=drover-ci-only \
  -p 127.0.0.1::5432 "$image" >/dev/null
ready=false
for _ in {1..60}; do
  if docker exec "$container" pg_isready -h 127.0.0.1 -U drover -d drover >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  docker logs "$container"
  exit 1
fi
port="$(docker port "$container" 5432/tcp)"
export DROVER_TEST_POSTGRES_DSN="postgresql://drover:drover-ci-only@${port}/drover"
uv run --python 3.11 --locked --extra dev pytest --require-pgvector -m pgvector \
  tests/test_memory_store.py tests/test_embeddings.py -v -s
uv run --python 3.11 --locked --extra dev pytest --require-pgvector \
  tests/test_ledger.py tests/test_memory_integrity.py -q
