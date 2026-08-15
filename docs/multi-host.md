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

## Adding A Machine

On the machine that already runs the hub:

```bash
drover-server pair-host --name build-mac
```

That prints a one-liner to paste on the new machine, carrying a single-use
code that expires in fifteen minutes:

```bash
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh \
  | bash -s -- --join 'drover://100.64.0.10:7080?v=1&code=H3TW-9KQ2'
```

The joining machine installs `drover-harnessd` only, never a second hub. It
asks whether the hub can reach it back, then registers as a direct host if so
and a relay host if not, so nobody has to know in advance which applies. That
probe does not consume the code, so a machine that turns out to be unreachable
can be retried without asking for a fresh one.

The two sections below describe the same modes for a source install, where you
pick the mode yourself.

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

Each paired device and each host holds its own credential. The central server
stores only a SHA-256 verifier of the token and never the token itself, so a
lost phone is revoked on its own without disturbing anything else:

```bash
uv run drover-server credentials list
uv run drover-server credentials revoke <credential-id>
```

Revocation takes effect on the next request.

The original shared cluster token still works while
`[auth] legacy_token_enabled` is true, which is the default. The daemon
resolves it from `--host-token`, `DROVER_API_TOKEN`, or `~/.drover/api_token`.
Prefer the environment or token file so it does not appear in shell history.
Turn the setting off once every device and host holds its own credential.

Because v0.2 does not bind a credential to a specific `host-id`, a host
credential can act as any host. Do not enroll a machine you do not fully
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

The installer writes and loads service units for you: launchd agents on macOS,
systemd user units on Linux, with lingering enabled so they survive a logout.
Both point at `~/.drover/runtime/current`, so an upgrade is a symlink flip
rather than a unit rewrite, and both set `PATH` explicitly, because a unit
that inherits nothing cannot find the agent CLIs it exists to drive.

To see what would be written without touching anything:

```bash
curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh \
  | bash -s -- --dry-run
```

For a source install, generate the same units with
`drover.server.service_units`, or write your own; review paths, bind
addresses, environment variables, and token-file permissions before loading
either.

`scripts/enroll-host.sh` has been removed. It required a hand-placed fleet
token and refused every mode except relay; `install.sh --join` covers what it
did and picks the mode from an actual reachability check.

## Updates

The hub decides what version the fleet runs. It polls the release feed on a
timer and publishes a target on the heartbeat every `drover-harnessd` already
sends, so no new channel and no inbound access to a host is involved.

A host that is behind installs the new version beside its current one and then
waits. It activates only when it has no live work: no running structured
session, and no attached terminal. If it cannot tell, it treats itself as busy.
An update deferred costs hours; an update that interrupts a turn costs work
nobody gets back.

Activation is a symlink flip plus a restart. Nothing is ever upgraded in place,
which is what makes going back cheap.

```bash
drover-server update --check     # installed, active, target, any pin
drover-server rollback           # return to the previous installed version
drover-server rollback --to 0.1.2
```

Two safety rails, neither optional:

- A version that cannot report its own version never gets the symlink.
- Before flipping, the host records what it is leaving. If the new version
  cannot reach the hub within ninety seconds, it puts the symlink back. A bad
  release costs ninety seconds rather than physical access to the machine.

`rollback` covers the other case: a version that starts, registers, and is
still wrong.

To freeze the fleet, pin it. A pinned hub never contacts the release feed:

```toml
[update]
enabled = true
check_interval_hours = 6
pinned_version = "0.1.2"
keep_versions = 2
```

Setting `enabled = false` stops the hub publishing any target, and hosts stay
where they are.

Version skew is expected during a rollout and is not an error. The hub keeps
serving hosts that have not converged yet, and a host waiting for quiesce is
fully functional the whole time.
