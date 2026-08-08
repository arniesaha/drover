# Getting Started

The first supported setup runs `drover-server`, `drover-harnessd`, and the
agent CLI on one machine. Add other trusted machines only after the local path
works.

## Prerequisites

- macOS or Linux with Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- At least one supported agent CLI installed and signed in
- Xcode 16+ and XcodeGen only if you are building the iOS app

## 1. Install

```bash
git clone https://github.com/arniesaha/drover.git
cd drover
uv sync --extra dev
uv run drover-server init
```

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

## 2. Start The Central Process

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

## 3. Start A Local Harness Host

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

## 4. Connect The iOS App

Build the app using [the source-build guide](../apps/drover/README.md). In app
settings, use:

- Server URL: `http://127.0.0.1:7080` for the simulator on the same Mac.
- API token: the contents of `~/.drover/api_token`.

For a physical iPhone, use a private LAN address or a private Tailscale address
that reaches the server machine. Do not use Tailscale Funnel.

## 5. Add Private Tailscale Access

Install Tailscale on the server machine and iPhone, sign both into the same
tailnet, and verify the phone can reach the machine's private Tailscale address.
Keep port `7080` private to the tailnet.

Central listeners bind to `127.0.0.1` by default. Bind only the cockpit HTTP
surface to the private interface when the iOS app must connect from another
device:

```bash
uv run drover-server run --metrics-host 0.0.0.0
```

Use `http://<private-tailscale-ip>:7080` in the app. The bearer token is still
required. Review [Security](security.md) before changing bind addresses.

OTLP and MCP remain loopback-only unless you also set `--otlp-host` or
`--mcp-host` explicitly.

## 6. Verify The Context Surface

```bash
uv run drover-server status
uv run drover-server doctor
uv run drover-server mcp tools
```

Agent-log collection and OTLP ingestion are optional extensions. See
[Integrations](integrations.md) and [Context Store](context-store.md) after the
command plane works end to end.
