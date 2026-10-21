# Test suite audit — 2026-10

Scope: every tracked test module at `HEAD` was inventoried with
`find tests -type f \( -name 'test_*.py' -o -name 'test_*.sh' \)`, plus the
web and iOS test modules.  The pre-change inventory was 316 Python/shell
modules and 4,211 Python test functions; it is 315 and 4,210 after the one
deletion below.  The app inventory is 107 modules (3 web, 104 Swift), with
151 Swift and 11 web test functions.  The commands are deliberately recorded
so the inventory is reproducible rather than a hand-maintained claim.

## Tier and classification inventory

The entries below are exhaustive patterns over the recorded inventory.  A
module matches the first applicable row. `Fast` means the isolated PR tier;
`merge/nightly` means it is marked `slow_integration` and runs after main
merges and in the scheduled workflow; `job-only` means its platform's native
CI job owns its runtime.  Individual pytest timings come from
`pytest --durations=0`; the durable report field is a runtime tier because
wall time varies materially by PostgreSQL, DuckLake extension cache, and
simulator availability.

| Modules | Tier / runtime | Class | CI flake history (2026-10-06–20) |
| --- | --- | --- | --- |
| `tests/contract/test_*.py` | PR / fast | core-capability contract: public Python/Rust harness envelope | #752 only in `test_auth_flow_contract.py`; fixed in `02d9c4f5` |
| `tests/test_harness_*.py`, `tests/test_structured_*.py`, `tests/test_session_*.py`, `tests/test_relay_*.py`, `tests/test_tempo_relay.py` | PR / fast, except `test_relay_e2e.py`, `test_structured_e2e.py` merge/nightly | core-capability contract: lifecycle and relay | no repeat failure found |
| `tests/test_pairing_cli.py`, `tests/test_profile_credential_scope.py`, `tests/test_web_auth.py`, `tests/test_web_credentials.py`, `tests/test_web_pairing*.py`, `tests/test_harness_auth*.py`, `tests/test_mcp_auth.py`, `tests/test_staging_credentials.py` | PR / fast | core-capability contract: pairing, auth, credentials and host binding | no repeat failure found |
| `tests/test_control_*.py`, `tests/test_db.py`, `tests/test_schema.py`, `tests/test_lake_*.py`, `tests/test_ledger.py`, `tests/test_memory_*.py`, `tests/test_postgres_*.py`, `tests/test_factory_observer_continuity_postgres.py` | PR / fast where isolated; PostgreSQL serving and pgvector gates remain unconditional PR jobs; only the duplicate generic copies of `test_postgres_control_store.py` and `test_postgres_control_benchmark.py` are merge/nightly | core-capability contract: control DB, lake integrity and migrations | no repeat failure found |
| `tests/test_exporter_freshness.py`, `tests/test_control_exporter.py`, `tests/test_control_outbox*.py`, `tests/test_incoming_export.py`, `tests/test_lake_exporter.py`, `tests/test_shipper.py` | PR / fast | core-capability contract: exporter/outbox | no repeat failure found |
| `tests/test_work_queue_*.py`, `tests/test_hub_work_items.py`, `tests/test_job_streams.py`, `tests/test_redis_*.py`, `tests/test_decisions.py` | PR / fast | core-capability contract: work items and queue | no repeat failure found |
| `tests/shell/test_*.sh`, `tests/test_in_place_activation.py`, `tests/test_update_*.py`, `tests/test_rollback_cli.py`, `tests/test_service_units.py`, `tests/test_setup_*.py` | PR / fast | core-capability contract: installer and host update | no repeat failure found |
| `tests/test_check_public_release.py`, `tests/test_release_identity.py`, `tests/test_verify_release_candidate.py`, `tests/test_github_workflows.py`, `tests/test_makefile.py`, `tests/test_documented_cli_commands.py`, `tests/test_ios_*` | PR / fast | core-capability contract: public release audit | no repeat failure found |
| `tests/test_web_*.py`, `tests/test_cockpit_analytics.py`, `tests/test_analytics_ui_copy.py` | PR / fast | web-client contract, except deleted incidental copy assertion | no repeat failure found |
| `tests/acceptance/test_*.py`, `tests/backup/test_studio_backup.py`, `tests/operations/test_studio_pg_supervision.py`, `tests/test_db_self_heal.py`, `tests/test_postgres_control_benchmark.py`, `tests/test_relay_e2e.py`, `tests/test_server_e2e.py`, `tests/test_session_history_perf.py`, `tests/test_structured_e2e.py`, `tests/test_testflight_smoke_live_hub.py` | merge/nightly / service- or data-bound | slow-integration | no repeat failure found |
| All remaining `tests/test_*.py` modules not matched above | PR / fast | incidental implementation-detail, retained pending a capability owner rather than deleted speculatively | no repeat failure found |
| `apps/web/src/*.test.ts` | PR / fast | core-capability contract: web client | no repeat failure found |
| `apps/drover/DroverKit/Tests/**/*.swift`, `apps/drover/DroverTests/**/*.swift`, `apps/drover/DroverUITests/**/*.swift` | iOS CI job only | core-capability contract: iOS client | no repeat failure found |

