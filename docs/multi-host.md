# Multi-Host Drover

Start with the one-machine path in [Getting Started](getting-started.md). A
multi-host fleet adds trusted machines over a private LAN or private Tailscale
network; it does not change Drover's single-operator trust model.

## Topology

```text
                       private LAN or tailnet

 iOS app  ───────────────► drover-server :7080
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
          drover-harnessd :7081       outbound relay connection
             direct host                   relay host
```

The central server presents one fleet API. Each `drover-harnessd` owns local
agent processes, structured sessions, and terminal I/O on its machine.

`drover-server` binds to localhost by default. Before adding a direct or relay
host, start its cockpit listener on the intended private interface:

```bash
uv run drover-server run --metrics-host 0.0.0.0
```

Only add `--mcp-host` or `--otlp-host` when remote agents or collectors need
those listeners too.

## Direct Hosts

Use a direct host when the central server can reach its private address. Bind
the daemon to the private interface and advertise the same reachable URL:

```bash
uv run drover-harnessd \
  --host-id build-mac \
  --display-name "Build Mac" \
  --kind macos \
  --listen 0.0.0.0:7081 \
  --local-url http://<private-host-address>:7081 \
  --central-url http://<private-central-address>:7080
```

Restrict port `7081` to the trusted LAN or tailnet with host firewall rules.
Do not advertise a public URL.

## Relay Hosts

Use relay mode when inbound access to the host is undesirable or unavailable.
The daemon opens an outbound WebSocket to the central server, which proxies
fleet requests over that connection:

```bash
uv run drover-harnessd \
  --host-id laptop \
  --display-name "Laptop" \
  --kind macos \
  --central-url http://<private-central-address>:7080 \
  --relay
```

The central address must still be private to your LAN or tailnet. Do not use
Tailscale Funnel. A relay host should not set `--local-url` or
`--tailscale-url`; its outbound connection is the route.

## Authentication

The central server and every host currently share one bearer token. The daemon
resolves it from `--host-token`, `DROVER_API_TOKEN`, or
`~/.drover/api_token`. Prefer the environment or token file so it does not
appear in shell history.

Because v0.1 does not bind a credential to a specific `host-id`, every host
belongs to the same trust domain. Do not enroll a machine you do not fully
control. See [Security](security.md).

## Validation

From the central machine:

```bash
curl -fsS \
  -H "Authorization: Bearer $(cat ~/.drover/api_token)" \
  http://127.0.0.1:7080/harness/hosts
```

Confirm each host reports the expected connection type and a current
heartbeat. Then connect through the iOS app, open a session on each host, send
a turn, and verify terminal attach only on machines where you intend to allow
it.

## Service Installation

The repository includes launchd and systemd templates under `scripts/`. Treat
them as starting points: review paths, bind addresses, environment variables,
and token-file permissions before loading a service. Source-build service
packaging remains in progress for v0.1.
