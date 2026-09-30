# Issue #331 validation

Validated on Arnabs-Mac-Studio.local, 2026-09-29, against base commit
251c63e. The worktree-local environment uses Python 3.14.7 and DuckDB 1.5.5
from uv.lock. The historical /Volumes/M2 1/drover/.venv interpreter symlink
targets a missing /Users/arnabmac path, so setup used:

```sh
uv sync --extra dev
```

## Design and files

See [analytical admission](analytical-admission.md) for configuration, rationale,
client findings, and limitations. The thread-per-request HTTP server already
keeps control requests out of analytical executor queues. Nonblocking admission
adds a bound on analytical work without introducing another queue.

Changed implementation files:

- src/drover/server/db.py: enforce the configurable shared analytical thread ceiling.
- src/drover/server/analytics_maintenance.py: never bypass an occupied gate after repeated skips.
- src/drover/server/harness/usage_rollup.py: gate the harness usage rollup.
- src/drover/server/native_usage_rollup.py: use strict maintenance admission.
- src/drover/server/advisory/worker.py: gate scheduling as well as the sweep.
- src/drover/server/__main__.py: wire the harness rollup and day-summary backfill into the shared gate.
- src/drover/server/web/app.py: separate analytical/listing admission, fast saturation errors, and hosts-only render-busy handling.
- src/drover/server/analytics_boundary.py: include Retry-After on internal analytical HTTP 503s.

Test changes:

- tests/test_analytical_admission_http.py: hold an analytical request with an event;
  read actual temporary fleet data in under one second; run 25 deferred ticks of
  each rollup; reject analytical GET/POST/DELETE and fleet requests promptly;
  cover custom archived limits, hosts-only busy responses, slot release, and
  worker-dispatcher admission.
- tests/test_analytics_maintenance.py: strict admission through 25 skipped ticks,
  advisory scheduling deferral, and both usage workers avoiding store access.
- tests/test_db.py: prove role overrides cannot exceed the shared instance ceiling
  and explicit configuration can raise it.

Documentation changes: CHANGELOG.md, docs/analytical-recovery.md,
docs/analytical-admission.md, and this report.

## Successful test commands

Every test subprocess removes **all** inherited DROVER_DUCKDB_* variables,
including the new ceiling if present. Tests can still set explicit overrides
with monkeypatch.

Focused modules:

```sh
.venv/bin/python - <<'PY'
import os
import subprocess
env = {k: v for k, v in os.environ.items() if not k.startswith('DROVER_DUCKDB_')}
raise SystemExit(subprocess.call(['.venv/bin/python', '-m', 'pytest', 'tests/test_analytical_admission_http.py', 'tests/test_analytics_maintenance.py', 'tests/test_db.py', 'tests/test_usage_rollup.py', 'tests/test_native_usage_rollup.py', 'tests/test_analytical_recovery_http.py', 'tests/test_metrics.py', 'tests/test_cockpit_analytics.py', 'tests/test_advisory_jobs.py', 'tests/test_runtime_roles.py', '-n', '4', '-q'], env=env))
PY
```

Result: **422 passed, 2 skipped in 24.65s**, exit 0.

Full backend suite:

```sh
.venv/bin/python - <<'PY'
import os
import subprocess
env = {k: v for k, v in os.environ.items() if not k.startswith('DROVER_DUCKDB_')}
raise SystemExit(subprocess.call(['.venv/bin/python', '-m', 'pytest', 'tests', '-n', 'auto', '-q'], env=env))
PY
```

Result: **4276 passed, 68 skipped, 9 warnings in 47.75s**, exit 0. Warnings
concern existing multiprocessing fork and deprecated streamable HTTP client use.

Formatting and whitespace checks:

```sh
git diff --check
.venv/bin/python -m black --check src/drover/server/{analytics_maintenance.py,analytics_boundary.py,db.py,web/app.py,native_usage_rollup.py,advisory/worker.py,harness/usage_rollup.py,__main__.py} tests/test_analytical_admission_http.py tests/test_analytics_maintenance.py tests/test_db.py
.venv/bin/python -m isort --check-only src/drover/server/{analytics_maintenance.py,analytics_boundary.py,db.py,web/app.py,native_usage_rollup.py,advisory/worker.py,harness/usage_rollup.py,__main__.py} tests/test_analytical_admission_http.py tests/test_analytics_maintenance.py tests/test_db.py
```

All passed; Black reported 11 files unchanged.

## Earlier checks

The first six-module run caught six setup errors in the new HTTP fixture
(missing CockpitService constructor arguments); 62 tests passed. Fixed the
fixture. One expanded command named a nonexistent test_cockpit.py and ran no
tests; corrected it to test_cockpit_analytics.py.

Two serial expanded runs passed the new tests but failed the existing
test_check_status_times_out_once_and_refuses_parallel_reads timing assertion:
0.200263s and 0.200314s against a strict 0.2s limit. Totals were 409 passed / 1
failed and 421 passed / 2 skipped / 1 failed. The isolated test passed on an
archive of base 251c63e (1 passed in 2.08s); both final parallel runs above passed
it. No assertion or unrelated advisory timeout was changed.

## Risks and follow-ups

- Continuous foreground traffic can defer maintenance indefinitely. Maintenance
  already running is not preempted; this is admission control, not OS CPU isolation.
- A stuck leading fleet render still needs separate deadline/cancellation work.
  Excess requests fail fast and existing followers retain their bounded wait.
- iOS and the web fleet page ignore Retry-After. Clients were not rewritten.
- #224 remains unchanged: archived fleet sessions are already bounded; daemon
  listing and active-session pagination need a separate contract decision.
- #364's instance/RSS isolation remains separate; instance-wide memory settings
  and private snapshot thread budgets are unchanged.
- Tests simulate delay and saturation; no production load test or deployment was
  performed.
