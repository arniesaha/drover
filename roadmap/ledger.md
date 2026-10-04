# Recovery ledger

## 2026-10-03 — #508 restart-selected DuckLake switch

| Area | Evidence / boundary |
| --- | --- |
| Scope | Startup config selects legacy (unchanged) or DuckLake reads plus exporter; DuckLake config/runtime/serving/exporter failures stop startup without legacy fallback. |
| Safety | No production data, config, service, deployment, or Studio hub was accessed or changed. Rollback remains config-to-legacy plus restart; legacy files are not mutated. |

## 2026-10-01 — verified recovery state

| Area | State | Evidence / boundary |
| --- | --- | --- |
| Approved-decision baseline | Reconstructed, not claimed as original | `plans/2026-10-01-memory-integrity-ducklake.md`; source plan was absent from searched history/checkouts. |
| M2 capability integration | PR #488 open | Capability-driven web/iOS controls for #419/#420, plus bounded analytical-cache diagnostics. No runtime or production data change. |
| #466 production RSS | Open / unresolved | The diagnostics/cache change is included in #488, but does not close the production memory investigation. |
| Phase 3 derived memory | PR #489 open | PostgreSQL job/state and pgvector recovery; no Phase 4 cutover, regeneration, or destructive state change. |
| Phase 3 migration collision | No collision found | Branch is based on `origin/main` `97849f2`; merge-tree simulation succeeded. |
| Phase 3 backend validation | Verified | Disposable real pgvector run: 3606 passed, 15 skipped; targeted: 49 passed, 3 expected inverse skips. |
| M2 backend validation | Verified handoff evidence | 3621 passed, 65 skipped. |
| M2 Swift validation | Known non-blocking environment failures | 836 tests; 9 pre-existing SSH Keychain `-25308` capability-suite failures. GitHub-hosted macOS CI is the merge gate. |
| CI observation | Report-only watchers armed | `drover-pr-watch #488` and `drover-pr-watch #489`, one-minute trigger cadence, target `agent:main:telegram:default:direct:8695792249`. |

## Not authorized in this recovery

No Phase 4 DuckLake cutover, raw/control-data deletion, derived-data regeneration,
production migration, deployment, or harness daemon restart. Any future cutover
must start with a verified backup and complete the stated 25-session audit,
under-five-minute audit, search freshness, 24-hour soak, and restore verification.

## 2026-10-03 — #497 continuity follow-through (worktree facts only)

Worker: `harness-f5032b0f-837b-4769-8fcd-f39fb48d6281`; Codex session:
`01a100a1-3981-7f62-abd1-09ff28c3a4eb`. Worktree:
`~/.drover/worktrees/harness-f5032b0f-837b-4769-8fcd-f39fb48d6281`.
Follow-through base: `2b38b5120bd07304c533e8d4a4f02bc8b25eee3c`.
Worker ownership and implementation/commit-only authority are unchanged.

| Area | Verified fact | Remaining boundary |
| --- | --- | --- |
| PostgreSQL | Five actual PG tests passed on disposable PostgreSQL 17.11 (Homebrew), aarch64 macOS, using the repository's `pg_control_path`/`postgres_dsn` facility. Independent connections, acquisition/renewal races, epoch CAS, duplicate retries, stale acks, expiry re-admission, and pool/store recovery were exercised. | No production DSN was used. Server crash recovery, partitions, live hub load, other PG versions, and production release remain unverified. Missing test target/driver dependencies explicitly skip. |
| Epoch correction | A supplied lease epoch must match the locked row even after expiry; delayed old renewals cannot reacquire after a newer epoch expired. | Epoch omission remains explicit acquisition/reclamation and conflicts with a live owner. |
| OpenClaw owner protocol | Strict versioned tool schema, trusted run/owner/scope binding, fixed endpoint transport seam, explicit consume/ack; scope checked before mutation. Mock harness proves worker completion and both CI colors, ack/reconstruction dedupe, lost-delivery retention, and approval blocking. | No live plugin registration, parent messaging, SDK execute handler, authenticated runtime transport, or runtime policy/discovery was tested or installed. Hermes remains untested. |
| Core/integration evidence | `docs/openclaw-owner-protocol.md` records the absent callable OpenClaw tool in this session and absent Drover owner-tool registration, with official supported API references and an issue candidate. | Missing wiring does not establish a core defect. Actual parent runtime version, plugin diagnostics, tool discovery, and normal invocation trace are needed before that classification. No issue was filed. |
| Focused validation | 49 passed in 5.65s, zero skips, in the foreground. Black, isort, and diff checks passed. | This combines actual PG proof and explicitly mocked owner-tool proof; they are not interchangeable. |

Exact final foreground command:

```sh
.venv/bin/python -m pytest tests/test_factory_observer_continuity_postgres.py tests/test_openclaw_owner_protocol.py tests/test_factory_observer_continuity.py tests/test_factory_observer_bridge.py -q -rs
```

Integration/release owner remains
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
Watchers remain report-only. Approval handoff, integration review/publication,
deployment, and terminal release remain external. No second worker, push, PR,
merge, deployment, existing-service restart, cron/config/policy/credential
change, or OpenClaw RPC/shell messaging was performed. External orchestrator
artifacts were not updated.

## 2026-10-03 — #497 inactive product plugin follow-through

Same worker/worktree and ownership as above; base
`6d150b8a46dfc18046ed1e5958fcd48952898b8a`. No second worker.

