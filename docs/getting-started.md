# Getting Started

The first supported setup runs `drover-server`, `drover-harnessd`, and an
authenticated supported agent CLI on one computer. Add other trusted machines
only after you can complete a small task in a project the agent can read.

## Install

```bash
# Existing operator-owned PostgreSQL
export DROVER_CONTROL_DSN='postgresql://USER:PASSWORD@HOST/DATABASE'
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --control-store existing

# Or a fresh central server with an already-running Docker/OrbStack Docker CLI
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --control-store managed
```

Existing mode validates the reachable database and preserves the existing-DSN deployment model. Managed mode creates/reuses a Drover-owned PostgreSQL 17 container, then waits for health before it initializes the empty control store. It never installs a container runtime. The installer then installs a verified release into
`~/.drover/runtime/<version>`, starts the hub and local harness, detects a
private address your phone can reach, and prints a QR code to pair with. It
stores the DSN in a private service environment file, not `config.toml` or
service logs. It refuses to run if it finds a Drover service it did not create;
pass `--adopt` to migrate an existing source install.

Useful flags:

- `--control-store existing|managed` selects the central PostgreSQL mode. In automation this is required unless `DROVER_CONTROL_DSN` is supplied, which is an explicit backwards-compatible existing mode.
- `--dry-run` previews the selected mode and mutations without contacting or creating containers.
- `--verify-release` downloads the selected release into a disposable runtime,
  verifies its published checksums, and proves it supports the PostgreSQL-default
  installer contract without writing `~/.drover`. Use this before a real install
  when you need release-feed compatibility evidence; it intentionally needs
  network access and is separate from the offline mutation preview.
- `--url <host:port>` overrides address detection. Private addresses only.
- `--version vX.Y.Z` pins a release instead of taking the latest. The pinned
  release must support PostgreSQL-default setup; older releases are rejected
  before the installer changes the active runtime.

The PostgreSQL database is an operator-managed dependency. Drover does not
install or manage its server process. [PostgreSQL control store](postgresql-control-store.md)
describes provisioning, initialization, explicit DuckDB compatibility, and
offline migration.

Add a second machine only after this first-computer path works. The existing
hub prints its one pasted setup command with `drover-server pair-host`; see
[Multi-Host](multi-host.md).

## Prerequisites

- macOS or Linux with Python 3.11+
- At least one supported agent CLI installed and signed in
- Xcode 16+ and XcodeGen only if you are building the iOS app

