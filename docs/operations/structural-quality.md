# Structural quality gates

Issue #1024, Slice B. These are two executable gates for the work
coordinator, plus the exact limits of what each one proves.

- Dependency guard: [`scripts/quality/boundaries.py`](../../scripts/quality/boundaries.py)
  (stdlib `ast`, no imports of the code under test).
- Response contract: [`scripts/quality/work_contract.py`](../../scripts/quality/work_contract.py)
  (strict pydantic models, the project's existing validation dependency).
- Tests and negative controls: [`tests/test_quality_boundaries.py`](../../tests/test_quality_boundaries.py).

## Canonical commands

From the worktree root, after [preflight](dev-preflight.md):

```sh
uv run --locked --extra dev python -I scripts/quality/boundaries.py --ratchet
sh scripts/quality/preflight.sh --out ~/scratch/<run> --expect-base <base-sha> \
  --then '{python}' -m pytest -q --basetemp ~/scratch/<run>/pytest \
  tests/test_quality_boundaries.py
```

The guard prints one JSON report. It exits 0 on pass, 1 on a violation, and 4
on a harness failure: a root that matches no module, or a source that doesn't
parse. `--eager-only` checks the import-time graph alone. Running with no flag
applies the full policy without exemptions, and today it fails on the known
violation below. `--closure` prints the reachable set for diagnosis.

## Boundary 1: work coordinator must not acquire analytics or enrichment

**Source of the rule.** In [Process Boundaries](../architecture.md#process-boundaries),
the work coordinator runs in the control API process. It owns work intents,
bounded provider reads and issue-close decisions. The analytics role owns
ingest, the lake, analytical routes, MCP and optional OTLP. Summaries,
embeddings and advisory are derived-memory enrichment.

**Roots:** every module under `drover.server.work_queue`.

**Forbidden module prefixes:**
- the lake: `drover.server.lake`;
- the analytics role: `drover.server.analytics_boundary`,
  `drover.server.analytics_maintenance`, `drover.server.cockpit`;
- the MCP read surface: `drover.server.mcp.tools` and `drover.server.mcp.server`;
- enrichment: `drover.server.summarizer`, `drover.server.embeddings`,
  `drover.server.advisory`, `drover.server.briefs`;
- optional OTLP: `drover.server.otlp`;
- the route layer: `drover.server.web.app`;
- the enrichment and telemetry SDKs: `anthropic` and `opentelemetry`.

`pyarrow` is deliberately not forbidden. It is a declared base dependency of
the control outbox itself, not optional enrichment.

**What the graph includes:**
- absolute imports, relative imports and `from pkg import submodule`;
- implicit parent-package `__init__` execution;
- literal `importlib.import_module("...")` calls.

`if TYPE_CHECKING:` blocks are excluded because they never run.

**Policy:**
- Every module's import-time edges count.
- The roots' *own* function-scoped (lazy) imports also count, because a root
  acquires a dependency when its own deferred import runs.
- The report gives the shortest path, with line numbers and `(lazy)` markers.

### Known violation (characterized, not redesigned)

The default policy fails on exactly one forbidden target:

```text
drover.server.work_queue.runtime:454 (lazy) -> drover.server.mcp.tools
```

`WorkQueueRuntime.quota()` imports `drover_provider_quota` from the MCP tool
module, and `work_queue.service.route()` imports `_build_routing_hint` from it.
Importing `drover.server.mcp.tools` then executes `drover.server.summarizer`,
and `drover.server.cockpit` via `project_activity`, at import time. So queue
admission in the control process loads analytics and enrichment code.

Fixing it means moving the quota/routing helpers out of `mcp.tools`, which
touches Slice A runtime sources and is outside this slice. The two exact
`(importer, imported)` edges are listed in `Boundary.known`. `--ratchet`
removes only those two edges, and it fails in two cases:
- any other path to a forbidden module appears;
- a known edge stops existing, so the entry must be deleted once the code is fixed.

### Negative controls (all must fail for the stated reason)

Each seed below lives in a throwaway tree, and the guard must report the
seeded forbidden prefix with a path starting at the seeded root module:
- absolute, `from` and relative imports, including `from .. import lake`;
- a root-lazy import and an `importlib` literal;
- a transitive eager import;
- a parent package `__init__`;
- a lazy third-party SDK import.

A copy of the real `src/drover` with `import drover.server.lake.serving`
appended to `work_queue/provider.py` must fail `--ratchet`. A similarly named
module (`drover.server.lakehouse`) and a `TYPE_CHECKING`-only import must pass.

### Limitations

- This is static and module-level. A lazy import inside a *shared* module, for
  example `control_outbox` → `memory_identity` → `lake`, is not attributed to
  the roots, because that needs a call graph. A test pins this as out of scope.
  Today such paths do exist from `work_queue` through `control_outbox` and
  `harness.registry`, so the gate is not a runtime guarantee that analytics
  code never loads.
- Dynamic imports with non-literal names, `__import__`, plugins and
  `sys.modules` manipulation are invisible to it.
- It covers one boundary. Other process boundaries need their own entries.

## Boundary 2: `/work/update` client-facing contract

**Path under test.** The real web handler `POST /work/update` calls
`runtime.dispatch`, which calls `WorkService.call` and then
`WorkService.update`.

**What `validate_update_response` checks:**
- **Error envelope:** strict, unknown fields forbidden. The status must be in
  {400, 401, 403, 409, 503}. The error string must be one the server actually
  emits for that status. A `reason` (at most 200 characters) is required
  exactly for 403 and 409.
- **Success semantics:** `complete` returns a verifier link (64-hex `run_key`,
  `run_role` of `verify`) or `deferred`/`marker_only`. `verified`, `failed`
  and `progress` return their own outcome or `deferred`. Wrong-type values are
  rejected, with no coercion.

**PG-free tier.** It runs anywhere: a real HTTP server, real credential
authentication and a real `WorkService`. The store is a tripwire, so each
rejection also proves that no durable state was reached. Cases:

| Request | Status |
|---|---|
| missing or restricted credential | 401 |
| non-object body | 400 |
| unknown field, including a spoofed `actor_principal` | 400 |
| missing `run_key` | 400 |
| `number` as a string or a bool | 400 |
| goal outside the workspace | 403 |
| item outside the goal | 403 |
| shadow mode | 403 |
| provider unavailable | 409 |
| control unavailable | 503 |

There are also two seeded bad service responses: a wrong-typed outcome, and
`verified` returned for `failed`. Both are emitted with 200 and rejected by the
contract.

**PG tier.** It runs only with an owned `DROVER_TEST_POSTGRES_DSN` and never
the `/tmp` auto-cluster. It reuses the Slice A pilot fixtures:
- **Opted-in GitHub pilot:**
  - These return 400 and nothing closes: a wrong-type `evidence_key`, a missing
    `evidence_key`, caller booleans without provider evidence, and a
    wrong-repository source.
  - Nonexistent evidence returns 409 `item provider unavailable`.
  - Provider evidence returns `verified` and closes exactly once.
- **Non-pilot caller-flag path** (kept distinct): `complete` returns a typed
  verifier link. A string `"true"` for `behavioral_passed` returns 400. A real
  `True` with rerun checks returns `verified`.

### Contract limitations

- The models validate response shape and outcome semantics. They don't replace
  server-side validation, and they aren't static typing of `work_queue`. The
  repo has no configured type checker, and none was added.
- `local_file` signed-provenance completion is not covered here. The non-pilot
  case uses the GitHub-sourced fixture.
- The PG tier is skipped without an owned DSN; a skip is not a pass.

## CI wiring (parent-owned; required for Slice B acceptance)

Two edits to `.github/workflows/ci.yml`.

1. In the main Python job, after `Check formatting with Black` and before
   `Run tests with pytest` (same `if:` guard; the guard is stdlib-only):

   ```yaml
       - name: Architectural boundary ratchet
         if: steps.scope.outputs.python == 'true'
         run: python -I scripts/quality/boundaries.py --ratchet
   ```

   The existing `pytest tests/ -n 2 -m 'not slow_integration'` step already
   collects `tests/test_quality_boundaries.py`. That job sets no
   `DROVER_TEST_POSTGRES_DSN`, so the PG tier is skipped there.

2. In `postgres-integration`, add `tests/test_quality_boundaries.py` to the
   `Run mandatory PostgreSQL integration contracts` pytest list. That job sets
   `DROVER_TEST_POSTGRES_DSN` from its service, so the PG tier runs. That job
   only runs when `integration-changes` reports integration paths. Confirm in
   the job log that the two PG-tier tests passed rather than being skipped.
