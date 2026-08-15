# Changelog

All notable changes to Drover will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
