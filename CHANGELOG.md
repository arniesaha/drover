# Changelog

All notable changes to Drover will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Three local-only, read-only archive inventory commands capture private
  native and Pond identity manifests and compare candidate coverage. They
  create only new owner-readable `0600` artifacts, refuse unsafe inputs and
  existing outputs, emit aggregate-only stdout, and exit `2` when conservative
  duplicate or readiness checks block progression. They do not sync, upload,
  configure remote storage, certify content, mutate the archive, or authorize
  deletion.

## [0.4.0] - 2026-08-28

### Added

- Optional cross-session recall archive: Drover reads a local Pond
  (pinned v0.16.3) HTTP endpoint, and the new `drover_recall_bundle` MCP
  tool composes bounded, source-cited evidence from archived native
  sessions alongside Drover's own summaries, briefs, and open loops.
  Disabled by default; loopback-only; responses are streamed and
  byte-capped before decoding, and bundles compose one at a time. An
  archive outage or the disabled state degrades to Drover-only context
  with a structured warning -- it never blocks startup or a live session.
- `[archive]` configuration block with strict validation (loopback URL,
  bounded timeouts, limits, and response caps).
- CI job proving the archive client against the real checksum-verified
  Pond binary, so an upstream contract drift breaks the build rather than
  an operator's hub.

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
