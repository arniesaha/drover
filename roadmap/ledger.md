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