| Area | Verified fact | Remaining boundary |
| --- | --- | --- |
| Product adapter | Standalone `plugins/openclaw-continuity-owner` package registers exactly the optional `drover_continuity_owner` tool via normal SDK factory registration. Trusted host config/context binds owner/run/scope and fixed authenticated Drover HTTP; strict exported protocol schemas. Default inactive, poll-only canary, explicit ack, no messaging/executor. Local npm pack succeeded with six product files. | Not installed, loaded, activated or published. Runtime discovery/schema normalization and parent messaging require integration-owner verification/authorization. |
| SDK evidence | Historical wrong-target proof: Studio OpenClaw 2026.3.13, commit `421effcf905b0956895166316c3fbe62baf6a22f`; its contract passed but did not prove current NAS compatibility. Superseded by the repair below. | This old checkout was not the NAS target. Current 2026.9.6 initially rejected the missing contracts.tools declaration; repaired below. No Gateway runtime proof. |
| Transport/canary proof | Real synthetic authenticated Drover HTTP, durable local store and reconstructed store: parent/owner read-only poll retains pending event, explicit lease/consume/ack then no duplicate delivery, unauthorized HTTP refused. Node tests cover scope/identity/config rejection, approval blocking, lost consume reply, redirect/cancellation and no autoack. | No live canary, sessions_send or runtime restart was performed. No new PG proof in this slice; prior actual PG evidence and remaining gaps above still apply. Hermes remains untested. |
| Core evidence correction | User confirms parent has first-class sessions_send despite child catalog absence. Earlier adapter-wiring gap now has inactive product code. | No core defect asserted/issue filed. Actual parent diagnostics/normal invocation traces needed if runtime fails. |
| Foreground validation | Python: 36 passed in 7.72s, no skips. Node: 15 passed, zero skips, 200.793583ms. Schema parity, Black, isort and diff checks passed. | SDK host mocked; authenticated HTTP/store actual; live OpenClaw runtime unverified. |

Exact foreground commands from the worktree root and package directory respectively:

```sh
.venv/bin/python -m pytest tests/test_openclaw_owner_plugin.py tests/test_openclaw_owner_protocol.py tests/test_factory_observer_continuity.py -q -rs
DROVER_TEST_OPENCLAW_SOURCE="$OPENCLAW_STUDIO_SOURCE" npm test
```

`plugins/openclaw-continuity-owner/README.md` specifies inactive packaging,
activation authorization, trusted schema and parent normal-tool/sessions_send
canary steps. Integration/release remains with
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
Implementation remains commit-only; integration review/publish and deployment
explicit approval remain external. No push/PR/merge/deploy/restart, host config,
cron, policy or credential change, live messaging, OpenClaw modification or
external ledger update was performed. Watchers remain report-only.

## 2026-10-03 — #497 current NAS 2026.9.6 compatibility repair

Same session/worktree; repair base
`77471886a27c42768a3e56ce4d7c7a3a4ce8fb58`. Earlier Studio 2026.3.13 evidence
was the wrong target; it is not NAS compatibility evidence.

| Area | Corrected/verified fact | Remaining boundary |
| --- | --- | --- |
| Current source | NAS `~/clawd/projects/openclaw`, read through existing SMB mount `${OPENCLAW_NAS_SOURCE}`: package 2026.9.6, HEAD `88027bc85c0a4eebbea49a2a5522faec71ecdc14`. SDK docs/source inspected first. | No OpenClaw source/config/policy/credential modification, install, activation or Gateway restart performed. |
| Concrete repair | Current registrar rejects absent manifest contracts.tools; now declares exactly drover_continuity_owner. Default definition object, V2 factory with required live host invocation guard before each HTTP request (including POST after awaited scope preflight), explicit shipped runtime entry, current Node engines, optional/non-replay-safe side-effect metadata and inactive/startup-lazy defaults. | Trusted run/owner/scope/transport bindings, strict protocol schemas, approval scopes, explicit ack, report-only watchers and no executor/messaging remain. No owner permission expansion. |
| Current-source proof | Test imports actual NAS source entry resolver/contract helpers and executes current registrar body with fake surrounding registry bookkeeping. Old manifest is rejected; repaired V2 tool registers; missing/retired host authority refuses calls. Revocation during GET preflight prevents POST. | Source/contract proof, not a live Gateway registry/lifecycle/discovery or messaging test. No new PG proof; prior PG facts/gaps unchanged. Hermes untested. |
| Supported owner installation | Current docs manage-plugins.md and install-source-plan.ts prove npm-pack archive form; README specifies exact owner command/path placeholder. | Explicit user authorization required before installation/config/activation/reload; not executed by worker. Parent sessions_send remains available first-class boundary and was not invoked. |

Final foreground commands/results (zero skips):

```sh
# Worktree root: 36 passed in 8.08s
.venv/bin/python -m pytest tests/test_openclaw_owner_plugin.py tests/test_openclaw_owner_protocol.py tests/test_factory_observer_continuity.py -q -rs
# plugins/openclaw-continuity-owner: 16 passed, 1300.021875ms
DROVER_TEST_OPENCLAW_NAS_SOURCE="$OPENCLAW_NAS_SOURCE" npm test
```

Schema parity, Black, isort and diff checks passed. Local `npm pack --ignore-scripts
--json` succeeded with six product files; no installation/publication performed. Parent
canary/authorization steps and exact current-source citations are in
`plugins/openclaw-continuity-owner/README.md`. Integration/release owner remains
`agent:coder:subagent:1998228b-5eb6-4b11-87a5-339a54576178`.
External orchestrator artifacts are untouched; commit-only, no second worker,
push/PR/merge/deploy or shell/RPC messaging.
