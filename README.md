# Drover

<p align="center">
  <img src="docs/assets/drover-hero.png" alt="Drover watching over a fleet of coding-agent terminals" width="480">
</p>

> Drive your coding-agent fleet from your pocket.

## What Drover is

Drover is a local-first cockpit and context store for a personal fleet of CLI
coding agents. It runs sessions on Claude Code, Codex, Antigravity (agy),
DeepSeek Harness, and compatible harnesses on machines you control, and ingests
activity from others, including OpenClaw and Hermes, as context. A native iOS
client lets you inspect sessions, answer prompts, send turns, hand work off,
and attach to a terminal.

Drover is self-hosted software for one trusted operator. The supported network
boundary is localhost, a private LAN, or a private Tailscale network.
It does not require a Drover cloud service.

## Screenshots

<p align="center">
  <img src="docs/assets/screenshots/ios-fleet.png" alt="Drover fleet view with active sessions and provider capacity" width="160">
  <img src="docs/assets/screenshots/ios-launch.png" alt="Drover new session controls" width="160">
  <img src="docs/assets/screenshots/ios-cockpit.png" alt="Drover cockpit view with observed activity and busiest projects" width="160">
  <img src="docs/assets/screenshots/ios-analytics.png" alt="Drover analytics view with provider capacity and observed usage" width="160">
</p>

The dark-mode fleet view groups live work by host and keeps provider capacity
within reach. The launch sheet selects a host and harness, checks
authentication, and carries model and reasoning preferences into a new
session. The cockpit summarizes observed activity and projects, while
Analytics expands provider-reported quota windows and usage distributions.

## How it works

![Drover command and context planes](docs/drover-architecture.png)

- The **command plane** connects the iOS app to `drover-server` and per-host
  `drover-harnessd` daemons for session control, structured chat, approvals,
  handoff, and terminal streaming.
- The **context plane** collects durable agent events into local Parquet and
  DuckDB storage, then derives summaries, project briefs, and embeddings for
  recall. OpenTelemetry span ingestion is an optional integration, off by
  default.
- The reference hub runs a PostgreSQL control store, migrated from DuckDB on
  2026-09-21; its analytical lake and every host-local spool stay DuckDB.
- A new central installation uses a **PostgreSQL control store** for fleet
  serving. Existing DuckDB configurations and every host daemon's local spool
  remain compatible until an operator completes the explicit migration. The analytics role exports
  central events into immutable Parquet batches, records their manifest, then
  acknowledges them; acknowledgement gates retention and replay.
- The **MCP surface** exposes that context to coding agents as `drover_*` tools.

See [Architecture](docs/architecture.md) for the component boundaries and
[Context Store](docs/context-store.md) for the data model. Operators planning
an explicit central serving store should read [PostgreSQL control store](docs/postgresql-control-store.md).

## Quickstart

Requires macOS or Linux with Python 3.11+.

Choose one explicit central control-store mode:

```bash
# Existing PostgreSQL you operate (backwards compatible when the DSN is set)
export DROVER_CONTROL_DSN='postgresql://USER:PASSWORD@HOST/DATABASE'
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --control-store existing

# Fresh trusted central server: reuse Docker/OrbStack's already-running Docker CLI
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --control-store managed
```

Existing mode validates connectivity and readiness without creating a database. Managed mode creates or reuses only a labeled Drover PostgreSQL 17 container, bound to `127.0.0.1:54329`, with a named Drover-owned volume. Drover never installs Docker, OrbStack, Podman, or another runtime; an unavailable CLI/daemon stops with guidance. The server and harnessd remain native processes.

The installer initializes the control store only after PostgreSQL is healthy, then starts the server and local harness host. It stores the DSN in `~/.drover/server.env` mode `0600`, never TOML or service arguments.

It also links `drover-server` into `~/.local/bin`, so it is on your PATH. When
that directory is not on your PATH, the installer says so and prints the line
to add.

Use `install.sh --dry-run` for an offline, non-mutating action preview. Before
committing a clean machine to an installation, use
`install.sh --verify-release` to download the selected release into a
disposable runtime, verify the published checksums, and confirm the
PostgreSQL-default installer contract without writing `~/.drover`.

