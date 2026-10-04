# Changelog

All notable changes to Drover will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
