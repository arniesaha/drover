# Plan 7 — `nexus-server doctor` + Historical Seed + GCP Decommission

**Status:** Partial — non-destructive bits shipped; GCP teardown deferred to user
**Date:** 2026-05-09
**Spec:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §5.4, §8 step 5, §8 step 9

---

## Why this plan exists

The lakehouse should self-audit on a cadence so we know when something
silently rotted (row drift, missing partitions, parquet corruption).
That's `nexus-server doctor`.

Three other things show up in the spec but they touch state outside
the daemon:

1. **Historical seed.** Re-run the migration script against the
   ~3.9 GB of BigQuery CSV exports under `local_backup/` so the new
   `~/.nexus/` lakehouse contains 2026-01 → 2026-05 history. The
   script exists (`migrate_to_duckdb.py`) but writes to the legacy
   `nexus.duckdb` / `parquet/` paths in the repo root. We need to
   relocate to `scripts/` and re-target `~/.nexus/`.

2. **GCP decommission.** `terraform destroy` against the BQ dataset,
   Pub/Sub topic, and Cloud Functions. **Hard blocker:** destructive,
   touches shared infra, requires authenticated GCP credentials.
   Documented here as a checklist for the user to run by hand.

3. **Cloud-function code retire.** `src/nexus/cloud_function/` is
   dead code. Delete after the seed is verified.

---

## Module / file changes (non-destructive)

```
src/nexus/server/doctor.py       # audit_lakehouse() and supporting helpers
src/nexus/server/compact.py      # combine small parquet files within a partition
src/nexus/server/__main__.py     # `nexus-server doctor` and `nexus-server compact` subcommands

scripts/migrate_to_duckdb.py     # relocated; default paths now ~/.nexus/
docs/decommission-gcp.md         # checklist (user runs)

tests/test_doctor.py
tests/test_compact.py
```

---

## Tasks

### T1. `audit_lakehouse(parquet_dir, duckdb_path) → DoctorReport`

Returns a dict with:
  - `agent_events_count` per `(date, agent_id)` (read from view)
  - `spans_count` per `date`
  - per-host `incoming/.processed/` file counts (if dir exists)
  - drift > 1% rows flagged in a `warnings: list[str]`

Tests: synthetic parquet partitions; assert counts and drift logic.

### T2. `compact_parquet(parquet_dir, table, date)` — combine small
files within one date partition into one file.

Test: write 3 small files, compact, assert 1 file remains with all
rows preserved.

### T3. Wire into CLI: `nexus-server doctor`, `nexus-server compact`.

Tests: smoke that the subcommands run on a freshly bootstrapped
lakehouse without crashing.

### T4. Move `migrate_to_duckdb.py` → `scripts/migrate_to_duckdb.py`,
update default paths to `~/.nexus/`. Add a `--source-dir` flag so the
user can point at any CSV directory. **Do not run.**

### T5. `docs/decommission-gcp.md` — checklist with explicit
USER-ACTION-REQUIRED markers for:
  - `terraform plan -destroy` and review
  - `terraform destroy` (with mandatory pause for confirmation)
  - `git rm -r src/nexus/cloud_function`
  - `git rm scripts/sync_*.sh` (legacy shipper scripts)

---

## What I will NOT do in this plan

- Run `migrate_to_duckdb.py` against `local_backup/`. (3.9 GB write to
  ~/.nexus/; user should kick this off when ready.)
- Run `terraform destroy`. (Destructive, shared infrastructure, requires
  authenticated GCP credentials.)
- Delete `src/nexus/cloud_function/` or `scripts/sync_*.sh` until the
  user confirms the seed and decommission have happened.

---

## Acceptance for this plan

- `nexus-server doctor` runs against a freshly bootstrapped lakehouse
  and prints a report with no crashes.
- `nexus-server compact` is a no-op on an empty partition and combines
  multi-file partitions into one file.
- `scripts/migrate_to_duckdb.py` exists at the new path; its default
  paths point at `~/.nexus/`. Smoke test imports the module without
  invoking main.
- `docs/decommission-gcp.md` is committed.
- All 194 prior tests still pass.
