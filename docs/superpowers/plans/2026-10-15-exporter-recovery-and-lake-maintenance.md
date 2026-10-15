# Exporter Recovery and Lake Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the managed hub after exhausted exporter restarts without a crash loop, and give operators a safe, measured DuckLake compaction command.

**Architecture:** Persist rolling restart-exit attempts in the PostgreSQL control store, so the exporter lifecycle can request a status-75 server exit at most three times per hour and otherwise publish an unhealthy guarded state.  Put compaction in a standalone catalog-admin CLI that takes the existing drained mutation fence, leaving runtime export and privileges unchanged.

**Tech Stack:** Python 3.11, psycopg, DuckDB/DuckLake 1.5.5, Click, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-exporter-recovery-and-lake-maintenance-design.md`

## Global Constraints

- Use DuckLake/DuckDB 1.5.5 for all integration measurements.
- Never add catalog schema `CREATE` to the exporter role.
- Maintenance is explicit and fenced; it is never an exporter startup action.
- Live lake and service inspection remain read-only.
- Measurements use disposable catalog databases, roles, and lake roots.

## Review Focus

- A fourth server process in one hour must remain up and report crash-loop guarding.
- A successful maintenance run must not run snapshot cleanup if any merge fails.
- The maintenance command must reject a live exporter holding the mutation fence.
- Dry-run must not attach to or mutate the catalog.
- Mutable export tables must remain out of inlining on DuckLake 1.5.5.

### Task 1: Durable bounded exporter escalation

**Files:**
- Modify: `src/drover/server/postgres_schema.py`, `src/drover/server/lake/lifecycle.py`, `src/drover/server/__main__.py`
- Test: `tests/test_exporter_freshness.py`, `tests/test_control_migration.py`

**Interfaces:**
- Produces: `ExporterLifecycle.server_exit_code() -> int | None` and guarded health fields.

- [ ] Write a failing lifecycle test for three allowed requests and a fourth guarded request.
- [ ] Run the focused test and confirm the missing durable guard behavior fails.
- [ ] Add the control-store migration and lifecycle guard; return exit code 75 only for an allowed request.
- [ ] Run focused lifecycle and migration tests.

### Task 2: Fenced lake maintenance CLI

**Files:**
- Create: `src/drover/server/lake/maintenance.py`
- Modify: `src/drover/server/lake/cli.py`
- Test: `tests/test_lake_exporter.py`

**Interfaces:**
- Produces: `maintain_lake(spec: LakeSpec, tables: tuple[str, ...]) -> dict` and `lake maintain`.

- [ ] Write a failing disposable-PG integration test that asserts fenced merge, snapshot expiry, cleanup ordering, and dry-run isolation.
- [ ] Run it with DuckLake 1.5.5 and confirm it fails because the command is absent.
- [ ] Implement the command with `drained_mutation`, per-table metrics, and fail-closed ordering.
- [ ] Run focused CLI/integration tests.

### Task 3: Measured fixture and rollout documentation

**Files:**
- Modify: `tests/test_lake_exporter.py`, `docs/operations/lake-exporter.md`

- [ ] Add a manually selected scale fixture creating thousands of small files/snapshots and recording merge time and peak RSS before/after maintenance.
- [ ] Run the fixture in foreground with disposable PostgreSQL and temp lake storage; record the result in the operator documentation.
- [ ] Document first-run and weekly scheduling steps without executing them on the Studio.
- [ ] Run the focused suite and formatting checks.
