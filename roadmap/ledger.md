# Recovery ledger

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
`/Users/arnab/.drover/worktrees/harness-f5032b0f-837b-4769-8fcd-f39fb48d6281`.
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