The installer brings its own [uv](https://docs.astral.sh/uv/) if you do not
have it.

## Build From Source

Contributors, and anyone who would rather not run an installer, can do the
same thing by hand.

### 1. Install

```bash
git clone https://github.com/arniesaha/drover.git
cd drover
uv sync --extra dev
git config core.hooksPath .githooks
export DROVER_CONTROL_DSN='postgresql://USER:PASSWORD@HOST/DATABASE'
uv run drover-server init
uv run drover-server control-store init
```

`git config core.hooksPath .githooks` is a one-time step per clone, and git
worktrees share the repository configuration, so setting it once covers all of
them. It enables the pre-commit hook, which runs the public release audit in
`scripts/check_public_release.py` over the files you have staged and refuses a
commit that would publish a private value or a planning document. CI runs the
same audit, but only after a push, and only over what is already committed, so
without the hook the author gets no signal and everyone else gets a red main.
Use `git commit --no-verify` to bypass the hook deliberately.

The generated config lives at `~/.drover/config.toml` and enables the local
cockpit on port `7080`. For a fresh central installation it also names the
PostgreSQL control store; it contains the DSN environment variable name, never
the DSN itself:

```toml
[control_store]
backend = "postgres"
dsn_env = "DROVER_CONTROL_DSN"

[server]
mcp_http_port = 7077
metrics_http_port = 7080
```

Span ingestion over OTLP is an optional integration and is off by default; no
core feature needs it. See [Optional span integration](optional-span-integration.md).

The server enables bearer-token authentication by default. On first start it
creates `~/.drover/api_token` with mode `0600` unless `DROVER_API_TOKEN` or an
explicit config value is provided.

### 2. Start The Central Process

```bash
uv run drover-server run
```

This starts the incoming-event watcher, local context store, MCP endpoint, and
the port `7080` HTTP surface used by the app. A source-managed process needs
`DROVER_CONTROL_DSN` in its environment every time it starts. Optional summarization and
embedding workers remain idle when no model backend is configured.

Verify authenticated access from another terminal:

```bash
curl -fsS \
  -H "Authorization: Bearer $(cat ~/.drover/api_token)" \
  http://127.0.0.1:7080/harness/hosts
```

### 3. Start A Local Harness Host

```bash
uv run drover-harnessd \
  --host-id local \
  --display-name "Local Mac" \
  --kind macos \
  --listen 127.0.0.1:7081 \
  --local-url http://127.0.0.1:7081 \
  --central-url http://127.0.0.1:7080
```

`drover-harnessd` owns the local agent processes and terminal sessions. The
central server owns the fleet API and proxies app requests to the daemon.

Run the authenticated hosts request again and confirm the local host appears.

## Connect The iOS App

Build the app using [the source-build guide](../apps/drover/README.md), then
pair it:

```bash
uv run drover-server pair
```

Scan the QR code with the app. The app receives its own credential and stores
it in the iOS Keychain. Nothing is typed by hand. The code is single use and
expires after ten minutes.

The QR points at `[server] advertised_url` from `~/.drover/config.toml`. Set
that to a private LAN address or a private Tailscale address before pairing a
physical iPhone. While it is unset, the command prints the loopback address and
warns that only the simulator on this Mac can reach it. Do not use Tailscale
Funnel.

If the camera is unavailable, use **Or enter it by hand** in **Pair & Connect**
and enter the server URL and pairing code from `drover-server pair`. This keeps
manual pairing as the recovery path without copying a shared bearer token.

After pairing, choose an authenticated supported agent and a readable project,
then send a small task. If that journey is not ready, run this optional
terminal-side diagnosis after selecting the host, harness, and project:

```bash
drover-server setup-check --host HOST --harness HARNESS --project PROJECT
```

It reports bounded, read-only recovery categories and actions. Add `--json`
when a structured result is useful; it does not change services or credentials.

## Add Private Tailscale Access

Install Tailscale on the server machine and iPhone, sign both into the same
tailnet, and verify the phone can reach the machine's private Tailscale address.
Keep port `7080` private to the tailnet.

Central listeners bind to `127.0.0.1` by default. The installer detects a
private address and writes both keys for you; set them by hand only on a
source install:

```toml
[server]
metrics_host = "0.0.0.0"
advertised_url = "100.64.0.10:7080"
```

`metrics_host` is the bind, and `advertised_url` is what the pairing QR points
at. Both live in config rather than only in a command line, because a
regenerated service unit that dropped the flag would silently revert the
server to loopback, and that failure is invisible until the app stops loading.
An explicit `--metrics-host` still overrides the config value.

`drover-server` subcommands that talk to a running hub, such as `pair`,
`pair-host` and `credentials`, call it at this address too. Setting
`metrics_host` to a single private address rather than a wildcard means
loopback is not served, so a command assuming `127.0.0.1` would report that the
server is not running while it is serving normally.

Review [Security](security.md) before changing bind addresses.

### Hosts With More Than One Network (LAN + VPN)

The installer writes the single address it detected, which on a machine with
Tailscale or another overlay VPN is the VPN address. That address exists only
while the VPN client is running. When it disappears the hub keeps running and
logs `cockpit HTTP cannot bind ... retrying` (at startup) or `bind address ...
is no longer assigned` (while serving), retries with backoff capped at 30
seconds, and serves again by itself once the address returns. Nothing reaches
it in the meantime, though, including loopback and the LAN. A local
`curl http://<vpn-address>:7080/healthz` in that window times out rather than
being refused, because the address no longer belongs to the machine and the
request is routed off it.

On a hub that should stay reachable when one network goes away, bind every
interface and keep pointing the phone at the address you want it to use:

```toml
[server]
metrics_host = "0.0.0.0"
advertised_url = "100.64.0.10:7080"
```

Every route except `/healthz`, `/readyz`, login, and the single-use pairing
and join-probe routes requires the API token or a paired device credential
on every interface. A wildcard bind does expose those public routes and the
login page on the LAN and any other network the machine joins, so add the
host-firewall or Tailscale-policy restriction from the
[Security](security.md#network-checklist) checklist, and avoid a wildcard bind
on a laptop that joins untrusted networks. Keep a single address there and
accept that the hub is unreachable while that network is down.

MCP remains loopback-only unless you also set `--mcp-host` explicitly. The
optional OTLP receiver, when enabled, is loopback-only unless you set
`--otlp-host`.

## Verify The Context Surface

```bash
uv run drover-server status
uv run drover-server doctor
uv run drover-server mcp tools
```

Agent-log collection and OTLP ingestion are optional extensions. See
[Integrations](integrations.md) and [Context Store](context-store.md) after the
command plane works end to end.
