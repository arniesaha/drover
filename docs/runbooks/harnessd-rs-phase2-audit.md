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
