# Work-queue guidance

Read this file before changing the work coordinator. It is navigation and a
scoped verification recipe, not a second policy definition.

## Canonical references

| Need | Source of truth |
| --- | --- |
| Goal/configuration schema and validation | `src/drover/server/work_queue/config.py` |
| HTTP and MCP work-operation surface | `docs/design/work-queue-phase-2.md` and `src/drover/server/work_queue/runtime.py` |
| GitHub pilot profile, provider facts, and owner handoff | `docs/operations/github-acceptance-pilot.md` |
| Work/verify/owner and GitHub/local-file stage boundaries | `docs/operations/verified-event-path-boundaries.md` |
| Queue product rules and planned/default policy | `docs/design/work-queue.md` |
| Protecting tests | `tests/test_work_queue_github_acceptance.py`, `tests/test_work_queue_execution.py`, `tests/test_work_queue_github.py`, and `tests/test_work_queue_local_candidates.py` |

## Ownership, stages, and non-goals

`status:queued`, `status:running`, `status:review`, and `status:blocked` are
workflow labels, not proof that an effect occurred. A completed work session
is not a verification result; a passed required check is not a behavioral
verdict; acceptance is not merge, deployment, publication, or native-owner
acknowledgement.

| Stage | Accountable component | May establish | Must not establish |
| --- | --- | --- |
| Work | Bound worker/session | implementation and terminal work completion | acceptance, close, merge, deploy, or publish |
| Verification | Separate verifier plus `WorkService` | verification request and, for the pilot, a provider-fact verdict | candidate-selected criteria/profile or local-file ownership |
| GitHub pilot acceptance | `GitHubProvider` and coordinator | a close request after current source, evidence, owner review, and required checks agree | producer independence or product quality from a success flag |
| Owner review | independently authorized owner | reviewed profile, criteria amendment, retained behavioral observations, and source/review trust | automatic installation or live acceptance proof |
| `local_file` native canary | native owner/continuity path | its signed deferred work -> verify -> acknowledge chain | GitHub pilot acceptance or a general GitHub successor scheduler |

The GitHub pilot is exact-issue opt-in. It does not upgrade historical accepted
records, run arbitrary report URLs or shell instructions, create a new
scheduler, or make GitHub work automatically follow the local-file native
chain. The real outcome states remain distinct: work finished, required checks
passed, owner reviewed behavior, accepted/closed, merged, deployed, and
acknowledged.

## Fresh-session, scoped verification

Use this only for focused work-queue checks. It provisions a disposable,
socket-only Postgres cluster owned by this shell. Prerequisites are Python 3.11+
available as `python3`, `uv`, and PostgreSQL `initdb`/`pg_ctl` on `PATH` (or set
`DROVER_TEST_PG_BINDIR` to their directory). `DROVER_TEST_PYTHON` and
`DROVER_TEST_UV` may name executables outside `PATH`; the recipe also finds the
standard `$HOME/.local/bin/uv` location. The test dependencies are resolved by
`uv` into the scratch root. Choose a new directory under `$HOME/scratch`; do not
use a production DSN, service, or database.

```sh
repo_root=$(git rev-parse --show-toplevel)
scratch_root=$(mktemp -d "${HOME}/scratch/drover-work-queue.XXXXXX")
printf 'scratch root: %s\n' "$scratch_root"
export UV_PROJECT_ENVIRONMENT="$scratch_root/venv"
export UV_CACHE_DIR="$scratch_root/uv-cache"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$scratch_root/tmp"
mkdir -p "$TMPDIR" "$scratch_root/socket"

python_bin=${DROVER_TEST_PYTHON:-$(command -v python3 || true)}
test -n "$python_bin" && test -x "$python_bin"
"$python_bin" -c 'import sys; sys.exit("Python 3.11+ required" if sys.version_info < (3, 11) else 0)'

uv_bin=${DROVER_TEST_UV:-}
if [ -z "$uv_bin" ]; then
  uv_bin=$(command -v uv 2>/dev/null || true)
fi
if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then
  uv_bin="$HOME/.local/bin/uv"
fi
test -n "$uv_bin" && test -x "$uv_bin"

if [ -n "${DROVER_TEST_PG_BINDIR:-}" ]; then
  pg_bin=$DROVER_TEST_PG_BINDIR
else
  pg_bin=$(dirname "$(command -v initdb)")
fi
test -x "$pg_bin/initdb" && test -x "$pg_bin/pg_ctl"

fixture_port=$("$python_bin" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
LC_ALL=C LANG=C "$pg_bin/initdb" -D "$scratch_root/pg" -A trust -U "$(id -un)" --no-sync -E UTF8
"$pg_bin/pg_ctl" -D "$scratch_root/pg" -l "$scratch_root/postgres.log" -w start -o "-k $scratch_root/socket -p $fixture_port -c listen_addresses='' -c fsync=off"
fixture_started=1
trap 'if [ "${fixture_started:-0}" = 1 ]; then "$pg_bin/pg_ctl" -D "$scratch_root/pg" -m fast -w stop; fi' EXIT INT TERM

cd "$repo_root"
export DROVER_TEST_POSTGRES_DSN="host=$scratch_root/socket port=$fixture_port dbname=postgres user=$(id -un)"
"$uv_bin" run --extra dev --extra postgres python -m pytest -p no:cacheprovider -q \
  tests/test_work_queue_github_acceptance.py \
  tests/test_work_queue_execution.py \
  tests/test_work_queue_github.py \
  tests/test_work_queue_policy.py \
  tests/test_work_queue_local_candidates.py \
  tests/test_github_work_item_events.py \
  --basetemp="$scratch_root/pytest"
"$uv_bin" run --extra dev --extra postgres python -m black --check \
  src/drover/server/work_queue/config.py \
  src/drover/server/work_queue/github.py \
  src/drover/server/work_queue/service.py \
  tests/test_work_queue_github_acceptance.py
git diff --check
```

The `trap` stops only the cluster started from `$scratch_root/pg`; it leaves the
fixture data and logs in scratch for inspection. Do not delete or reuse that
directory while preserving evidence. A passing fixture suite verifies the code
contract, not a live GitHub provider outcome or installed pilot.