The “remaining” row is intentionally retained, not silently removed.  These
modules are proposed consolidation candidates; each needs a named capability
owner before deletion.  That is safer than lowering coverage by treating
source proximity as a reason to remove a test.

The fast generic command excludes the two explicitly marked PostgreSQL
modules, but this does not remove their PR coverage: `postgres-integration`
still runs unconditionally on every PR and executes
`test_postgres_control_store.py` and `test_postgres_control_benchmark.py`.
`pgvector-memory-integration` also remains an unconditional PR release gate
for `test_memory_store.py`, `test_embeddings.py`, and the production-schema
drift check. `test_postgres_schema_migrations.py` and
`test_factory_observer_continuity_postgres.py` are not marked slow, so they
continue in the generic PR tier as before.

## Core capability map and gaps

| Capability | Protecting modules | Gap |
| --- | --- | --- |
| Harness contract (Python + Rust) | `tests/contract/test_*`, `tests/test_harness_*`, Rust contract workflow | None; shared black-box subset covers both implementations. |
| Session lifecycle / relay | `test_session_*`, `test_structured_*`, `test_relay_*`, contract session/relay modules | Native host crash/reconnect under a real multi-host relay is merge/nightly only. |
| Pairing / auth / credentials / host binding | pairing, auth, credential, profile and web pairing modules | No dedicated test proves Keychain isolation; contract fixture now stubs it. |
| Control DB / lake integrity / migrations | control, DB, lake, schema, Postgres and acceptance modules | Production-size Lake rebuild is scale/manual only. |
| Exporter / outbox | exporter, outbox, shipper and incoming-export modules | None. |
| Work items / queue | work-queue, hub work-items, job-stream and decision modules | None. |
| Installer / host update | shell install suites, activation, update and rollback modules | None. |
| Public release audit | release/public-check/workflow/Makefile modules | None. |
| Web client | Python web routes plus three Vitest modules | Browser accessibility journey is iOS-only; no browser E2E lane. |
| iOS | DroverKit, app and deterministic UI suites | CI-only by platform; no local Python substitute. |

## Flake investigation

* #750 / `test_codex_version_stdout_flood_is_bounded_while_stderr_also_floods`:
  the test asserted a one-second wall-clock deadline while competing with a
  subprocess scheduler and stderr flood.  The real contract is the bounded
  output classifier, not elapsed time.  It now gives the fixture enough time
  to be scheduled and asserts the deterministic `protocol_error` result.
* #752 / `test_provider_usage_keeps_plan_label_on_a_failing_anthropic_card`:
  the HTTP route returns an inventory placeholder before its asynchronous
  refresh publishes a credential result, so a one-shot request raced
  `usage_unavailable` against `not_configured`.  The fixture also left macOS
  Keychain ambient.  It now stubs `security` and waits for the intended
  no-credential observation.
* Codex catalog: this is the #750 test above; CI reported both `timeout` and
  `protocol_error` depending on which deadline won.

The two targets passed 50 consecutive combined Studio invocations with
`-p no:randomly`, followed by 13 enclosing-module tests.  No retries or flaky
decorators were added.

## Deletions and timing

Deleted: `tests/test_analytics_ui_copy.py` (one test).  It counted literal
presentation text in Swift source and protected neither a user flow nor a
public contract; UI behaviour remains covered by the web/iOS presentation
suites.

Before: CI run `39507045678` completed in 14m16s (2026-10-20).  The old PR
lane included all non-acceptance Python modules plus unconditional PostgreSQL
and pgvector jobs.  After: the PR Python command is xdist-enabled with two
workers and excludes only `slow_integration`; the PostgreSQL serving and
pgvector release gates remain unconditional on PRs, while the remaining
service/data-bound coverage runs on main and the nightly schedule.  A real post-change CI run requires a push,
which this audit deliberately does not perform; local target timing is 13
tests in 5.44s and 50 flake repetitions in about 74s.

## Commit sequence

* `02d9c4f5` — flake root-cause fixes.
* This report — classification snapshot.
* Follow-on change — marker, CI tier split, contributing guidance, and the
  incidental-test deletion.
