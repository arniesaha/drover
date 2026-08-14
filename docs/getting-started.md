# Getting Started

The first supported setup runs `drover-server`, `drover-harnessd`, and the
agent CLI on one machine. Add other trusted machines only after the local path
works.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash
```

That installs a verified release into `~/.drover/runtime/<version>`, starts
both services, detects an address your phone can reach, and prints a QR code
to pair with. It refuses to run if it finds a Drover service it did not
create; pass `--adopt` to migrate an existing source install.

Useful flags:

- `--dry-run` prints exactly what it would do and changes nothing.
- `--url <host:port>` overrides address detection. Private addresses only.
- `--version vX.Y.Z` pins a release instead of taking the latest.
- `--join <drover://...>` adds this machine to an existing fleet. See
  [Multi-Host](multi-host.md).

Adding a second machine is one pasted command, printed by
`drover-server pair-host` on the machine that already runs the hub.

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
uv run drover-server init
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
cockpit on port `7080`:

```toml
[server]
otlp_grpc_port = 4317
mcp_http_port = 7077
metrics_http_port = 7080
```

The server enables bearer-token authentication by default. On first start it
creates `~/.drover/api_token` with mode `0600` unless `DROVER_API_TOKEN` or an
explicit config value is provided.

### 2. Start The Central Process

```bash
uv run drover-server run
```

This starts the incoming-event watcher, local context store, MCP endpoint, and
the port `7080` HTTP surface used by the app. Optional summarization and
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

Scan the QR code with the app. The app receives its own token and stores it in
the iOS Keychain. Nothing is typed by hand. The code is single use and expires
after ten minutes.

The QR points at `[server] advertised_url` from `~/.drover/config.toml`. Set
that to a private LAN address or a private Tailscale address before pairing a
physical iPhone. While it is unset, the command prints the loopback address and
warns that only the simulator on this Mac can reach it. Do not use Tailscale
Funnel.

Manual URL and token entry stays available in app settings as the recovery path
for when a camera is unavailable.

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

Review [Security](security.md) before changing bind addresses.

OTLP and MCP remain loopback-only unless you also set `--otlp-host` or
`--mcp-host` explicitly.

## Verify The Context Surface

```bash
uv run drover-server status
uv run drover-server doctor
uv run drover-server mcp tools
```

Agent-log collection and OTLP ingestion are optional extensions. See
[Integrations](integrations.md) and [Context Store](context-store.md) after the
command plane works end to end.