Add another machine with the one-liner printed by
`drover-server pair-host --name <host>`.

## Recall and planned backups

`drover_recall_bundle` serves bounded hub context: keyword matches, summaries,
project briefs, and open loops. Its existing response envelope remains stable
and includes `sources: ["hub"]`.

Pond is deprecated and removed. Legacy `[archive]` config keys are accepted
and ignored with one warning per process; remove that section from your config.
Native history `archive source-inventory` and `archive source-eligibility`
commands remain available for private local audits.

DuckLake replaces Pond in **Phase 4 (#481)**. The planned R2 backup boundary is
DuckLake catalog+files generations plus a Postgres dump, with receipts and
verification. This backup implementation does not exist yet; see
[Backup design](docs/backup.md).

Run the installer as `install.sh --dry-run` to preview its actions without
changing anything.

Continue with [Getting Started](docs/getting-started.md) for the source-build
path, verification, private Tailscale setup, and optional context ingestion.

## Context store

Raw agent events are durable facts. Drover stores them as partitioned Parquet,
exposes normalized DuckDB views, and keeps mutable derived context such as
summaries, briefs, embeddings, and job provenance in DuckDB. Derived records
always retain links back to source sessions. Spans from an external OTLP
producer are kept the same way when the
[optional span integration](docs/optional-span-integration.md) is enabled.

The model and its compatibility boundary are documented in
[Context Store](docs/context-store.md). Historical telemetry may retain
`nexus.*` attributes; new public APIs, commands, and MCP tools use Drover.

## Supported networking and security

- Supported: localhost, a trusted private LAN, and a private Tailscale network.
- Not supported: Tailscale Funnel or any public-internet exposure.
- Authentication: individually issued device and host bearer credentials; the
  legacy shared token remains available for upgrades until explicitly disabled.
- Host credentials are not yet bound to a host identity, so any host
  credential can act as any host
  ([#13](https://github.com/arniesaha/drover/issues/13)).
- Not provided: multi-user isolation, RBAC, SSO, or a hosted control plane.

Read [Security](docs/security.md) before exposing a listener beyond localhost,
and [Multi-Host](docs/multi-host.md) before adding another machine.

## Build the iOS app

The iOS app ships from source. It requires Xcode 16+, iOS 18+, and XcodeGen.

```bash
brew install xcodegen
cd apps/drover
xcodegen generate
open Drover.xcodeproj
```

Select your Apple development team and run the `Drover` scheme on a simulator
or connected iPhone. See the [iOS build guide](apps/drover/README.md) for tests,
device signing, and server configuration.

## Documentation

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Harness Adapter Architecture](docs/harness-adapter-architecture.md)
- [ADR 0001: Harness Adapter Capability Registry](docs/adr/0001-harness-adapter-capability-registry.md)
- [Factory Observer Delegation Bridge](docs/factory-observer-bridge.md)
- [iOS app](apps/drover/README.md) and [TestFlight runbook](apps/drover/docs/internal-testflight-runbook.md)
- [Context Store](docs/context-store.md)
- [PostgreSQL Control Store](docs/postgresql-control-store.md)
- [Integrations](docs/integrations.md)
- [Multi-Host](docs/multi-host.md)
- [Security](docs/security.md)
- [GitHub Actions Runner](docs/github-actions-runner.md)
- [Agent Skills](skills/README.md)

## Status and limitations

Drover is source-distributed software for technical users operating a trusted
personal fleet; the current release line is listed in the
[changelog](CHANGELOG.md). The Python server and native iOS client are
functional. The iOS app is built from source or distributed to testers through
the TestFlight production lane; it is not on the public App Store. Push notifications work when the
hub is configured with an APNs key (see the
[iOS guide](apps/drover/README.md#push-notifications)); otherwise the app falls
back to best-effort local notifications. Packaging, host-bound credential
enforcement, and broader context interchange standards remain future work.

See [open issues](https://github.com/arniesaha/drover/issues) for current bugs
and accepted user-visible work.

## License

Apache-2.0. See [LICENSE](LICENSE).
