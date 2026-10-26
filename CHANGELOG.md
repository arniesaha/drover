# Changelog

All notable changes to Drover will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.43] - 2026-10-26

### Fixed

- Work queue automatic execution now carries the scoped goal ID into candidate actions and logs/skips an individual malformed candidate instead of terminating the execution worker (#845, #846).

## [0.6.42] - 2026-10-26

### Added

- Continuity owner plugin 0.1.6: wakes live subagent owners through OpenClaw's `requestLiveSubagentOwnerWake` (openclaw fork PR #16). A gateway without that API falls back to the plain hook wake, and main-session owners are unchanged (#818, #835).

## [0.6.41] - 2026-10-25

### Fixed

- Work queue: a worker that declared `done`, or that the idle-undeclared sweep reported, no longer holds a goal/workspace concurrency slot (the same item still cannot be launched twice; a failed lookup holds all slots) (#831)
- `/work/launch` and the other work operations now return and log the real denial `reason` (capacity, launch intent uncertain, systemic review, provider unavailable, coordinator required) instead of a generic 409; unexpected errors are logged with a traceback and return only the exception class (#831)
- MCP `drover_work_launch` reports the hub status and reason; docs explain that a `credential:<id>` needs a `principal_roles` coordinator mapping (the legacy operator bearer is mapped separately) (#831)

## [0.6.40] - 2026-10-25

### Fixed

- Continuity owner plugin 0.1.5: without a fixed `runId`, only runs authorized to `ownerSessionKey` are accepted (no longer `ownerPrincipal`); listed run ids are validated, the 250-run truncation warns, and finished runs leave the wake map. `poll_only` is valid without a `runId`.
- `GET /harness/factory-observer/continuity?owner_id=` is limited to the owner principal or a coordinator/operator; `authority_scope`/`offset` with `run_id` is a 400.

## [0.6.39] - 2026-10-25

### Added

- Continuity owner plugin 0.1.4 binds the owner SESSION: `GET /harness/factory-observer/continuity?owner_id=` lists an owner's actionable runs (optional `authority_scope`, `limit`, `offset`), the plugin wakes for any run whose `authorized_owner_id` equals `ownerSessionKey`, and tool calls may carry `run_id`. The wake uses heartbeat `source: "hook"` so OpenClaw does not drop it.

## [0.6.38] - 2026-10-25

### Fixed

- Work-coordinator workers' `Drover-State: done|blocked|needs-approval` declarations now reach the continuity ledger: the persisted event envelope keeps the message in `text` beside an empty `payload`, which the projection previously missed. The idle sweep also reconciles any declaration that ingest missed (#816).

## [0.6.37] - 2026-10-25

### Fixed

- A Work-coordinator run authorized to a canonical agent session key can now be leased, woken, consumed and acknowledged: only the coordinator/operator principal may act, and only for exactly the run's bound owner. Owner plugin 0.1.2/0.1.3 also accept the hub-projected worker kinds (`worker_started`, `worker_done`, `worker_blocked`, `worker_needs_approval`) in its inbox reply, which previously failed the whole poll (#813, #814).

## [0.6.36] - 2026-10-24

### Fixed

- A bound Work-coordinator worker that finishes and idles at `awaiting: input` without a `Drover-State` line now wakes its owner after `work_queue.idle_undeclared_seconds` (default 300) with its final output attached as an `idle_undeclared` continuity fact; the worker brief now says to put the result before the state line (#810).

## [0.6.35] - 2026-10-24

### Fixed

- Continuity re-decisions reuse their already-fenced wake instead of failing on divergent delivery payloads, and WorkQueue refreshes capacity before routing a launch so newly available quota is honored (#806, #807).


## [0.6.34] - 2026-10-24

### Added

- Work-coordinator implementation and integration launches now bind one canonical OpenClaw owner, record durable worker-start facts, and project explicit done, blocked, and needs-approval states into the continuity ledger (#804).

### Fixed

- Packaged the continuity-owner plugin with static tool-authoring metadata and schemas, and route durable owner wakes to the exact fenced OpenClaw session while preserving inert deployment and poll-only modes (#802, #803).

## [0.6.33] - 2026-10-23

### Fixed

- Rebuilt corrupt persisted legacy harness session-sequence ART indexes into the versioned v2 form, preserving recovery from prior state; the unified production-backup clone rehearsal validated the migration path (#800).

## [0.6.32] - 2026-10-23

### Fixed

- Replaced the unsafe DuckDB reconciliation-pending ART index with an immutable reconciliation cursor index, avoiding failures when acknowledgement updates change pending state (#798).

## [0.6.31] - 2026-10-23

### Fixed

- Release preflight now conservatively recognizes an inline numeric `CHECK` on the same newly added nullable control column, while rejecting quoted non-ASCII identifiers and other non-equivalent forms (#796).

### Upgrade notes

- Additive control migration 30 includes `notification_hour`, a nullable integer with literal `0..23` bounds. No DuckLake schema or engine/runtime pin change is intended; installed-artifact `release-preflight` remains authoritative.

## [0.6.30] - 2026-10-23

### Added

- Notification digests persist local-time delivery preferences and durable queued items (#767).
- Cutover backups support immutable generations and verified publication receipts (#775).
- Continuity work queue control now records explicit worker states and authority, enforces deadlines and owner leases with non-destructive reaping, keeps hub decisions pure with shadow parity checks, persists fenced wakes with acknowledgment, and makes the hub the decision authority (#789–#794).

### Fixed

- MCP hub reads and work-queue attention-source collection stay within their request deadlines, reducing latency under concurrent load (#782, #793).

### Upgrade notes

- Additive control migrations 30–31 add persisted notification digest preferences and queue state; migrations 32–36 extend continuity control state. No DuckLake schema or engine/runtime pin change is intended; the point-release classifier and installed-artifact `release-preflight` remain authoritative.

## [0.6.29] - 2026-10-23

### Added

- `cutover release-preflight` can record a private, append-only evidence ledger that correlates an existing verified Studio NAS snapshot receipt with its read-only preflight result; it does not publish, select, or roll out a release (#772).

### Fixed

- Factory observer completion events are routed through durable, authenticated owner handoff and receipt records, preserving explicit owner acknowledgment boundaries for the inactive continuity plugin (#774).

### Upgrade notes

- Additive control migrations 28–29 extend Factory observer owner handoff and durable completion receipts. No DuckLake schema or engine/runtime pin migration is included. Upgrade the hub and harnessd hosts; NAS keeps `--relay`.

## [0.6.28] - 2026-10-22

### Added

- `drover hub serve --tailscale` configures the existing hub listener behind private Tailscale Serve HTTPS and reports its MagicDNS URL without enabling Funnel or hub-native TLS (#765).

### Fixed

- The isolated UAT launcher uses a private, mode-0700 `~/drover-uat` root only for its child harness process. It rejects traversal, symlinked, outside-home, and `/tmp` roots without changing a running production daemon (#764).
- Updated the web test toolchain to Vitest 4.1.12 and refreshed its lockfile, removing the reported development-only audit advisories; the shipped Preact bundle is unchanged (#766).
- The web cockpit now renders active harness sessions with a normalized running state (#768).
- Local embedding selection logs report the configured backend accurately (#769).
- Advisory provider facts now read from DuckLake, preserving the production source of truth (#770).

### Upgrade notes

- No control or DuckLake migrations. Existing hubs and harnessd hosts remain compatible. Tailscale Serve is opt-in and private-only.

## [0.6.27] - 2026-10-21

### Added

- Web 1.0 foundation and live session controls: five-destination navigation, hub-backed cockpit views, New Session, session-detail composer, approvals, interruption, and watch controls gated by harness capabilities (#745, #749).
- Hub-owned work-item continuity: lifecycle tracking, durable signed event subscriptions, GitHub PR/CI fact ingestion, and opt-in rules/queue execution (#748, #754, #757, #758).
- Rust harnessd Phase 3 Claude Code and DeepSeek adapters, preserving Python-compatible projections and canonical D10 deduplication (#747).
- Scheduled, safety-gated DuckLake compaction and referenced-file early warnings; it remains opt-in and requires a separate maintenance DSN (#743).

### Fixed

- Staged host runtimes are relocatable and verified before activation; pairing commands no longer require the lake service environment (#744).
- iOS navigation guards and regression coverage protect the build-24 Analytics-loading horizontal-swipe crash path (#756).
- Test-suite flake fixes and fast/slow CI tiers improve deterministic validation (#755).

### Changed

- Documented runtime dependencies, added Python and Rust `drover-harnessd store check`, and added a cloud hub Dockerfile (#751).
- Refreshed public and operational documentation, including web client, Rust harnessd, and release guidance (#742).

### Upgrade notes

- Additive control migrations 23–26 create the work-item continuity, event delivery, GitHub webhook/fact, and rules-engine tables and columns. No DuckLake schema or engine/runtime pin migration is included; determine the final rollout path with installed-artifact `release-preflight` before activation.
- Upgrade the hub and harnessd hosts. Rust harnessd now supports Claude Code and DeepSeek; existing Python harnessd hosts remain compatible. iOS changes ship in the next TestFlight build.


## [0.6.26] - 2026-10-19

### Added

- Hub-served web client at `/ui`, with iOS-style Home and session-detail screens; the packaged wheel includes its hashed web assets (#718, #723).
- Rust harnessd Phase 2 protocol, delivery, transport, event-bus, and folder capabilities, plus the GPU-PC pilot runbook (#727).

### Fixed

- APNs device registrations now deduplicate by push token and mode so a phone has one active `action` registration (#726).

### Changed

- Added the BYO-cloud container-hub design note and refreshed operational documentation (#738).

### Upgrade notes

- No control or DuckLake migrations. Upgrade the hub and Studio/NAS harnessd hosts; NAS keeps `--relay`.


## [0.6.25] - 2026-10-19

### Fixed

- Structured event ownership rejections now quarantine retained host history with a reason and a hub insight. Missing-session registration grace is bounded to ten minutes, and reconciliation distinguishes ownership, delivery, and local ledger failures instead of reporting healthy hubs as awaiting recovery (#720, #721).
- Make the ownership reconciliation regression independent of HTTP deadlines and background-worker timing on loaded CI runners; verify transport timeouts separately without retries or skipped coverage (#724).

### Upgrade notes

- Control migration 22 creates the additive `harness_event_quarantine` table. No lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`).

## [0.6.24] - 2026-10-18

### Fixed

- Hub startup no longer spikes native memory on DuckLake hubs that haven't retired legacy writers (the Studio config). 0.6.23 still peaked at 4.18 GiB because #702's analytical pin only ran with `retire_legacy_writers`, so provider-refresh advisory enqueues closed the last handle to the legacy `drover.duckdb` and checkpointed it on every close. The bounded analytical pin now runs whenever DuckLake is selected, before bootstrap and workers. Off-production replay: first-120 s peak 3.42 GiB -> 0.56 GiB (#466, #708).
- TestFlight bundle display names, and Apple validation errors are kept in the upload log (#707).

### Changed

- CI: iOS workflow provisions and verifies the widget TestFlight profile (#700); harnessd notarization uses an ephemeral signing keychain on the search list and assesses the bare notarized binary (#705, #709).

### Upgrade notes

- No control or lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`).

## [0.6.23] - 2026-10-18

### Fixed

- Hub startup no longer spikes native memory (0.6.22 peaked at 5.4 GiB on the Studio). Exclusive DuckLake hubs now pin the retired analytical DuckDB instance, so short advisory ledger opens no longer destroy and reload it (with a checkpoint and a fresh task scheduler each time). Instance settings (`memory_limit`, `threads`, `preserve_insertion_order`) are passed to `duckdb.connect(config=...)` instead of `SET` after connect. A lake-shaped cold/warm harness asserts startup peak under 1 GiB (#466, #702).

### Added

- Hub startup footprint sampler: one sample per second during startup, and a jump of 64 MiB or more captures a redacted macOS native sample signature (#702).
- Rust harnessd (`harnessd-rs/`, binary `drover-harnessd`) phase 2 pilot slice: shell and Codex sessions, relay spoke client, and the tagged `rust_phase2` contract slice in CI on linux x86_64 (static) and aarch64-apple-darwin (#691, #701).
- Rust harnessd relay shell terminal channels (open, attached output, input and interrupt forwarding, exit) with a bounded PTY output buffer, and `--import-jsonl` / `--import-dry-run` to import drain-handoff session and event JSONL with event IDs and dedup keys preserved (#691, #704).
- `harnessd-macos-notarize.yml`: builds `drover-harnessd` for aarch64-apple-darwin, signs it with the Developer ID identity (hardened runtime) and notarizes it; `release.yml` calls it after the release is created to attach the signed asset. The job runs in an approval-gated environment (#691, #703).

### Upgrade notes

- No control or lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`). The Python harnessd remains the shipped implementation; the Rust binary is a pilot and is not installed by this rollout.

## [0.6.22] - 2026-10-18

### Fixed

- harnessd startup no longer does unbounded control registry work: startup is bounded, acked events are pruned (7 days / 500 per session), hosts run with `write_outbox=False`, and the loopback timeout is 120s. Control migration 21 adds `harness_events.reconcile_pending` plus two indexes (#690 phase 0, #696).
- iOS store builds tighten App Transport Security: arbitrary loads are removed from Release/StoreRelease (local networking stays allowed); plain-http FQDN hub URLs get a re-pair prompt explaining HTTPS, and the pairing QR prefers the Tailscale or LAN IP (#684 phase 1, #687).
- CI relay-hello and startup-rehearsal tests are stabilized against a fixture timing race and an unrelated shared-clock sleep observation (#699; test-only).

### Added

- iOS home screen: stable session ordering, host and project filters, and a bird's-eye fleet overview matrix that filters on tap (#688).
- iOS offline demo mode for App Store review: an in-process demo hub with fictional data; saved config and Keychain token are left untouched (#683, #692).
- iOS home-screen widget (small and medium) for account quotas and active fleet hosts, fed by an App Group snapshot with an optional fresh hub fetch (#695).
- drover.fyi `/how-it-works` and `/quickstart` pages (Claude Code, Codex, OpenClaw, Hermes) and a Stories section (#694).
- harnessd black-box contract suite and `tests/contract/PORTING.md`: 110 source tests are ported and 34 are explicitly dropped. The suite runs against the shipped Python harnessd with `HARNESSD_IMPL=py`; its `rs` lane is a placeholder that skips until the Phase 2 Rust binary exists (#697). No Rust harnessd binary ships in this release; the Python harnessd remains the shipped, bugfix-only implementation under D12.

### Upgrade notes

- Control migration 21 (additive DDL plus a one-time `reconcile_pending` backfill over `harness_events`, atomic with the migration). Time the backfill against the real control DB before the hub flip. No lake migration. Upgrade the hub and the Studio, NAS and GPU PC harnessd hosts (NAS keeps `--relay`). iOS changes ship in the next TestFlight build; the widget needs its provisioning profile.

## [0.6.21] - 2026-10-17

### Fixed

- Hub analytical connections are admitted through a bounded lock: a caller waits at most 30s for the analytical connect lock and then gets 503 busy instead of piling up behind a long holder. Lake readers are spawned without forking the hub process (#466, #679).
- Notification modes behave as labelled. "Action needed only" alerts only on live input or approval requests (finished and failed no longer alert), and "Daily digest" no longer sends immediate local alerts; a one-item digest is always a `Session updates` summary (#680).

### Added

- Hub memory attribution: `hub_activity` start/end log lines with footprint, delta and duration for requests and background passes (including the DuckLake export batch), analytical lock acquire/release logging with holder and hold time (warning over 30s), and a one-shot all-thread stack dump the first time current footprint crosses 1 GiB (#466, #679).
- Push notifications carry per-session content and grouping: title and body from the session (repo, session title, condensed question or approval), one thread per session, a collapse id per session and kind, a 60s per-session debounce, interruption levels and an Open category. Content is gated by the hub's durable content consent and fails closed to the generic text; approval commands are redacted before they reach APNs (#689).
- drover.fyi `/privacy` and `/support` pages; the iOS app's privacy and support links point at them (#681).

### Upgrade notes

- No control or lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`). iOS changes from #680/#681 ship in the next TestFlight build.

## [0.6.20] - 2026-10-17

### Fixed

- The hub rejects `/harness/events` request bodies over 16 MiB with 413 before reading them, so one oversized replay can't balloon hub memory (a 1 GiB envelope went from 3.1 GB to 123 MB peak in the repro). The harnessd event pusher now sends byte-bounded pages (15 MiB), splits a page and retries on 413, and replaces a single event too large to send with a size-capped tombstone that keeps its `event_id` and sequence, logging a warning and counting it as undelivered (#466, #676).

### Upgrade notes

- No control or lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`). Measured real replay pages are under 2 MB, so 0.6.19 pushers stay compatible with the new hub limit. iOS changes from #672 ship in the next TestFlight build.

## [0.6.19] - 2026-10-17

Supersedes 0.6.18, which was tagged but never rolled out: its strict release preflight refused control migration 20 as a lake schema change. 0.6.19 ships the 0.6.18 changes plus the classifier fix below.

### Fixed

- `release-preflight` classifies additive control DB migrations (new tables, `ADD COLUMN IF NOT EXISTS` nullable or defaulted columns, new indexes) as `control_migration`, allowed on the point-release path. Drops, renames, type or nullability changes, data rewrites, edited existing migrations, and any lake schema or runtime pin change still require the full cutover (#674).

### Upgrade notes

- Same as 0.6.18: control DB migration 20 (additive), no lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`). iOS changes from #672 ship in the next TestFlight build.

## [0.6.18] - 2026-10-17

### Fixed

- Hub startup no longer binds the legacy Parquet views when `analytics.backend = "ducklake"`. That step resolved the broad Parquet globs and scanned every historical span in the hub process, taking the Studio hub from 1.83 to 5.13 GiB at startup for views a DuckLake hub never reads. `scripts/measure_hub_memory.py` now measures RSS through the `/cockpit/overview`, `/analytics` and `/insights` HTTP paths; findings are in `docs/operations/hub-rss-466.md` (#466, #670).
- Replayed events for pruned sessions are acknowledged instead of retried forever. The hub acks events whose `event_id` already exists centrally even when the session row is gone, persists the unknown-session retry window in the control DB so restarts don't reset it, and returns an explicit orphaned list after 24h. harnessd marks acked or orphaned ledger rows reconciled and logs one info line per orphaned session instead of a warning per batch. New counter `drover_harness_orphaned_events_total` (#669, #671).
- Analytics: the Sessions | Tokens ranking toggle works. The client now sends a `metric` parameter that flows through the analytics boundary allowlist into the DuckDB ordering and cursor, so top-N pages follow the selection. Token figures use compact K/M/B units, coverage text appears once per row, and Popular Projects coverage is computed across all projects (#672).

### Upgrade notes

- Control DB migration 20 (additive: new `harness_unknown_session_retries` table and a `reconciled_at` column on the harnessd ledger). No lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`). iOS changes ship in the next TestFlight build.

## [0.6.17] - 2026-10-17

### Fixed

- Content-consent propagation recovers when a host holds a newer epoch than central. harnessd's 409 now carries `code=stale_consent_epoch` with the host's epoch and state; central advances its epoch past the host's and re-propagates once, a host already in the desired state reports `acknowledged`, and older 409s get a clear reason instead of an endless retry (#664, #666).
- Expired Claude Code logins are classified instead of reported as probe failures: an expired access token with a usable refresh token is `login_refresh_needed`, a dead refresh token is `login_expired` (last-known usage kept), and a host with no Claude credentials is `not_configured` and left out of fleet quota and connector alerts. Credentials stay read-only; the cockpit shows the per-host repair step (#665, #667).

## [0.6.16] - 2026-10-16

### Fixed

- harnessd serves `GET /lifecycle/worktrees` from a background inventory cache (300s refresh, 60s per-run bound, one scan in flight) instead of running Git inspection on the request path, so slow hosts like the NAS no longer exceed the hub's 10s relay timeout. Returns 503 before the first scan; failures keep the last good inventory and add an error (#660, #661).
- `release-preflight` errors now name the bad input (exception type and message, credentials redacted) and validate the lake root, lake path, plist, backup dir and sha256 values up front. Paths with spaces must be quoted (#659, #662).

### Upgrade notes

- No control or lake migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`).

## [0.6.15] - 2026-10-16

### Fixed

- harnessd refreshes provider usage in the background every 300s and `GET /providers/usage` serves only the cache, so slow probes no longer stall the request or leave quota stale. A probe that outlives its 60s wait is not re-entered and its late result is still published; failed probes keep the last good quota as `status=stale` with the newest error category (#655, #657).
- When harness and native events both observed a session, usage totals come from the more complete source (more observed events, then exact over inexact). Affected sessions are re-projected once; no migration (#319, #652).
- Session recaps and titles no longer show meta-commentary from the summarizer; they fall back to the repo, folder or harness title. Live prompts reach the app faster (harness batch window 2s to 250ms, hub stream poll 1s to 200ms) (#609, #651).
- iOS: work decisions are shown as compact rows with a count header, collapse to two rows with Show all, expand when a notification opens a hidden item, and expose actions to VoiceOver. Stale items are dimmed and tagged (#653, #654).

### Added

- A regression test for draining trailing terminal output when the app detaches from a direct (non-relay) proxy (#98, #650).

### Upgrade notes

- No control migration. Upgrade the hub and the Studio and NAS harnessd hosts (NAS keeps `--relay`).
- The faster stream settings raise the harness pusher POST rate (up to 4/s per streaming session) and per-WebSocket registry polling (5/s).

## [0.6.14] - 2026-10-16

### Fixed

- Lake commands that publish a new verification proof (`lake verify`, `lake deprecate-legacy --apply`) now print the new `[analytics] verification_sha256` line and include it in their JSON output. A stale pin now fails startup with the config key and the expected and actual hashes (#642, #645).
- The hub reaches hosts that have a bound credential only through the relay. A bound host with no attached relay returns `502 host offline: relay not attached` instead of a 401 from a direct dial. harnessd refuses to start without `--relay` when the hub requires it (#574, #644).

### Added

- `drover-server recover-planned-stop` restarts the hub once after a planned stop, then reports `recovered` or the first startup error. `cutover switch` and `cutover rollback` write the planned-stop marker; without a valid marker it does nothing and reports an unplanned outage (#643, #647).
- `cutover release-preflight` has a `host_proxy` check: every online host must answer `fs/list` through the hub with a 200. It needs a device credential in `DROVER_RELEASE_GATE_DEVICE_TOKEN` (#644, #648).
- Tests covering the iOS launch folder requirement and double-launch guard (#608, #646).

### Upgrade notes

- No control migration. Upgrade the hub and the Studio and NAS harnessd hosts; bound hosts must run harnessd with `--relay`.
- Set `DROVER_RELEASE_GATE_DEVICE_TOKEN` before running the preflight. Hosts whose harnessd predates folder listing fail `host_proxy`; upgrade or retire them first.

## [0.6.13] - 2026-10-15

### Fixed

- `lake verify` now accepts receipted exporter rows written after a rebuild. It checks the live lake against the frozen rebuild baseline plus the exporter receipt chain and still fails closed on any row neither explains (#637).
- `lake deprecate-legacy --apply` keeps writer RSS bounded. Rows with no same-day version are bulk-deleted; the count-aware delete runs only on days with overlapping versions. A prod-copy apply peaked at 1.45 GB instead of exceeding the rebuild RSS limit (#640).

### Added

- `drover-server lake deprecate-legacy` removes unreceipted legacy rows. It is dry-run by default; `--apply` holds the maintenance fence, deletes the rows, refreshes `activity_daily`, writes a deprecation record and maintenance receipt, then runs verify, proof and checkpoint under the same fence. Runbook: `docs/operations/lake-deprecate-legacy.md` (#639).

### Changed

- `lake import` now fails closed (#639).

### Upgrade notes

- No control migration. Upgrade the hub and the Studio and NAS harnessd hosts.
- Before `lake deprecate-legacy --apply`, take and verify control, lake-catalog and lake-data backups. The delete commits before verify runs, so a failed verify is recovered by restoring from backup. `import_watermark` is cleared entirely.

## [0.6.12] - 2026-10-15

### Fixed

- `lake maintain` no longer locks the hub out. Merge, snapshot expiry and file cleanup now commit as one `drover-maintenance` snapshot. Each table's logical content is fingerprinted before and after, and a maintenance receipt binds that snapshot to the serving proof, so the serving check accepts it and advances its checkpoint. Unknown or tampered snapshots still fail closed (#634).
- Snapshot expiry during maintenance keeps both the serving baseline and the 30-day time-travel window (#634).

### Added

- `drover-server lake reconcile` finds batches that control acknowledged but whose receipts are missing from the lake, for example after a catalog restore. It is dry-run by default; `--apply` re-exports them without overwriting newer event versions (#634).

### Upgrade notes

- No control migration. Upgrade the hub, then stop it and run `lake maintain` after verifying the control, lake-catalog and lake-data backups. After a lake restore, run `lake reconcile` (dry-run first).
- Upgrade Studio and NAS harnessd hosts.

## [0.6.11] - 2026-10-15

### Added

- The DuckLake exporter now bounds restart escalation to three requests per hour. When that guard is reached, readiness reports the protected stopped state instead of allowing a restart loop (#631).
- Operators can run the fenced `lake maintain` command to compact the five export tables, expire snapshots under the existing 30-day policy, and clean files only after a seven-day safety window (#631).

### Fixed

- DuckLake 1.5.5 provisioning keeps mutable export tables out of inlining, avoiding a least-privilege delete failure while preserving inline writes for append-only export tables (#630).

### Upgrade notes

- Hub startup applies additive control migration 19 for the restart-escalation guard.
- After upgrading, stop the hub and run `lake maintain` once after verifying the control, lake-catalog, and lake-data backups. Schedule the same command in a weekly maintenance window.
- Upgrade Studio and NAS harnessd hosts.

## [0.6.10] - 2026-10-14

### Changed

- Empty work-decision sections are hidden on the iOS Sessions home screen (#625).
- DuckLake export recovery now handles small batches reliably by keeping tiny catalog writes inline and adding clearer exporter health signals (#627).

### Fixed

- The exporter fails fast instead of hanging during a recovery, and catalog snapshots now expire after 30 days (#627).

### Upgrade notes

- Restart the hub after upgrading, then run `lake provision-exporter` once with the catalog admin DSN to retune existing export tables.
- Upgrade harnessd hosts, including Studio and NAS.


## [0.6.9] - 2026-10-14

### Added

- Work queue support adds GitHub provider integration, policy controls, execution links, decision events, MCP and HTTP tools, quota-aware launch routing, restart recovery, and verification before completion (#616, #618).
- iOS now shows a Needs you list with direct links to pending work (#622).

### Changed

- The chat composer, activity chips, and artifact chips use a floating translucent design on iOS (#619).
- Installer shell tests reliably run on iOS-only changes (#621).

### Fixed

- Backup failures now preserve useful diagnostics and safely handle retry and quarantine paths (#623).

### Upgrade notes

- Restart the hub after upgrading. The hub applies an additive control migration that creates two new tables.
- Upgrade harnessd hosts, including Studio and NAS.
- The work-queue reconciler remains in shadow mode by default; do not enable it during this upgrade.

## [0.6.8] - 2026-10-13

### Added

- Backups now include live DuckDB stores and verify the frozen copies made on the NAS (#611).
- PostgreSQL setup can resume an interrupted switchover, and the Studio backup script now matches the documented backup flow (#602).

### Changed

- Claude replies finalize streamed thinking and text blocks in place, without adding duplicate messages (#600, #612).
- The iOS chat screen is lighter and stays compact at accessibility text sizes; preferences fit on one row (#601, #613).
- Push classification reads split turn payloads correctly (#610).

### Fixed

- iOS recovers its first folder read and prevents overlapping session launches (#614).
- The PostgreSQL LaunchAgent now starts with the expected locale (#602).

### Upgrade notes

- Restart the hub after upgrading.
- Upgrade harnessd hosts, including Studio and NAS.
- Reinstall the updated Studio backup script and set `backup.target_root` in the backup configuration; see the backup documentation.

## [0.6.7] - 2026-10-12

### Added

- Release rollout tooling now validates launchd jobs, runs a point-release
  preflight, and guards the updater pin (#581).
- The collector can read OpenClaw archives, handles transcripts that are
  rewritten before growing, and reports its health in `drover doctor` (#582).
- A backup and restore audit document explains how to check recovery readiness
  (#583), and a nightly Studio-to-NAS backup job is available (#584).
- A PostgreSQL LaunchAgent can be prepared for installation without loading it
  automatically (#585).
- The folder picker can create folders and page through results with a cursor
  (#586).
- Routing hints now take the account and quota freshness into account (#594,
  #592).

### Fixed

- An untouched Antigravity quota is shown as 0% used instead of unavailable
  (#588, #587).
- Unknown provider quota fields are ignored, quota window durations are kept,
  and iOS labels five-hour and weekly windows correctly (#595, #590, #591).
- MCP quota refresh uses the hub transport and keeps saved quota state if a
  refresh fails (#596, #589).
- The Codex quota probe resolves the same Codex runtime and supports a
  configurable timeout (#597, #593).

### Upgrade notes

- Restart the hub after upgrading.
- Upgrade harnessd hosts, including Studio and NAS, for the quota probe fixes.
- Install the backup job and PostgreSQL LaunchAgent separately; see the backup
  and restore documentation.

## [0.6.6] - 2026-10-11

### Added

- Tapping a `drover://` pairing link now opens a confirmation sheet before it
  pairs a device (#573).

### Fixed

- UAT fixes improve counts, interrupt handling, opaque bars, history, token
  counts, worktree names and large-text layouts. Folder-picker errors are
  clearer, and launch errors are shown in plain language (#573, #575).
- A finished reply no longer arrives as a "Needs your input" notification,
  banners for the chat you are viewing are suppressed consistently, and the
  "recall may be out of date" warning no longer flashes on first load (#575).
- The send control stays reachable at the largest accessibility text size
  (#577).
- Claude Code replies now stream as they arrive. Recap titles no longer expose
  summarizer complaints, orphan-session events cannot block another host's
  batch, and refused authentication retries with backoff (#576).
- Exporter recovery isolates its reads and exits the process after all recovery
  retries are exhausted, rather than continuing in an unsafe state (#578,
  #579).

### Upgrade notes

- Restart the hub after upgrading.
- Upgrade harnessd hosts, including Studio and NAS, for the streaming and
  orphan-replay fixes.

## [0.6.5] - 2026-10-10

### Added

- The product and documentation site now has a clearer landing page, tighter copy
  and fixes for narrow mobile screens (#567, #570).
- Push notifications are grouped by session, and delivery preferences can be set
  in the hub. This adds hub settings migration 17; restart the hub after upgrade
  so it can apply the migration (#568).
- Pairing now returns the host identifier bound to the credential, and the
  installer passes it with `--host-id` so host credentials cannot be used by a
  different host (#569).
- `drover-collect` reads OpenClaw sessions from the per-agent SQLite transcript
  store used by current OpenClaw releases (source id `openclaw_sqlite`). The
  store is opened read-only, read in small batches, watermarked per session by
  transcript sequence, and skipped with a diagnostic when busy or of an unknown
  schema. `[sources.openclaw]` gains `store` (`auto`, `sqlite`, `jsonl`),
  `state_dir`, `agents` and batch tuning; existing configs keep working and
  pick the SQLite store up automatically. See
  [OpenClaw session collection](docs/integrations/openclaw.md).
- OpenClaw events carry `tool_calls` extracted from assistant content blocks,
  for both the JSONL and SQLite sources.
- DuckLake exporter freshness in readiness, `drover doctor` and the iOS fleet
  header, including backlog ages, durable last success and recovery guidance.
- Bounded in-process exporter recovery with fenced receipt replay, five retries
  and exponential backoff, including cancellation of live hung batches after
  an optional configurable deadline (default 120 seconds).
- Control-schema migration 16 adds freshness indexes; exporter sampling now
  runs every ten seconds and avoids scanning acknowledged history.

### Fixed

- The iOS app keeps your place in a detached transcript and provides a jump to
  the latest event when new activity arrives (#565).
- iOS UAT round 4 fixes improve folder browsing and make session-stop feedback
  clearer (#571).

### Security

- MCP now requires active bearer credentials for every protocol request and tool,
  sharing HTTP credential scopes, revocation and profile-agent tiers. Profile
  credentials remain read-only. Anonymous access and non-loopback binds are
  refused; disabling authentication prevents MCP startup. Client integrations
  must configure credentials before upgrading. The bundled MCP client reads
  `DROVER_MCP_TOKEN` and refuses redirects. Profile import defaults are unchanged.

### Upgrade notes

- MCP clients must send `Authorization: Bearer <token>` using a dedicated host
  credential.
- Reissue host/device profile-agent bindings after upgrading.
- On its first run, the collector re-imports its source data.

## [0.6.4] - 2026-10-08

### Added

- Portable user profile, iteration 1 (#545): tier-filtered, budget-bounded
  profile bundles over HTTP and the `drover_profile` MCP tool, proposals with
  provenance and operator accept/reject/revert, and a dry-run-by-default
  `drover profile import`. Adds PostgreSQL migration 15 and a `drover` console
  script alias for `drover-server`. See [the profile guide](docs/portable-profile.md).
- Recall, search and handoff results carry store and host identity plus a
  computed data watermark; non-hub stores label themselves non-authoritative (#475).
- Hub-side context container producer and a dry-run-by-default
  `drover-server context backfill-containers` command (#475). Off by default
  (`[context_containers] enabled = false`).
- Report-only hub session lifecycle scans, authenticated publication snapshots,
  durable stop intent, and read-only host worktree inventory (#547). See
  [the API and configuration guide](docs/session-lifecycle.md). Enforcement,
  archiving, hiding and collection remain disabled.
- Additive PostgreSQL migrations 13 and 14 for lifecycle metadata and records.

### Fixed

- Terminal sessions expose null attention in session API projections while
  retaining raw historical attention (#234).
- Offline or missing-host terminate requests return 202 with durable pending
  intent instead of falsely ending the session. Host acknowledgement or
  reconnect reconciliation confirms the end.

## [0.6.3] - 2026-10-06

### Fixed

- Serving-proof receipt checks run as one batched query instead of one query per export,
  so cockpit and MCP reads stay inside the 5-second analytical deadline as exports grow (#541).
- The exporter advances the serving proof with rolling checkpoints, so verification no longer
  stops serving after 10,001 exports; tamper checks still fail closed (#543, #546).
- The serving check's referenced-file bound is raised from 10,000 to 50,000 (#546).

## [0.6.2] - 2026-10-06

### Fixed

- Summarizing a large active session no longer exhausts the query child's memory:
  files and tools are extracted with one JSON parse per event (#539). The query child
  now reports a sanitized cause (for example `analytics_memory_limit_exceeded`)
  instead of a bare `analytics_unavailable`, and the gate adds
  `A3_summarize_most_recent_session`.
- On macOS the hub memory guard measures phys_footprint instead of RSS, which counted
  freed allocator pages; `/readyz` reports the measurement source (#539).

## [0.6.1] - 2026-10-06

### Fixed

- DuckLake v2 at production scale (#537): the gate picks the largest session from
  the raw lake table instead of the JSON-parsing serving view (6.7s to 0.5s); the
  summarizer reads bounded 50k-event slices and derives files and tools from the
  newest 25,000 events, saying so in the summary (578k-event session: 35s to 7s,
  peak 1.05GB to 409MB); cockpit overview reads in one query child with the
  identity/epoch binding check kept (p95 about 0.6s on the prod lake).
- Control-plane DuckDB budget is sized for checkpoints of large stores, and a
  failing checkpoint is reported by readiness instead of failing silently (#536).

## [0.6.0] - 2026-10-05

### Added

- DuckLake v2 (#481): acceptance harness with a prod-shaped control store and a
  seeded 5M-event lake (#521); a single ingest path through the control outbox
  with `outbox replay --sink legacy` (#526); production-size read paths for the
  summarizer and cockpit (#532); a fresh lake with selective, idempotent
  `lake import --since/--session` and archived recall status (#531).
- `drover-server gate`, cutover preflight, and `cutover switch|rollback|backup`
  with `--dry-run`, backup-first ordering and a required fresh gate verdict, plus
  the runbook in docs/operations/lake-cutover.md (#533).

### Fixed

- Summarizer query children project tool fields only after LIMIT and run with
  bounded engine settings, keeping 40k-event sessions far under the child RSS
  cap (#534).
- Cockpit lake activity no longer reports unavailable when sessions exist
  (identity column mismatch from #531, fixed in #533).

## [0.5.7] - 2026-10-04

### Fixed

- Antigravity Claude/GPT quota no longer reports a false 100% remaining. Buckets
  whose reset time slides with every fetch are omitted, and real agy 429
  "Individual quota reached" errors are recorded per host and model group as
  `observed_exhausted` until the reported reset (#522, #527).

### Added

- MCP tool `drover_provider_quota`: per-account quota windows (used %, reset,
  status, source) with a bounded `fresh` re-probe, masked account labels and a
  compact `routing_hint` (#523, #528).

## [0.5.6] - 2026-10-04

### Fixed

- Raised the shared analytical DuckDB memory limit default from 1GB to 4GB after
  legacy-store autocheckpoint OOMs. Recovery reopens use the same effective limit
  (`DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT`), and the startup checkpoint follows it
  unless `DROVER_ANALYTICAL_CHECKPOINT_MEMORY_LIMIT` is set. `/healthz` stays a
  liveness probe (200) but its body now reports `analytical=<status>`; `/readyz`
  returns 503 while analytical recovery is in progress (#520, #524).

### Added

- DuckLake v2 acceptance harness (`tests/acceptance/`, A1–A7) with a prod-shaped
  control-store fixture (#521).

## [0.5.5] - 2026-10-04

### Fixed

- Control-schema migration 11 creates `lake_export_batches` on stores that applied
  migration 2 before it was edited in place, so the DuckLake exporter no longer fails
  with UndefinedTable. Released migrations are now hash-pinned in tests (#517).
- DuckLake reads: the summarizer pages canonical events with a bounded keyset instead of
  failing `analytics_row_limit_exceeded`, and cockpit activity scopes its PostgreSQL
  snapshot with bound parameters and logs the real lake error code (#518).
- `lake rebuild` preflights catalog permissions and surfaces the sanitized attach cause
  instead of `analytics_unavailable` (#515).

### Added

- `drover-server lake provision-exporter` explicit admin step, and exporter startup
  failures now log the real cause (#516).

## [0.5.4] - 2026-10-04

### Fixed

- Offline DuckLake rebuilds now use a bounded RSS cap, preventing unbounded
  memory growth while preserving the rebuild path (#513).

## [0.5.3] - 2026-10-04

### Added

- Session history across hosts: `GET /sessions/history` pages every session
  in the PostgreSQL control plane, newest activity first. It uses a keyset
  cursor and supports host, harness, repo, state, date-range and full-text
  (`q`) filters. Pages default to 30 rows, are capped at 50 rows and 64 KiB,
  and p95 is 6.8 ms per page at 10K sessions. `GET /sessions/history/facets`
  serves filter values. Migration 9 adds the keyset indexes and a
  trigger-maintained `session_search` document. See
  `docs/design/session-history.md`.

### Changed

- Host liveness (online / stale / offline / retired) is derived from heartbeat
  age in one helper and sent by the hub as `liveness` on `/harness/hosts`, so
  fleet, provider capacity, launch pickers, content consent, MCP
  `drover_fleet_status` (`host_liveness`), web and iOS agree. Relay hosts that
  go dark now read stale, then offline, instead of staying online. Thresholds
  are `DROVER_HOST_STALE_AFTER_SECONDS` (45) and
  `DROVER_HOST_OFFLINE_AFTER_SECONDS` (600); see `docs/multi-host.md` (#474).

- A test-only adapter with an unusual capability mix now runs through the
  registry, harnessd, the hub API, and the web and iOS fixtures without any
  harness-specific branch (#422). The extension points this measured are in
  `docs/design/adapter-extension-points.md`. Making it pass changed the
  contract:
  - harnessd starts a PTY session only for a harness that advertises `pty`.
    Provider CLIs no longer start as raw terminals through the API.
  - Native resume is the adapter's resume operation and runs as a structured
    session. The per-harness resume flags in harnessd, and the bare "latest"
    forms, are gone.
  - The hub's Continue and restart recovery follow the target host's
    capabilities instead of the hub's own adapter list. Continue onto a host
    without a capability matrix is refused with an upgrade message.
  - Interrupt, approvals, attachments outside the declared MIME types, model
    or effort without a catalog, and unsupported native resume are refused
    before any provider call.
  - Harness rows carry an additive `display_name`, used by the web pickers and
    the iOS launch sheet.
- OpenClaw is observe-only end to end (#421). harnessd no longer carries an
  OpenClaw native-resume argument or maps a terminal command to `openclaw`;
  neither path was reachable without a preset. Collection, parsing,
  attribution, metrics, recall and historical rows are unchanged, and a
  regression test keeps OpenClaw and Hermes out of presets, the adapter
  registry, the capability envelope and both launch modes.
- Span ingestion is an optional integration, off by default (#473). The OTLP
  receiver only starts with `[telemetry] spans_enabled = true`; with it off no
  span Parquet is read, no span embedding jobs are enqueued or claimed,
  `doctor`/`quality`/`data_quality` report spans as `disabled` instead of
  missing or stale, advisory checks no longer raise "span feed is silent",
  `drover_recall` returns session-summary hits only, and span-only commands
  (`trace-tail`, `recent-traces`, `embeddings enqueue-spans`/`reset-stale-spans`/
  `prune-orphan-spans`, `decisions derive`, `session graph --spans`) name the
  flag. Historical span data is kept; no schema is dropped.
  `drover-collect tempo-relay` remains as an optional, legacy command. See
  `docs/optional-span-integration.md`.
- `drover-server session graph` now draws the work tree from recorded launch
  metadata (delegation parent, handoff source, Factory run) with each
  session's state and what is stuck; `--run RUN_ID` shows a Factory run.
  Also served at `GET /harness/sessions/{id}/graph` and
  `GET /harness/runs/{run_id}/graph` (#473).
- MCP `drover_project_activity` answers "what happened on this project and what
  is still open" from Drover's own sessions, summaries and `session_usage`,
  with hard caps (30 days, 20 projects, 200 sessions, 20 open items). Also
  served at `GET /projects/activity` (#473).
- The app hides the API-billed cost metric when no session reported a cost,
  and names spans as a measurement source only when they contributed (#473).
- Phase 1 of the memory-integrity program (#478, program #476) removes Pond
  recall, CLI, and backup integration. Legacy `[archive]` keys are ignored with
  one deprecation warning per process. Recall bundles serve hub context and
  include `sources: ["hub"]` while retaining their response envelope.
- DuckLake and R2 catalog+files generation backups plus a Postgres dump are
  planned for Phase 4 (#481); `docs/backup.md` records the unimplemented design.
- Web console launch and session controls come from the advertised harness
  capability matrix, not harness names (#419). Launch offers only an enabled
  schema v1 harness with a launch mode the web can drive, and sends that mode
  (`structured` is preferred when both are advertised). Provider CLIs now start
  as structured sessions with a web turn composer instead of raw PTY terminals.
  Interrupt, Approve/Deny, attachments (advertised MIME types only), model and
  effort pickers, native resume and the worktree note appear only when
  advertised, and every action re-checks the latest envelope. Hosts without a
  matrix are listed with an upgrade note and cannot launch.
- Derived memory moves to the PostgreSQL control store with one job ledger
  (#480, closes #464). Session summaries, live recaps (now the live phase of
  one `session_memory` row), project briefs and pgvector `vector(768)` session
  embeddings live in PostgreSQL; summarize/embed/brief/recap jobs live in
  `pipeline_jobs`, claimed with `FOR UPDATE SKIP LOCKED` under lease tokens,
  with bounded retries, per-row quarantine and a reason on every terminal
  failure. The DuckDB `summarize_jobs`/`embed_jobs`/`brief_jobs`/
  `span_embed_jobs` queues, the DuckDB ledger shadow, the Redis memory streams
  and span embeddings (#473) are gone. Derived rows are rebuilt, not migrated:
  schema bootstrap preserves legacy data. Run
  `drover-server memory requeue --since <date>` after upgrading;
  `drover-server memory purge-legacy` previews optional cleanup. A DuckDB-only control store
  records sessions but derives no memory.
- `/readyz` reports a `memory` section: pgvector status (missing pgvector
  fails readiness), whether embeddings are configured or disabled, and per job
  kind the pending, running (oldest lease age), retrying, dead-lettered and
  quarantined counts, oldest pending age and last success (#471). The
  `ledger reconcile|replay` and `embeddings reset-stale-*|enqueue-spans|
  prune-orphan-spans` commands are removed; expired leases are reclaimed
  automatically.

### Added

- Harness launches accept an optional `parent_session_id` so an orchestrator
  can record delegation explicitly; it is stored on `harness_sessions`
  (PostgreSQL control-store migration 5) (#473).

- Sign-out revokes the phone's own hub credential and clears its APNs
  registration through `DELETE /auth/device/credential` (self-only and
  idempotent). Offline or older hubs still complete local sign-out with a
  warning and no retained credential (#443).
- The production TestFlight lane waits up to 45 minutes for Apple processing,
  applies the declared export-compliance answer, and assigns and verifies the
  build in an internal beta group (#441).
- The factory-observer delegation bridge can launch Claude Code: `thinking_effort`
  is optional (validated when supplied), every factory launch requires an
  isolated Git worktree, and Claude factory sessions run with
  `--permission-mode dontAsk`, so tools that would need approval are denied
  instead of parking the session (#445).
- iOS harness controls are capability-driven (#420). The launch sheet, sign-in,
  model and effort pickers, approvals, interrupt, handoff targets, worktree note
  and attachments read the host's schema v1 capability matrix instead of
  harness-name lists. Controls a host does not advertise are hidden or
  disabled with a spoken reason. Hosts without a matrix are listed but offer
  nothing to launch, with an "update Drover on the host" explanation. A
  DroverKit test fails if a quoted harness ID appears in iOS control code.
- Capability schema v1 gains an additive `turn_preferences` flag: model and
  effort overrides reach later turns of a running session. It is projected
  from the adapter's existing turn-dispatch contract, so it is false for
  Claude Code and true for Codex, agy and DeepSeek. Hosts that predate the flag
  omit it, and clients read that as false (#420).

### Fixed

- Codex shell edits now contribute to `files_touched`: each turn records
  Git commit paths and outstanding worktree changes in the normalized event
  stream, including clean committed turns and edits subsequently reverted
  within a turn (#492). Existing summaries need new source evidence and
  regeneration; this does not backfill historical sessions automatically.

- The Agy model catalog works with agy 1.2.11, which no longer writes
  `google_accounts.json`. Sign-in and the catalog's account scope now come
  from the same Keychain/file credential reader the usage probe uses (identity
  from the credential's `id_token`, legacy account file as a fallback), and
  models come from native `agy models` instead of the host reporting
  `not_authenticated` with a stale catalog. Switching or signing out of the
  account invalidates the cached catalog; no tokens or addresses are logged.
- The hub survives its bind address disappearing, for example a VPN client
  stopping. A missing `[server].metrics_host` at startup no longer exits into
  a launchd/systemd restart loop (API role) or leaves the all-in-one hub
  running with no listener. The hub stays up, logs, retries with backoff
  capped at 30 seconds, and serves as soon as the address appears. A specific
  address that vanishes while serving has its listener closed and rebound when
  it returns, and binding no longer does a reverse DNS lookup that could stall
  while the VPN's resolver is gone. The docs now recommend
  `metrics_host = "0.0.0.0"` with the existing auth and a firewall for hubs
  with both LAN and VPN addresses (#457).
- APNs rejections are no longer silent. Device-level rejections
  (`BadDeviceToken`, `DeviceTokenNotForTopic`, `Unregistered`/410,
  `ExpiredToken`) clear that phone's registration and the hub keeps refusing
  the same token; key- or topic-level rejections (`BadEnvironmentKeyInToken`,
  `InvalidProviderToken`, `ExpiredProviderToken`, `TopicDisallowed`, ...) mark
  that APNs environment unavailable until the hub restarts, with one
  rate-limited error naming the key and topic. Throttling, 5xx and network
  errors keep registrations. `PUT /auth/device/apns` answers the existing
  `503 hub push is unavailable` in both cases, `/readyz` reports a `push`
  state, and the app re-sends its token when it comes to the foreground so it
  resumes local notifications. PostgreSQL control stores apply migration 4
  (#449).
- The iOS launch sheet says when a host's release cannot complete paths
  instead of staying silent. The hub keeps an unknown host, a host without the
  `/fs` routes (404 with `reason: unsupported`) and a registry read failure
  (500) apart; the status for an unsupported host stays 404 so older app
  builds are unaffected (#232, #453).
- A Codex or DeepSeek session parked between turns no longer blocks host
  updates forever with `not_quiescent`. Quiescence now asks whether a restart
  would cut work off instead of whether the session is open; a running turn, a
  pending approval, a live Claude Code process, or an open Agy session (which
  cannot be recovered after a harnessd restart) still blocks (#236, #452).
- An Agy session parked between turns no longer blocks host updates either:
  after a harnessd restart it resumes its conversation with
  `agy --conversation <id>` (the same invocation every later turn already
  uses) instead of reporting that it cannot be resumed. A session that never
  completed a turn has no conversation to resume and asks for a new session.
  Activation also closes the turn-vs-restart race: harnessd stops accepting
  turns before its final quiescence check and answers `409 host is restarting
  for an update` until it restarts, lifting the gate after five minutes if the
  restart never comes (#236).
- iOS honors `Retry-After` on hub `503`s across session polling, stream
  reconnects, cockpit and insight loads, auth polling and background refresh,
  with separate cooldowns for analytical and session reads, and shows
  "Hub busy, retrying in Ns" instead of an error (#442).
- Delegated (factory-observer) sessions record their isolated worktree `cwd`
  and repository identity, so iOS and web show project labels; iOS prefers
  `repo_name` over the directory name (#444).
- When the hub cannot send push, it refuses the device APNs registration so the
  app keeps local notifications instead of assuming push will arrive (#439).
- harnessd `GET /sessions` no longer returns every session the host has ever
  run. It lists every live session (sessions waiting on the user first), then
  the newest 20 finished ones; `?archived=N` (up to 100) and
  `?archived_cursor=` from `next_archived_cursor` page through older history.
  The hub's `/harness` fleet view, which iOS and web read, is unchanged (#224,
  #451).
- The watcher no longer fails a batch about once a day with `Conflict on tuple
  deletion!` in `enqueue_summary_generation`. When a live session's next batch
  arrived while the summarizer was committing the previous generation, both
  rewrote the same `summarize_jobs` row in overlapping transactions; the batch
  was left in place and parsed again. Every `summarize_jobs` write in the hub
  now goes through one in-process writer, so the enqueue waits for that commit
  and the next generation is queued behind it. Re-parsing a batch after a
  failed enqueue adds no events and no second job (#308).

- Keep metrics scrapes and small insight actions independent of cockpit admission,
  allow heavy requests a one-second admission wait, retry deferred day summaries
  after 30 seconds, and let control-only usage rollups progress during analytics
  (#331, #436).

- Bound analytical CPU and HTTP concurrency, keep background rollups deferred
  throughout foreground builds, and return retry hints for saturated analytical
  and fleet listings without queueing control requests behind analytics (#331).

- One Google account no longer shows as two provider cards. agy 1.2.11 keeps
  the signed-in email only in its credential's ID token (`google_accounts.json`
  is gone), so the Studio reported the generic label `Antigravity` while older
  hosts reported the email. The agy probe now reads the email (or a hashed
  Google subject) from the credential it already uses for quota and reports
  `Unknown account` with a null identity when it cannot; it no longer treats
  the account file's `old` history as the signed-in user. Provider readings
  carry an optional `account_identity` (older readings derive it from an email
  label), and iOS groups accounts by provider plus identity: an unknown reading
  joins the provider's only known account and never guesses between several.
  Quotas are still never summed across hosts. Readings from retired hosts, and
  `host_offline` readings older than 72 hours, fold into a collapsed
  **Stale hosts** list on the Accounts page and no longer count on Home; no
  data is deleted. Older app builds ignore the new field.
- A host that has been dark for days no longer keeps a quota meter on Home just
  because its last probe failed as `unavailable` instead of `host_offline`.
  Relay hosts are exempt from the 45-second stale-heartbeat skip, so the hub
  kept probing one that had gone dark and recorded each failure as
  `unavailable`; it now tags any host whose last heartbeat is over 10 minutes
  old as `host_offline`, whatever its connection kind. iOS also treats any
  reading the hub no longer reports as current (anything but `ok` or
  `usage_unavailable`) as a stale host once its last success is over 72
  hours old, whatever the last error was, so older hubs are covered too. Quotas
  are still never summed across hosts, and account identity grouping is
  unchanged.

### Changed

- iOS cockpit: provider quota shows once per account as compact rows with
  per-host probe indicators and expandable windows; Analytics leads with totals
  and coverage and compares one dimension at a time; Insights adds a severity
  overview, compact rows and folded advanced filters. Chat shows cached session
  activity (current tool, elapsed time, completed steps) and stops tool
  animation on disconnect, approval or turn completion, respecting Reduce
  Motion (#447).
- iOS Home keeps the pinned header to a compact, horizontally scrolling strip
  of individual account meters (lowest remaining quota first; one meter per
  account, never summed across hosts), so sessions are no longer pushed out of
  view. Account identities, host reporting states, quota windows and
  diagnostics move to a dedicated Accounts page with pull to refresh, and
  Home's activity and project blocks become compact Analytics and Insights
  links below the session list (#456).
- iOS Accounts cards list every host for an account in one chip row: live
  hosts with their usual icon, stale or retired hosts with the clock icon and
  their name. The collapsed **Stale hosts · N** disclosure and the
  "Not reporting on …"/"Couldn't reach …" footer are gone; each chip's
  VoiceOver label carries its state, last report time and error (for example
  "NAS, stale, last reported 5 days ago, couldn't reach host"). Stale hosts
  still never count on Home.
- TestFlight uses the production lane with unrestricted hub pairing; the
  internal staging lane and separate staging hub are retired. A read-only,
  bounded live-hub smoke runs before upload, and physical-device acceptance is
  against the operator's live hub (#390, #440).
- Device-scoped credentials can no longer revoke other credentials by ID; a
  phone revokes only itself through sign-out (#443).

- Host and central harness listings publish bounded capability schema v1 from
  the adapter registry, with fail-closed mixed-version handling. Legacy fields
  remain available; command arrays are cleared to keep launch arguments private
  (#418).

- Claude Code, Codex, agy, and DeepSeek structured sessions now resolve driver
  construction, default commands, worktree policy, authentication, and model
  catalogs through the validated harness adapter registry. No intentional
  lifecycle or client behavior changes.

- Public README, security policy, security guide, and threat model are
  version-neutral: only the latest tagged release is supported, PostgreSQL is
  the fresh central-install control store without automatic DuckDB migration,
  and the legacy shared token and unbound host credentials (#13) remain
  explicit limitations. Documentation only (#415, #450).

## [0.5.2] - 2026-09-26

### Fixed
- Fixed the post-publication managed PostgreSQL backup verifier to use the clean-install DROVER_HOME when invoking drover-managed-postgres.

## [0.5.1] - 2026-09-26

### Fixed

- The clean-install release verifier now streams its temporary PostgreSQL setup
  SQL over standard input, avoiding runner temp-directory permissions when
  executing `psql` as the isolated `postgres` account.

## [0.5.0] - 2026-09-26

### Added
- First-class explicit PostgreSQL setup choice: validated operator-owned DSN or a Drover-owned, loopback-only PostgreSQL 17 container.
- Managed-mode health, idempotence, lifecycle, logical backup, and explicit purge boundaries.


### Fixed

- Release publication now builds a disposable runtime from the checksum-covered
  wheel and hash-pinned dependency lock before creating a GitHub Release. The
  gate requires the PostgreSQL-default control-store configuration, service
  environment-file support, and the control-store initializer, so a candidate
  that would be rejected by the public installer never becomes latest.

- The clean-install release check now provisions an isolated PostgreSQL target
  and verifies schema readiness, service environment wiring, runtime activation,
  pairing, and authenticated access without logging its generated DSN.

- `install.sh --verify-release` now checks the selected public artifact in a
  disposable runtime before any installation mutations. Plain `--dry-run`
  remains an offline action preview.

## [0.4.18] - 2026-09-19

### Fixed

- The fleet listing stopped reading the largest column in the control-plane
  store on every render. `harness_events` has no index on `session_id`, so
  the session-preview window is a full scan of the table, and it carried
  `payload_json`: 171 MB of the store's 240 MB. Every `/harness` render read
  it from disk while holding the control-plane lock, every two seconds under
  the phone's polling. Caught in the act with the SIGUSR1 dump added in
  0.4.17: 72 threads queued on that lock while the one holding it sat in
  `latest_session_previews`, in the same frame across two dumps three seconds
  apart, and again in a second wedge 40 minutes later. Measured on a copy of
  the live store: 678 ms cold with the column, 11 ms without it. Candidates
  with a blank stored preview now get one narrow follow-up query keyed on
  `event_id`; the fallback behaves exactly as before.

## [0.4.17] - 2026-09-19

### Fixed

- The watcher's startup backlog runs before the port binds again, as it did
  through 0.4.15. 0.4.16 moved it onto a thread so the port could bind sooner;
  on the hub that put the backlog, the retention sweeps and every worker on a
  slow disk at the moment the fleet started polling, and `/harness` answered
  503 to every request until the release was rolled back. The rest of 0.4.16
  is kept: the usage rollup prefilter, the retention-aware legacy prune, the
  backlog completion log line and the ingest-lock re-check.

### Added

- `drover-server run` writes every thread's Python stack to its stderr log on
  `SIGUSR1`. The fleet listing has wedged behind a render that never finished
  more than once, and until now the only way out was a restart that destroyed
  the evidence.

## [0.4.16] - 2026-09-19 [YANKED]

Withdrawn the day it shipped: the fleet listing answered "fleet listing busy"
to every request for about eight minutes after the hub restarted onto it, and
the hub was rolled back to 0.4.15. See 0.4.17.

### Fixed

- The hub no longer ingests the files already waiting in `incoming/` before
  it binds its port. The watcher did that pass on the caller's thread, and one
  restart spent 49 seconds in it with nothing answering. The pass now runs on
  its own thread after the observer starts, and logs
  `watcher backlog: N file(s) in Xs` when it finishes. A file seen by both the
  observer and the backlog pass is ingested once; the one that loses the race
  returns quietly instead of logging a false "ingest failed".

- Swept advisory occurrences came back on every restart. The pre-split copy
  of the control-plane tables is re-copied at each boot for any row the
  control plane lacks, and the 30-day sweep deletes old occurrences, so one
  boot re-inserted 5,668 rows that the sweep deleted again two seconds later.
  `drover-server harness prune-legacy-tables` would never drop that copy,
  because retention guarantees some rows are always "missing". It now treats
  rows past the configured retention window as swept, and reports how many as
  `exempt_past_retention`, so a dry run shows exactly what `--apply` drops.

### Changed

- The usage rollup fetches only events that can carry token usage (plus every
  row that is not valid JSON or not an object), instead of every event of a
  session. On the busiest session measured this halved the rows read.

### Added

- `scripts/load_harness_poll.py` polls `/harness` at the phone's rate and
  exits non-zero on any response other than 200.

## [0.4.15] - 2026-09-10

### Fixed

- A saturated fleet listing raised `NameError` instead of answering. The
  exception the handler catches was imported for type checking only, so the
  name did not exist at runtime and the `except` clause failed rather than
  caught: the 503 with `Retry-After` added in 0.4.13 never went out, and the
  request died with no usable answer under exactly the saturation it was
  written for. A client reads that as "ask again now", which is the retry
  storm the 503 exists to stop. The coverage that missed it asserted the
  collector raises; nothing drove the route, where the broken clause lived.

## [0.4.14] - 2026-09-09

### Fixed

- Every message sent from the phone rendered twice. The client owns a
  correlation ID for a delivery and clears its local pending row only when the
  stream echoes it back, but the hub parses `client_turn_id` as a UUID and
  echoes the canonical lowercase form, while the phone generates the uppercase
  one. The raw string comparison never matched a real echo, so the pending row
  outlived the message proving it was delivered: it decayed to "Still
  confirming delivery" and came back after a restart as a delivery held for
  review. No turn was ever dispatched twice -- the hub keys idempotency on the
  normalized ID -- but the duplicate had to be discarded by hand.

- Every MCP tool that answers "what sessions" failed with an out-of-memory
  error: active sessions, fleet status, handoff and recent sessions. The
  `active_sessions` view ranked every event in the lakehouse by dedup key
  before filtering to the last thirty minutes, and no predicate above a window
  function can be pushed into it, so a question about the last half hour
  materialised the whole history. Underneath that, the view treated any
  session with a summary as ended -- but the summarizer fires on an idle gap,
  so a session still working gets one long before it stops and then vanished
  from every handoff. The same view backs the session-start hook and the CLI
  handoff, so the "what else is running" line has been empty rather than
  wrong.

### Added

- An internal TestFlight staging gate: a logically separate staging hub with
  its own service root, identity, ports and hostname, a bounded structured
  probe that must observe one exact response, and a preflight credential
  limited to release identity, readiness and the harness host listing. A
  TestFlight build built for that stage refuses to dial anything else, in the
  foreground and from a background refresh alike.
- `POST /auth/credentials` issues a preflight credential from the running hub.
  The credential store is loaded once per process, so a token written to the
  file by a separate CLI would never be honoured by the server that has to
  accept it -- and would be erased by that server's next write.

### Changed

- A harness whose command cannot be built -- a staging host whose API key file
  is missing, unreadable, or not private -- now fails closed with a reason
  (400 on create, 409 on recover) instead of dropping the connection and
  surfacing as a bare 502.

## [0.4.13] - 2026-09-06

### Fixed

- A saturated fleet listing answers instead of leaving the client waiting.
  Pollers arriving while a render is in flight now share it rather than each
  starting their own, and a caller that cannot be served inside the budget gets
  a 503 with `Retry-After`. An unanswered request reads to a client as "ask
  again now", and that retry is what kept the hub saturated until it was
  restarted. This is the server half of #331; the client half shipped
  separately.

## [0.4.12] - 2026-09-06

### Fixed

- The event day summary cache decayed back to a full scan and stayed there. A
  day is marked stale as soon as its partition is re-ingested, the watcher does
  that about hourly, and the backfill only ran at startup -- so the cockpit
  quietly returned to the behaviour the cache exists to prevent, until the next
  restart. It now sweeps every 15 minutes.

## [0.4.11] - 2026-09-06

### Fixed

- The event day summary backfill introduced in 0.4.10 never ran on a live hub.
  It checkpointed between days, and "Cannot CHECKPOINT: there are other write
  transactions active" is the normal state of a running server, so every pass
  failed at the first day. The isolated store it was validated against had no
  concurrent writers to reveal it. Closing the connection is what releases the
  buffers the checkpoint was there for, and the backfill already opens one per
  day. Reads fell back to scanning throughout, so this cost speed rather than
  correctness.

## [0.4.10] - 2026-09-06

### Changed

- The cockpit's observed-activity query no longer rescans raw event partitions
  on every request. A 30-day window pushed 2,488,925 events through a single
  window operator to produce 348 rows, which took the whole 1 GB analytical
  budget; since that budget is instance-wide, it starved the summarizer, the
  advisory worker and host registration alongside it. Each day is now
  summarised once and reused. Measured on the hub at the same budget: a 30-day
  build drops from 11.7 s to 1.0 s, a 7-day one from 1.1 s to 0.3 s, and the
  memory a 30-day build needs from 1 GB to 384 MB. Two concurrent builds both
  succeed where one used to fail; four now get three through where all four
  failed. Output is unchanged.

## [0.4.9] - 2026-09-06

### Fixed

- Cache-read efficiency no longer counts codex cache reads twice. codex reports
  cached input as a subset of input, so summing the two inflated the
  denominator: a session at a healthy 11% was measured as 9.91% and flagged. The
  finding also carried the highest confidence tier, because the numbers behind
  it were exact.
- A completed insight check keeps its answer. The finding row was read pinned to
  the latest analyzer run, so any later pass discarded the check's own evidence
  and turned a settled result into "inconclusive" with nothing to show. A client
  polling a resolved check watched it flip for no visible reason.
- A check-status timeout can no longer interrupt an unrelated query. The
  interrupt was issued after releasing the lock that guards the shared
  control-plane handle, so it could land on whatever acquired that handle next.
- A queued check no longer reports the previous attempt's finish time, which
  dated a check that had not run.
- Insight detail messages reach the reader: a lifecycle error is no longer
  suppressed when the notice it duplicates is off screen, a stale reanalysis
  notice clears on refresh, and a cancelled request no longer draws
  "Could not queue reanalysis." as an error.

### Changed

- Confidence on a cache-efficiency finding now reflects whether span-derived
  data is load-bearing, rather than whether any span record exists at all.
- `tests/test_server_cli.py` no longer gives different answers on identical
  code. Interpreter startup, which every setup-check request pays, has been
  measured spiking from 0.05 s to 11.7 s on the hub; budgets sized for a warm
  machine turned that into a wrong answer rather than a slow pass. See #321.

## [0.4.8] - 2026-09-05

### Changed

- JSON responses are compressed when the client offers it, not only session
  message pages. Measured on the hub: the fleet listing drops from 41.5 KB
  to 7.9 KB, the host list from 6.2 KB to 1.1 KB, the cockpit overview from
  18.6 KB to 2.6 KB, and insights from 23.5 KB to 2.9 KB. The phone polls
  all four, and re-fetches them after every abandoned request, so the saving
  compounds exactly when the server is under pressure (#224).

### Added

- iOS builds carry the signing team and an encryption-exemption
  declaration, so a device build works from a clean checkout and an App
  Store Connect upload does not stall on an unanswered question (#351).
- The Settings screen links to the privacy policy and support pages (#347).
- CI runs the deterministic UI journey and the DroverKit package tests
  rather than only building them (#348).

## [0.4.7] - 2026-09-05

### Fixed

- The session-usage rollup no longer holds the control-plane lock while it
  parses events, so `/harness` stays answerable while a busy session is
  rolled up. Passes of 47 s, 141 s and 238 s were measured on the hub, each
  one a window in which the phone's session list could not load and its
  retries queued behind the same lock. The pass is now three short windows
  per session with the parsing outside them (#334).

## [0.4.6] - 2026-09-04

### Fixed

- Background analytical work no longer starves the control plane of CPU.
  A maintenance connection used to raise DuckDB's instance-wide thread count
  for a cockpit build already running, and nothing arbitrated between them;
  on 2026-09-04 that left the server at 374 percent CPU with every
  `/harness*` endpoint timing out while `/healthz` answered in
  milliseconds, and the client's retries sustained it until a restart. The
  advisory sweep and the native usage rollup now stand aside while a request
  is in flight, bounded so a polled hub still makes progress, and background
  roles can no longer ask for more parallelism than the foreground reader
  they share an instance with (#331).

## [0.4.5] - 2026-09-04

### Fixed

- A transient shortage on the shared analytical DuckDB instance no longer
  does lasting damage. An out-of-memory failure is classified apart from an
  analyzer error, so it always retries and never dead-letters a healthy
  advisory analyzer; recording that failure can no longer abandon the
  analyzers queued behind it; and the cockpit's provider-capacity section
  serves its last good answer marked stale instead of an empty error
  section the client draws as "no accounts" (#328).

## [0.4.4] - 2026-09-04

### Fixed

- A chat turn is no longer lost or sent twice when delivery is uncertain.
  The composer clears immediately and the message appears as a local pending
  bubble; structured turn submission is idempotent on a client-supplied turn
  id, so replaying an ambiguous send returns the original turn instead of
  dispatching a second one. Attachment persistence and session-preference
  updates sit inside that boundary, and the accepted-id map is rebuilt from
  the event ledger after a daemon restart (#326).
- An accepted turn whose stream echo never arrives now becomes retryable
  instead of leaving the composer disabled indefinitely, and the Retry
  control survives an approval, interrupt, or terminate clearing the
  transient hint (#326).

### Added

- The Analytics screen decodes optional projection completeness from the
  server, shows a catch-up notice while historical data is still rebuilding,
  and offers Retry instead of an indefinite loader when the first load fails.
  Malformed or absent projection metadata degrades to no notice rather than
  blanking the screen (#325).

## [0.4.3] - 2026-09-04

### Added

- Imported native-history events now carry typed token columns, and a second
  rollup source folds them into `session_usage` behind a per-source ledger
  with its own watermark, so sessions on hosts without harnessd get real
  token numbers too. Precedence is winner-take-one per session, never a sum,
  and each native date partition is written in one transaction that leaves
  the watermark unadvanced if it fails (#318, closes #311).
- The advisory telemetry aggregate prefers exact session usage over span
  values, falling back to spans when usage is absent (#318).

### Fixed

- `coverage.sources` percentages count every session in the window, so the
  cockpit reports where its numbers came from even when the token-bearing
  sessions carry no repository. Project ranking keeps its own
  attributed-only gate, so the projects list is never ordered by a column
  that is zero for every project (#317, closes #316).
- The iOS client decodes and shows the source coverage block (#317,
  closes #312).
- The empty `agent_events` seed parquet is rewritten atomically, so a
  concurrent reader cannot observe a half-written file (#318).

## [0.4.2] - 2026-09-03

### Added

- Token and cache numbers on the cockpit now come from the harness event
  stream. A `UsageRollupWorker` rolls `harness_events` usage up to one
  `session_usage` row per session in the control-plane store (claude-code
  per-message, codex cumulative; harnesses that report nothing stay
  truthfully unobserved rather than zero), and the activity analytics read
  that row before OTLP spans, per metric. Coverage gains an additive
  `sources` block naming which source supplied tokens and cache, and projects
  rank by tokens only when one source alone covers 80 percent of sessions
  (#309, closes #17).
- Three `/metrics` lines for the rollup: `drover_usage_rollup_sessions_total`,
  `drover_usage_rollup_malformed_payloads_total`, and
  `drover_usage_rollup_last_pass_seconds` (#309).
- A bounded metadata-only eligibility assessment can produce a private,
  fingerprint-bound receipt for a canonical Claude source containing only
  title/name events. Coverage reports the source as
  `source_not_archive_eligible`; changed, duplicate, message-bearing, oversized,
  or noncanonical sources fail closed.

### Changed

- `docs/integrations.md` and `docs/context-store.md` now say tokens come from
  the harness stream and OTLP spans add cost and latency (#309).

## [0.4.1] - 2026-09-01

### Changed

- The 30-day cockpit activity build keeps `raw_data` out of the event dedup
  window; measured 22.6-27.7s down to 9.1-11.1s on a production-store copy,
  with peak RSS down from ~2.5GB to ~1.6GB (#260).
- Analytical DuckDB roles run with a 1GB `memory_limit`, sized for the
  post-#249/#260 loaders; 512MB was measured below the floor (#259).

### Added

- A daily retention sweep bounds `advisory_occurrences` in the control-plane
  store (default 30 days, `advisory_occurrence_retention_days`); a finding's
  newest failing occurrence always survives (#302).
- The advisory worker's control-plane window duration is exported as
  `drover_advisory_plane_window_seconds`, `_max_seconds`, and
  `_windows_total` (#303).

### Fixed

- Hook facts read timestamps through `CAST(... AS TIMESTAMPTZ)`, closing the
  same non-UTC shift fixed for provider facts (#303).

## [0.4.0] - 2026-08-28

### Added


### Fixed

- The fleet's most-polled endpoint (`/harness/hosts`) is cached per render
  variant; previously every poll recomputed the snapshot and opened a
  DuckDB connection under the registry connect lock, stalling for tens of
  seconds behind writers (#289).
- Pre-split control-plane table copies no longer resurrect deliberately
  deleted rows; `harness prune-legacy-tables` removes the copies once the
  control plane demonstrably holds every row (#283, #284).
- Provider token usage is normalized under one arithmetic per harness
  (cumulative vs per-message), counting every billed token class (#287).

## [0.3.7] - 2026-08-21

### Fixed

- Check Again scope probes are globally single-flight. One probe runs at a
  time, same-scope callers share its result, a different scope fails fast, and
  the DuckDB connection is interrupted when the five-second budget expires.
  PR #252 bounded the caller's wait but let the worker run on, so repeated
  views of a scope could stack concurrent snapshots. The result cache is now
  capped at 128 entries, with a shorter cooldown for transient failures.
- harnessd's terminal audit mirror no longer uses an unbounded queue. It is
  bounded at 2,048 records with 128-record batches, counts overflow into
  `drover_harness_dropped_events_total`, and writes `transcript.gap` markers
  so dropped audit records are visible rather than silent.

### Changed

- The cockpit `session_facts` query builds its session-key set once and joins
  the three sources, replacing three `UNION ALL` branches with correlated
  `NOT EXISTS` de-duplication. This is a simplification: benchmarking against
  a copy of a production store showed no significant change in either time or
  peak memory for the 7-day or 30-day window. The cost localized in #260 is
  not the union shape and #260 remains open.

## [0.3.6] - 2026-08-20

### Fixed

- The hub no longer exhausts its memory budget building advisory snapshots.
  The two loaders behind the operational snapshot read `spans_enriched`, a
  view that coalesces three repository columns by running two `DISTINCT`
  scans over every span and two joins over every agent event. Neither loader
  selects any of those three columns, so the whole enrichment was computed
  and discarded on every advisory cycle, and the seven-day predicate could
  not be pushed through it to bound the work. They now read `spans`, the
  relation documented for broad analytics that must not join agent events.
  Measured against the live 129,399-span store at one thread, the routing
  loader fell from 2211 MB peak to 297 MB, and the telemetry loader from
  1773 MB to 224 MB, with identical results. On a 2 GB instance-wide budget
  the routing loader alone had been enough to hit the ceiling, which is what
  took chats and charts down on 2026-08-19
- Refreshing an insight no longer runs an unbounded scope probe. The probe
  had no time budget and was repeated on every poll, so several could stack
  up and starve every other endpoint on the hub. It now runs under a budget
  and its result is briefly remembered instead of recomputed
- A cockpit section whose query fails now shows the last good data it had,
  marked stale, rather than blanking to an empty chart. An empty chart and a
  chart with no data yet looked identical, which is why a server-side
  failure read to the user as "nothing is loading"
- An authentication flow no longer parks two threads for ten minutes. Every
  sign-in leaked a pair of threads that stayed alive until they timed out,
  whether or not the flow had already finished
- Checking a file for a shebang no longer reads the whole executable. A
  large binary on the path cost its full size in memory to answer a question
  about its first two bytes

### Added

- The watcher enforces `processed_retention_days` on the processed spool,
  which was configured but never applied, and a new `drover-server reclaim`
  reports what else on the host is large without deleting any of it

### Changed

- The Python test suite runs in parallel

## [0.3.5] - 2026-08-17

### Fixed

- A finished session no longer says it is waiting for you. A batch of events
  arriving late, which is exactly what a daemon restart produces, could set
  `awaiting` on a session that had already completed and send a notification
  about work that was over. The guard now sits on the writes that set it,
  rather than on the status change, because a late write is the whole problem
- A host that has stopped answering is no longer probed for provider capacity
  every few minutes. Each attempt blocked for ten seconds and logged a warning
  naming an address rather than a host, which had already sent one
  investigation after the wrong machine. Probing resumes on its own as soon as
  the host heartbeats again
- The working directory field no longer reports a healthy host as unreachable
  when it is simply running an older release that has no completion endpoint

### Changed

- Import order is configured and checked in CI, so it stops disagreeing with
  the formatter

### Added

- The working directory field completes against the selected host as you type.
  A partial path lists the directories that actually exist there, debounced so
  a keystroke does not cost a request, and the paths the app already knew
  about (favourites, recent working directories) rank above the live ones
  rather than being replaced by them
- A host can activate an update in place instead of flipping the runtime
  symlink, for a service manager that cannot exec a newly created
  environment. Off by default: every host keeps the symlink flip, which is
  safer, unless `update.activation` says otherwise

### Fixed

- A favourite working directory is no longer offered on hosts where it does
  not exist. An untagged favourite meant "every host", which is not a claim an
  absolute path can honour, so directories present on one machine were
  suggested on all of them. The app now asks each host which of them are real

## [0.3.3] - 2026-08-16

### Fixed

- Listing a host's sessions no longer costs a second per session. The daemon
  read every session row in one query, discarded them, then re-read each row
  one at a time, and each of those reads built a fresh DuckDB instance,
  extensions and all. On a host with 114 sessions that was 115 instance
  constructions per request and `GET /sessions` took 42 to 55 seconds. It now
  takes about 15 milliseconds
- Starting a session no longer times out while a listing is in flight. Creates
  compete for the same control-plane lock, so they queued behind the listing
  above and overran the hub's 120 second budget, leaving the app showing a
  handoff that never resolved for work the host had in fact started
- A reconciled event reaches the hub with the instant it actually happened.
  Timestamps read back from a DuckDB `TIMESTAMP` column are naive local wall
  time, and the wire carried them without an offset, so the hub read them as
  UTC and moved every reconciled event by one UTC offset. The hub is
  idempotent on event id, so a shifted event was never corrected afterwards
- A session keeps the label recording what it was resumed from. It was cleared
  moments after every session started, so the app had nothing to show
- Structured session events are reconciled from DuckDB after a harnessd
  restart, durably and without blocking the liveness path
- The launch picker shows stale hosts as stale rather than hiding them, and
  keeps the selected host while the app is offline

### Added

- A host reports why it refused a self-update: which version, the reason
  (failed smoke test, not quiescent, failed install), and when the refusal
  began. A release that no host will accept was previously visible only by
  reading each host's log on each host
- A refusal clears when the release that caused it is withdrawn, so pulling a
  bad release is enough to clear the fleet rather than leaving every host
  reporting a version nobody is offering any more

## [0.3.2] - 2026-08-15

### Fixed

- A handoff or "continue in a new session" no longer reports failure for work
  the host completed. The hub stopped waiting after 15 seconds and reported
  the timeout as if the host were unreachable, so the app advised retrying a
  create that may already have produced a session. A timeout is now reported
  as such, says the session may exist, and is recorded on the hub
- Repeating a handoff adopts the session the first attempt created rather than
  starting a second agent in the same repository
- A pipeline job that cannot record an attempt is parked instead of retried
  forever. One job produced 916 of the 917 warnings in a single server log,
  never advancing and never giving up, because a job that cannot be leased had
  no other state to move to
- An event re-delivered by a host is stored once instead of raising a duplicate
  key error. The host retains undelivered batches and re-offers them, and the
  hub's guard against that was a check two concurrent deliveries could both
  pass, which accounted for 195 tracebacks in one server log
- A completion that arrives twice no longer queues a second recap for work
  already summarised
- A failed dedup-key lookup during ingestion says so, rather than silently
  reporting that nothing in the batch is a duplicate
- Chat no longer renders its unreachable message one letter per line. An empty
  transcript sized itself to its padding, and the failure notice inherited a
  container about one character wide
- Terminal's Retry acts on a connection attempt that is already running.
  Previously it only cancelled a pending backoff, so pressing it at the moment
  it was most likely to be pressed did nothing
- Observed cost reads `Not reported` rather than `$0.00` when no session in the
  window reported one, and the Analytics screen labels it API-billed and shows
  its coverage, as the fleet card already did
- The session title has room again: its subtitle no longer spends width on a
  percentage derived from the two numbers printed beside it

### Documentation

- Favourite working directories, including how to scope one to a single host
- What `[server] metrics_host` means for local `drover-server` commands
- Architecture diagram regenerated for v0.3

### Fixed

- Observed cost of zero is no longer rendered as `$0.00` when nothing measured
  it. Drover computes no prices: `cost_usd` is whatever the harness reported,
  and subscription-billed usage has none to report, so a fleet running entirely
  on subscriptions saw a confident zero where the honest answer is that no
  session reported a cost
- The analytics screen labels its cost figure `API-billed` and prints cost
  coverage beside token coverage, matching the cockpit card. It had shown a
  5%-coverage figure under the label `API cost` with nothing to qualify it

## [0.3.1] - 2026-08-15

### Fixed

- `drover-server` subcommands reach the hub at the address it is actually
  bound to. The CLI assumed loopback while the server binds
  `[server].metrics_host`, which the installer sets to the address it detected
  for the phone, so on a machine with a LAN address every command that calls
  the hub reported "could not reach drover-server, is it running?" about a
  server that was running and serving
- The release workflow's install verification probes that same configured
  address. It had been curling loopback regardless, which is why it failed for
  v0.2.0 and v0.3.0 against installs that were working

## [0.3.0] - 2026-08-15

### Added

- DeepSeek Harness (`dsh`) as a launchable structured harness, with model
  catalog, authentication flow, and turn correlation
- iOS launch sheet reworked: host and harness snapshot loading, working
  directory suggestions, and model and reasoning-effort preferences carried
  into a new session
- `/readyz` readiness endpoint that queries the database handle rather than
  checking that the process is alive
- Favorite working directories can name the host they belong to, so a path
  that exists on one machine is no longer offered for the rest of the fleet

### Changed

- OpenClaw is no longer offered as a session target. It shipped as a default
  preset with no structured driver, so selecting it produced an internal
  error. Its telemetry ingestion is unchanged: it remains an observed agent
- Antigravity turns are no longer capped at five minutes. `agy --print`
  defaults to a 5m0s deadline, which ended long turns mid-command and reported
  the result as a completed turn followed by a bare exit code

### Fixed

- DuckDB snapshots are captured atomically. The copy read a live store as
  three unsynchronized filesystem operations, so a snapshot taken during a
  write corrupted the handle: the server stayed up, the process looked
  healthy, and every query failed
- Snapshot copies are written beside the store rather than into the system
  temporary directory, and orphans are swept at startup. They had accumulated
  at roughly one directory every fifteen minutes and filled the boot volume
- The readiness probe can no longer wedge the server. It waited on the
  control-plane lock unbounded while holding its own cache lock, so one
  blocked probe stacked every later request behind it
- A Codex session no longer dies at argument parsing when the prompt begins
  with a dash. A prompt written as a markdown bullet list was read as a
  command-line option and the turn exited before it began
- Opening Chat or Terminal with the fleet unreachable now says so and offers a
  retry. Both screens showed an indefinite spinner and no message: the
  reconnecting indicator was gated on having connected at least once, so a
  first load that never landed could not report anything at all
- A DeepSeek session refuses to launch against a working directory that does
  not exist, rather than anchoring its sandbox workspace to an unusable root
- A non-zero harness exit is recorded on the host with the turn and return
  code, and the app distinguishes a process that failed after completing its
  turn from one that failed before producing anything

### Added in earlier development

- Fleet management cockpit for iOS app with grouped live sessions by host
- Context store with Parquet facts and DuckDB derived views
- `context_containers` table for confidence-aware context grouping
- Redaction policy support (`summary_md`, `redaction_policy` fields)
- `context_id` identity for context containers without source repositories
- `curated_context_records` and `curated_context_provenance` tables
- Host registry separation into `drover.registry.duckdb` for command-plane operations
- Pairing code-based device/host credential provisioning
- Per-credential revocation (`drover-server credentials revoke`)
- Legacy shared token deprecation path (enabled by default, configurable)
- Multi-host support with `drover-server pair-host` command
- Relay host dial-out protocol for NAT-traversal
- Local cockpit HTTP surface on port 7080
- MCP `/mcp` endpoint with `drover_*` tools:
  - `drover_fleet_status`: Current fleet state and host health
  - `drover_handoff`: Transfer work between agents
  - `drover_recent_sessions`: Query recent session metadata
  - `drover_session_replay`: Full turn-by-turn session logs
  - `drover_search_fleet`: Indexed search across sessions
  - `drover_recall_project`: Project briefs and summaries
  - `drover_context_query`: Structured context queries
  - `drover_files_touched`: File change history by session
  - `drover_data_quality`: Context quality and adoption metrics
- Agent CLI ingestion from Claude Code, Codex, Antigravity (agy), OpenClaw, Hermes
- OpenTelemetry span ingestion (AgentWeave compatible)
- Pull request event tracking (`drover_server pr`)
- Repository attribution and path mapping via `DROVER_REPO_ROOTS_JSON`
- Flexible repository name mapping for Claude Code ambiguity: `DROVER_CLAUDE_CWD_MAP`
- General workspace configuration: `DROVER_GENERAL_WORKSPACE_ROOTS`
- DuckDB views for session relationships: `sessions`, `active_sessions`, `session_links`
- Integration views: `openclaw_span_links`, `active_sessions_enriched`
- Advisory snapshot for query optimization: `co_pilot_advisory_snapshot`
- Session summarization worker with configurable backends (`harness`, `hybrid`, `cloud`)
- Session embeddings via Ollama or OpenAI-compatible endpoints
- Brief generation with quality snapshot generation
- Project brief synthesis from recent sessions
- Decision extraction from agent activity
- Pipeline provenance table for job intent and execution tracking
- Optional Redis Streams for worker coordination, retries, and backpressure
- Local storage of large payloads in `raw_objects/` by URI reference
- Health check endpoint at `/healthz`
- Server status CLI: `drover-server status`
- Doctor diagnostic CLI: `drover-server doctor`
- MCP tools listing: `drover-server mcp tools`
- Context store CLI: `drover-server context` commands

### Changed

- Split context storage: lakehouse `drover.duckdb` for facts, `drover.registry.duckdb` for command state
- Improved query performance by isolating DuckDB instances (command plane vs lakehouse)
- Token storage: server stores `sha256("drover-cred-v1\0" + token)` hash, never raw tokens
- Pairing code scope enforcement (device vs host codes distinct)
- Agent adoption registry configuration via `DROVER_AGENT_ADOPTION_JSON`
- Compatibility layer for historical `nexus.*` telemetry (readable, not re-emitted)
- Default behavior: legacy shared-token fallback remains enabled for upgrade
  compatibility until an operator disables it
- Installer now detects existing Drover services and refuses to start (safety check)
- Default bind addresses: all central listeners default to `127.0.0.1`

### Deprecated

- Shared bearer token authentication (legacy mode is enabled by default and
  remains available until an operator disables it)
- Compatibility job tables: `summarize_jobs`, `brief_jobs`, `embed_jobs`, `span_embed_jobs` (replaced by pipeline ledger)

### Fixed

- `/readyz` now probes both DuckDB stores and answers `503`, naming the failing
  store, when a handle can no longer be queried; it previously answered
  `200 ok` while every query against an invalidated database failed
- Reduced timeout spikes on `/harness` endpoints during background scans
- Fixed session link reconciliation for integration-specific event namespaces
- Corrected repository attribution for cross-machine path collection
- Fixed pairing code expiry timing and rejection behavior
- Improved error handling for unauthenticated `/harness/hosts` responses

### Security

- Hardened pairing endpoint against brute-force: 5 failures/minute limit, identical responses for unknown/expired/used codes
- Token hash algorithm: `sha256("drover-cred-v1\0" + token)` with explicit prefix for collision protection
- Pairing codes single-use with scoped redemption (device never becomes host credential)
- Default configuration: localhost-only bindings, no public exposure
- Credential file permissions: `0600` for `credentials.json` and `api_token`
- Revocation workflow: `drover-server credentials revoke <id>`

### Documentation

- New documentation: [Threat Model](docs/threat-model.md)
- New documentation: [Context Store](docs/context-store.md) comprehensive overview
- Improved [Security](docs/security.md) documentation with network checklist
- Added [GitHub Actions Runner](docs/github-actions-runner.md) security runbook
- Expanded [Integrations](docs/integrations.md) with agent ID patterns and path mapping
- Added [Multi-Host](docs/multi-host.md) setup instructions

## [0.2.0] - 2026-08-13

### Added

- Initial public release with core fleet management capabilities
- iOS client application with fleet, cockpit, and analytics views
- `drover-server` central process with HTTP and WebSocket API
- `drover-harnessd` per-host daemon for agent process management
- Local context store using Parquet and DuckDB
- Basic session tracking and harness host registry
- Bearer token authentication with shared API token
- Local-only networking (localhost and configurable private bind)
- Source distribution via GitHub repository
- Automated installer script with checksum verification
- Python 3.11+ development environment with uv

### Changed

- N/A (initial release)

### Fixed

- N/A (initial release)

### Removed

- N/A (initial release)

### Security

- Initial security posture: single-operator trust model, token hashing, pairing codes
- Basic security documentation (Security.md)

## Release Notes

### Version 0.2.0

Version 0.2.0 is focused on:

1. **Fleet Cockpit**: Visual management of coding agent sessions from mobile or desktop
2. **Context Store**: Durable, queryable agent activity with summaries and embeddings
3. **iOS App**: Native client for on-device fleet control and handoff
4. **Local-First**: Fully self-hosted, no external services required
5. **Multi-Host**: Support for multiple machines on private network
6. **Docker-Ready**: Can be containerized for deployment flexibility

### Known Limitations

- Single trusted operator only (no multi-tenant)
- No RBAC or fine-grained permissions
- Agents execute with full host privileges (no sandboxing)
- Tailscale Funnel and public exposure not supported

### Upgrade Path

From source (previous development versions):

```bash
# Use --adopt flag to migrate existing installation
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -- --adopt
```

From token-based deployment:

```bash
# Pair a device or add a host with a new credential
drover-server pair
drover-server pair-host --name <host-id>
```

## Versioning Policy

Drover follows Semantic Versioning:

- `MAJOR`: Breaking changes (migration required)
- `MINOR`: New features, backwards compatible
- `PATCH`: Bug fixes, backwards compatible

Breaking changes will be documented in the CHANGELOG with explicit migration guide.

## Attribution

Special thanks to all contributors:

- [@arniesaha](https://github.com/arniesaha): Original author and maintainer

See the repository for full contribution history and commit log.
