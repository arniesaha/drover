# Rust harnessd Phase 2 gap audit

Baseline: `fc647b24` (origin/main, 2026-10-19). Scope follows the supplied
§5 Phase 2 list and the local planning copy `/tmp/harnessd-rust-port-plan.md`.
This table records the audit **before implementation**. All source paths below
are relative to `harnessd-rs/src/main.rs` unless noted. Completion evidence will
be added as milestones land. No live host is part of this work.

| Scope item | Baseline | Evidence / gap |
|---|---|---|
| CLI/config subset | partial | `Args`, `config`: flags accepted; local/tailscale URLs ignored, invalid TOML silently ignored, default config not read, no version flag. |
| Bearer auth | done | `App::authorized`; shared auth/top-level contract. Empty tokens fail closed. |
| healthz/capabilities shell+codex, interactive_auth:false | partial | `health`, `capabilities`: only shell+codex but no explicit interactive_auth flag. |
| Heartbeat consent, relay_required, retired 409 | missing | `heartbeat` discards every response. |
| SQLite store + migrations | partial | `init_db` only CREATE IF NOT EXISTS; no versioned migrations. |
| EventBus/StoreWriter/unified Pusher | partial | `emit` synchronously writes SQLite; pusher only checks 2xx; unconditional 5,000-row pruning can lose unacked history. No structured event subscription. |
| 403-pending, 413, tombstones, auth backoff | missing | `pusher` retries every failure at 250 ms; ignores acknowledgements. |
| #721 ownership quarantine | missing | No delivery capability header, receipt, exclusion from retries or retention protection. Python `structured/pusher.py` defines protocol. |
| Sessions CRUD, terminate, turns | partial | Routes exist; turn-in-flight/idempotency not fenced, child lifecycle incomplete. |
| interrupt, recover | missing | No routes. |
| permission 409/404 | missing | Fallback returns 404 even for live structured sessions. Python currently returns 400 for dormant Codex approvals; shared dormant behavior must be aligned. |
| Shell PTY + terminal WS forwarding | partial | PTY and plain relay channels exist (#704); no direct terminal WS, resize, reaping or durable terminal evidence. |
| Codex + git_changes evidence | partial | `run_codex_turn` launches exec; no resume, normalized events, native ID capture, git evidence; hardcoded fixture model in production catalog. |
| Worktree create/cleanup/startup reclaim | partial | `create_worktree`, `cleanup_worktree`; no ownership lock or startup reclaim; cleanup ignores failures. |
| Relay framed_responses_v1 + terminal channels | partial | Both transports forward GET /sessions only. Plain WS has terminal data/input; WSS always open_error. No shared in-process HTTP dispatch. |
| providers/usage stub | missing | Fallback 404. |
| model-catalog 404 stub | missing | Fabricated contract-model catalog instead of planned 404. |
| lifecycle/worktrees inventory | missing | Fallback 404. |
| fs/* folder picker | partial | List/create/exists exist; cursor is emitted but ignored, no allowed-root fence, filter or complete. |
| JSONL event + drain session-state import | partial | Import session/event rows exists; no Python exporter, atomic validation, drained-state normalization or worktree/lifecycle handoff. |
| Signed Linux + macOS CI builds | partial | CI builds musl + macOS; Developer ID/notarization workflow proven by #705/#709. No Linux signed artifact lane. |
| Shared contract subset green on both | partial | Rust-specific job selects 19 tests; generic Rust lane can skip all when binary absent. Selection includes fabricated catalog behavior and misses core gaps. |

## Milestones

1. Durable delivery, migrations and ownership quarantine.
2. Shared protocol/session/PTY/relay/worktree behavior and contracts.
3. Drain handoff, signed artifacts and pilot runbook; exact-head CI gate.

## Completion evidence

| Phase 2 scope item | Final status | Implementation and executable evidence |
|---|---|---|
| CLI/config subset | done | Version/help, flags, token precedence, default/explicit TOML, worktree path and advertised URLs in `main.rs::config` / heartbeat. Updater settings intentionally unused. |
| Bearer auth | done | Constant-time comparison and router middleware before extraction; health/options exceptions; shared authentication/unknown-route contracts. |
| healthz/capabilities | done | Versioned shell/Codex matrices, MIME attachment list, `interactive_auth:false`; `test_capabilities_are_accepted_by_hub_validator` validates both HTTP and registration envelopes with the actual hub validator. |
| Heartbeat | done | Consent epoch persisted atomically, direct `relay_required` refusal before Python background workers start, retired 409 stops Rust delivery; shared startup/consent contracts and Rust retired-host delivery regression. `target_version` deliberately ignored. |
| SQLite + migrations | done | WAL, private store, exclusive ownership lock, transactional schema versions 1–4; v0 upgrade contract preserves identities and next sequence. |
| EventBus / StoreWriter / unified Pusher | done | `event_bus.rs`: bounded 256-record ingress, one writer, commit-before-broadcast, transactional sequence allocation. `delivery.rs`: same durable queue for live delivery and restart reconciliation; retention prunes only acknowledged, non-quarantined tails. |
| 403-pending / 413 / tombstones / auth backoff | done | Strict acknowledgement validation, per-session 30→300 s pending backoff, independent session progress, global auth backoff, byte/count bounded pages, split-on-413, identity-preserving tombstones. Shared 413 contract and Rust delivery regressions cover pending/auth retention, singleton tombstone and original payload preservation. |
| #721 ownership quarantine | done | `quarantine-v1` request header; receipt and acknowledgement transaction; rejected originals remain unacknowledged, excluded from retries/pruning, including after restart. Rust unit test injects receipt-write failure; process tests cover 7/41-event missing/wrong-owner rejection and restart replay exclusion. |
| Sessions CRUD/terminate/turns/interrupt/recover | done | Host identity and durable-registry confirmation in create responses; serialized mutation gate, persisted session/turn identity, active-turn guard, process-group termination/reaping, native-ID recovery and durable dedup; shared session, recovery, concurrent recovery and Codex resume contracts. |
| permission 409/404 | done | Dormant live structured session returns 409, unknown/closed returns 404. Python dormant Codex response aligned from 400 to 409; Python daemon unit regression and shared contract. Hub direct and relay proxies return downstream status verbatim (`metrics.py::_proxy_harness_request`, `relay_client.py::_loopback_request`). |
| Shell PTY + terminal WS | done | Direct attach and relay channels share input/resize/interrupt/ping semantics, bounded scrollback/subscribers, durable terminal output and child reaping. Shared direct and relay terminal contracts. |
| Codex + git_changes evidence | done | Per-turn exec/resume, native ID capture, model/effort/image argv, normalized message categories, completion after stream drain; Git evidence unions committed, reverted, staged, unstaged and untracked paths. Shared native-resume and real-Git evidence/preservation contract. |
| Worktree create/cleanup/startup reclaim | done | Exclusive lock shared with Python; dedicated branches, fail-closed creation; startup reclaims known, closed, untouched owned trees. Restart-recoverable, edited, committed or foreign trees stay retained. Ownership checked before cleanup; shared real-Git preservation contract. |
| Relay | done | One WS/WSS implementation, same in-process router, 16-request concurrency gate, bounded terminal channels, negotiated `res_start` + body framing, response byte cap, ping handling and disconnect task cleanup. Shared mutation, framing, inventory and terminal contracts. |
| providers/usage + model-catalog stubs | done | Empty accounts with observed_at; catalog returns 404 instead of fixture data. Catalog and usage flags remain disabled. |
| lifecycle/worktrees | done | Inventory from SQLite worktree records, hub-compatible row shape, conservative retained/no-collection reasons; advertised lifecycle inventory v1. Imported lifecycle operation metadata is retained, never executed automatically. |
| fs/* | done | Descriptor-pinned no-symlink allowed roots, create/exists/complete, filtered bounded enumeration, identity/path/filter-bound cursor; shared folder picker and root/cursor fence contracts. Legacy unrestricted completion is intentionally narrowed to picker roots in Rust. |
| JSONL importer + drain session state | done | Authenticated Python drain gate and read-only stopped-registry exporter; atomic validated/dry-run import, idempotent events/sessions, preserved lifecycle/worktree/quarantine records and metadata, PID clearing/running→errored conversion. Real Python-export→Rust-import contract checks source DB unchanged. |
| Signed Linux/macOS CI | done | Both platform jobs build/test the release binary; pilot artifacts include SHA256 and GitHub OIDC-signed provenance. macOS pilot CI adds ad-hoc signing; release Developer ID/notarization remains in the proven existing workflow. No signing secrets on PRs. |
| Both implementations green | done, subject to exact-head CI | Identical `rust_phase2` selection for Python/Rust; missing Rust binary fails instead of skipping. Full Python contract coverage remains in the main test job. Rust-only migration/delivery checks run separately on both platform binaries. |
| GPU PC pilot | operator gate pending | Runbook covers isolated spare-port rehearsal, install, drain/stop/import/same-port cutover, health, rollback and uninterrupted 72-hour quantitative gates. No live host was accessed. |

### Later phases

Phase 3 retains the persistent Claude stream-json adapter, richer native-history
and native-transcript parsers, factory-observer launch rewriting and staging
credential handling. The existing basic Codex history parser is not full native
history parity. Per the plan, model catalogs/provider probes are Phase 4,
Agy/DeepSeek are Phase 5, interactive auth/advisory are Phase 6, and updater,
watchdog/activation and broader lifecycle automation remain later work. These
are capability-gated; none is silently claimed as enabled by the Rust pilot.
