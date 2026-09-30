# Issue #331 / PR #436 follow-up

Validated on Arnabs-Mac-Studio.local on 2026-09-30 using the existing worktree
environment (Python 3.14.7, DuckDB 1.5.5).

## Rebase

Ran `git rebase 15b34d6`. The original fc5ca469 commit became 44318d8.
There was one conflict, in src/drover/server/web/app.py's internal analytical
dispatcher. Resolution retained both the foreground context from #331 and the
post-dispatch recovery check **only for non-2xx results** from #363. Continued
with `git add src/drover/server/web/app.py` and
`GIT_EDITOR=true git rebase --continue`.

Other overlapping files auto-merged. The resulting code preserves:

- `/healthz`: HTTP 200, `ok\n`, and X-Drover-Analytical health metadata.
- Public and internal successful responses: never replaced with recovery 503s.
- Analytical recovery: continues retrying with capped backoff; no retry-loop
  changes were made in this follow-up.

## Fixes and rationale

- Keep one heavy analytical slot by default: increasing it would reintroduce
  competing scans on the same CPU/memory budget. Heavy callers now wait up to
  one second for admission, then receive 503 with Retry-After.
- Exempt GET /metrics and POST /insights/{id}/acknowledge, dismiss, and check
  from that slot. Reserve independent nonblocking capacity for two scrapes and
  four small mutations. Apply the classification in the public handler, worker
  dispatcher, and forwarding client, including deployments with transport
  max_concurrent_requests=1. Small routes cannot queue behind a cockpit build
  or create unbounded admitted work. Existing scrape caching and insight scope
  query deadlines remain in place; this is not a new deadline for every DB call.
- Retry deferred day-summary passes after 30 seconds, returning to the usual
  900-second interval after admission. Keep shutdown waits interruptible.
- Remove harness UsageRollupWorker from analytical admission: it reads/writes
  the control store and parses transcript payloads, without analytical queries.

## Files

Implementation:

- src/drover/server/web/app.py
- src/drover/server/analytics_boundary.py
- src/drover/server/__main__.py
- src/drover/server/harness/usage_rollup.py

Tests:

- tests/test_analytical_admission_http.py: all-in-one and split scrape/mutation
  exemptions, independent capacity saturation, heavy-slot handoff, bounded
  timeout responses, and native-rollup/control-plane isolation.
- tests/test_analytics_maintenance.py: deterministic deferred day-summary ticks
  at 30 seconds, no store work during deferral, and 900-second cadence afterward.
- tests/test_usage_rollup.py: actual control-store usage rollup makes progress
  while foreground analytics is registered.

Documentation: CHANGELOG.md, docs/analytical-admission.md,
docs/issue-331-validation.md, and this report.

Existing #363 tests cover liveness, successful-response preservation, and
continued capped-backoff recovery after the rebase.

## Commands and results

Every test subprocess clears all inherited DROVER_DUCKDB_* variables.

Focused modules:

```sh
.venv/bin/python - <<'PY'
import os, subprocess
env = {k: v for k, v in os.environ.items() if not k.startswith('DROVER_DUCKDB_')}
raise SystemExit(subprocess.call(['.venv/bin/python','-m','pytest','tests/test_analytical_admission_http.py','tests/test_analytics_maintenance.py','tests/test_usage_rollup.py','tests/test_native_usage_rollup.py','tests/test_analytical_recovery_http.py','tests/test_db_self_heal.py','tests/test_runtime_roles.py','tests/test_metrics.py','tests/test_cockpit_analytics.py','tests/test_advisory_jobs.py','tests/test_db.py','tests/test_readiness.py','-n','4','-q'],env=env))
PY
```

Result: **483 passed, 2 skipped in 32.29s**, exit 0.

Full backend suite:

```sh
.venv/bin/python - <<'PY'
import os, subprocess
env = {k: v for k, v in os.environ.items() if not k.startswith('DROVER_DUCKDB_')}
raise SystemExit(subprocess.call(['.venv/bin/python','-m','pytest','tests','-n','auto','-q'],env=env))
PY
```

Result: **4299 passed, 68 skipped, 9 warnings in 48.75s**, exit 0.
Warnings concern existing multiprocessing fork and deprecated streamable HTTP
client use.

Formatting and whitespace:

```sh
git diff --check
.venv/bin/python -m black --check src/drover/server/{__main__.py,analytics_boundary.py,harness/usage_rollup.py,web/app.py} tests/test_{analytical_admission_http,analytics_maintenance,usage_rollup}.py
.venv/bin/python -m isort --check-only src/drover/server/{__main__.py,analytics_boundary.py,harness/usage_rollup.py,web/app.py} tests/test_{analytical_admission_http,analytics_maintenance,usage_rollup}.py
```

All passed; Black reported seven unchanged files.

The initial six-module run had 84 passed, 2 skipped, and one test-harness timeout:
the deliberately held request used a three-second client timeout while the test
now exercises five sequential one-second admission waits. Raised that helper's
socket timeout to ten seconds; individual latency assertions still enforce the
subsecond small-route and one-to-two-second heavy-route budgets. The subsequent
expanded focused run passed.

No push, PR operation, merge, release, or deployment was performed. Existing
limits remain: admission does not preempt running queries, persistent foreground
load may still defer maintenance, and #224 pagination / #364 instance isolation
are separate follow-ups.
