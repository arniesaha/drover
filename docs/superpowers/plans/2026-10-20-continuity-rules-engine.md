# Continuity Rules Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make hub-owned work-item continuity event-driven, conservatively automated, and opt-in for queue execution.

**Architecture:** Migration 26 extends the existing work-item projection with policy, dependency, rule-action, and cursor state.  A rule worker consumes persisted facts and a bounded sweep, recording each idempotent result through the existing timeline event outbox.  Existing `WorkService.launch` remains the only launch implementation; execution reconciler calls it only after opt-in policy checks.

**Tech Stack:** Python 3.11, PostgreSQL control plane, Pydantic config, requests, pytest.

**Spec:** `docs/superpowers/specs/2026-10-20-continuity-rules-engine-design.md`

## Global Constraints

- Migration 26 is additive only; existing work items default to no auto-actions.
- Worker sessions publish branches; the hub never executes `git push`.
- GitHub writes use the configured provider credential and degrade to `orchestrator_wake` without one.
- Shadow mode and every execution opt-in default off.
- Run only focused touched test modules in the foreground; GitHub is mocked.

## Review Focus

- A delayed green fact for an earlier head must never merge the current PR.
- Duplicate facts and a sweep must not repeat an external provider mutation.
- Remote branch verification must distinguish absent branches from a different SHA.
- A gate must always override `auto_merge`.
- An execution denial must not create a launch intent or session.

### Task 1: Add durable policy, dependency, and action state

**Files:**
- Modify: `src/drover/server/postgres_schema.py`, `src/drover/server/work_queue/store.py`, `src/drover/server/work_queue/config.py`, `src/drover/server/work_queue/runtime.py`, `src/drover/server/web/app.py`
- Modify: `tests/test_hub_work_items.py`, `tests/test_postgres_schema_migrations.py`, `tests/test_lake_rollout.py`

**Interfaces:**
- Produces: `WorkStore.claim_rule_action(item_id, rule, head_sha, dry_run)` and policy/dependency patch support.

- [ ] **Step 1: Write failing tests** for default-off policy fields, dependency unblocking, and duplicate action claims.
- [ ] **Step 2: Run focused tests** and confirm the new behavior fails.
- [ ] **Step 3: Add migration 26 and store/config/API support** using only additive DDL and stable timeline facts.
- [ ] **Step 4: Run focused tests** and confirm they pass.
- [ ] **Step 5: Commit** the tested persistence layer.

### Task 2: Add GitHub rule operations and the durable rule worker

**Files:**
- Create: `src/drover/server/work_queue/rules.py`, `tests/test_work_item_rules.py`
- Modify: `src/drover/server/work_queue/github.py`, `src/drover/server/work_queue/runtime.py`, `src/drover/server/metrics.py`, `src/drover/server/web/app.py`

**Interfaces:**
- Consumes: Task 1 policy/action/dependency store methods.
- Produces: `WorkItemRuleWorker.process_once()` and mocked `GitHubProvider` branch/PR write methods.

- [ ] **Step 1: Write failing tests** for publish verification/wakes, stale green, gates, duplicate events, dry-run, merge/unblock, and missing credential.
- [ ] **Step 2: Run the rule test module** and confirm it fails because the worker is absent.
- [ ] **Step 3: Implement provider reads/writes and rule evaluation** with SHA-bound actions and short stable reasons.
- [ ] **Step 4: Start the worker after GitHub fact processing and expose advisory health/metrics.**
- [ ] **Step 5: Run focused rule/hub/GitHub tests** and commit.

### Task 3: Turn reconciler candidates into opt-in launches

**Files:**
- Modify: `src/drover/server/work_queue/reconciler.py`, `src/drover/server/work_queue/config.py`, `src/drover/server/work_queue/runtime.py`, `tests/test_work_queue_reconciler.py`, `tests/test_work_queue_execution.py`

**Interfaces:**
- Consumes: `WorkService.launch` and per-goal/global execution config.
- Produces: one execution path with cap, budget, and kill-switch denials.

- [ ] **Step 1: Write failing tests** for default shadow behavior, successful opt-in launch, each concurrency cap, budget denial, and kill switch.
- [ ] **Step 2: Run focused queue tests** and confirm they fail.
- [ ] **Step 3: Implement execution mode through `WorkService.launch` only**, retaining candidate reporting.
- [ ] **Step 4: Run focused queue tests** and commit.

### Task 4: Document the operational contract and verify migrations

**Files:**
- Modify: `docs/design/work-items-hub-owned-continuity.md`, `site/src/content/docs/api.mdx`
- Modify: focused tests touched above.

**Interfaces:**
- Consumes: final config/API names from Tasks 1-3.

- [ ] **Step 1: Document rule table, policy fields, config, kill switch, and reduced cron role.**
- [ ] **Step 2: Run the exact focused test set**, including migration and rollout tests.
- [ ] **Step 3: Review the diff for additive migration safety and commit.**
