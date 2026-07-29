# Drover — Multi-Host Fleet

How to add a machine — on the LAN/tailnet or on the open internet — to a
Drover fleet as a harness host, and how the hub reaches each shape of host.

## Fleet topology

```
                    ┌─────────────────────────┐
                    │   Hub — Mac Mini         │
                    │   drover-server          │
                    │   :7080 metrics/API      │
                    │   :7077 MCP  :4317 OTLP  │
                    └────────────┬─────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │ LAN / tailnet                                    │ internet
        │ (inbound reachable)                              │ (via Tailscale Funnel)
        ▼                                                   ▼
┌───────────────────┐   ┌───────────────────┐   ┌───────────────────────┐
│ Mac direct         │   │ NAS direct         │   │ Laptop (relay)         │
│ drover-harnessd     │   │ drover-harnessd     │   │ drover-harnessd --relay │
│ :7081, hub dials in │   │ :7081, hub dials in │   │ dials OUT to the hub's │
│ launchd             │   │ systemd            │   │ public funnel URL      │
└───────────────────┘   └───────────────────┘   └───────────────────────┘
```

The hub always registers a host the same way (`GET /harness/hosts`, chat,
terminal attach all look identical from the app). What differs is *how the
hub's requests reach the harness daemon*:

- **Direct hosts** (Mac, NAS) are reachable inbound — the hub connects
  straight to `host:7081` over LAN or tailnet.
- **Relay hosts** (anywhere: a laptop off the tailnet, a machine behind NAT
  you don't control) are not inbound-reachable, so the harness daemon dials
  *out* to the hub instead (`--relay`) and the hub proxies requests back down
  that outbound connection.

## The three host shapes

All three are enrolled with the same script:

```bash
./scripts/enroll-host.sh --host-id <id> --central-url <url> [--relay]
```

It validates the fleet API token against `<central-url>/harness/hosts`
*before* touching launchd, renders
`scripts/launchd/com.drover.harnessd-relay.plist.template` into
`~/Library/LaunchAgents/com.drover.harnessd.plist`, and loads it.

### 1. Mac direct (hub-local or LAN-adjacent Mac)

Reachable by the hub over LAN/tailnet; no `--relay`.

```bash
./scripts/enroll-host.sh --host-id mac-mini-2 --central-url http://mini.local:7080
```

Installs `~/Library/LaunchAgents/com.drover.harnessd.plist` (see
`scripts/launchd/README-nexus-server.md` for the equivalent hub-side
`launchctl load`/`unload`/log-tail pattern this template follows).

### 2. NAS direct (systemd host)

`enroll-host.sh` is launchd-only (macOS) — **no `drover-harnessd` systemd
unit exists in this repo yet.** One needs to be written and tested when the
NAS actually moves to running harnessd; until then this is guidance for
writing that unit, not a ready-to-copy file.

Follow the shape of the existing `scripts/systemd/*.template` units (e.g.
`nexus-server.service.template`, `nexus-tempo-relay.service.template`),
which invoke console scripts via `uv run` rather than an absolute venv
path:

```
ExecStart=@@HOME@@/.local/bin/uv run --quiet drover-harnessd --host-id nas --central-url @@CENTRAL_URL@@ --listen 127.0.0.1:7081
WorkingDirectory=@@REPO_DIR@@
```

with `WantedBy=default.target`, matching the other `*.template` units'
`[Install]` section. (`<repo>/.venv/bin/drover-harnessd` — the same
absolute-venv-binary shape `enroll-host.sh`/the launchd template use on
macOS — also works once `uv sync` has populated `.venv`, since it's the
same console script either way; `uv run` is just what every other unit in
`scripts/systemd/` already does, so match that for consistency rather than
introducing a second invocation style on the NAS.)

Validate the token the same way the enroll script does, before enabling the
unit:

```bash
curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $(cat ~/.drover/api_token)" \
  <hub-url>/harness/hosts
# must print 200 before you `systemctl enable --now` anything
```

### 3. Laptop (relay)

Not inbound-reachable — dials out through `--relay` to the hub's public
Tailscale Funnel URL:

```bash
./scripts/enroll-host.sh --host-id work-laptop --central-url https://mini.tailnet.ts.net --relay
```

The daemon still listens on `127.0.0.1:7081` locally (for `drover-collect`
and local tooling) but registers itself over an *outbound* WebSocket to
`--central-url`; the hub proxies fleet requests down that connection instead
of dialing the laptop directly.

`--central-url` accepts `http://`, `https://`, `ws://`, or `wss://` (`ws`/`wss`
are aliased to `http`/`https`); any other scheme is a hard config error at
startup. The funnel URL is always `https://…`.

> **Never pass `--local-url` or `--tailscale-url` on a relay host.** A relay
> host has no meaningful inbound URL — its outbound socket is the only way in.
> Every host shape in this repo listens on `127.0.0.1:7081`, so a URL on a
> relay host's registry row would resolve against the *hub's own loopback*.
> The hub refuses to dial a `connection_kind = "relay"` host by URL for
> exactly this reason (it returns `502 relay host is not connected` instead),
> but don't set one in the first place.

## Tailscale Funnel setup (hub side)

The hub's metrics/API port (`7080`) is what needs to be reachable from the
public internet for relay hosts to dial in and for the iOS app to work off
the tailnet (e.g. on cellular).

```bash
# Turn the hub's :7080 into a public HTTPS URL
tailscale funnel --bg 7080

# See the assigned public URL and current funnel state
tailscale funnel status

# Turn it off
tailscale funnel --https=443 off
```

`tailscale funnel status` prints the public `https://<machine>.<tailnet>.ts.net`
URL once the funnel is up — that's the value to pass as `--central-url` to
relay hosts. `tailscale funnel --bg` keeps the funnel running across
reboots/logouts; re-run `tailscale funnel status` any time to confirm it's
still active.

## Security

The funnel URL is **public** — anyone with the URL can reach `/harness/*`.
The bearer token (`~/.drover/api_token` / `DROVER_API_TOKEN`) is the *only*
gate on that surface; there is currently one shared token for the whole
fleet. Rotate it via the existing manual rotation flow (write a new value
into `~/.drover/api_token` on every host, verify `401` bare / `200` with the
new bearer token, update any stored copies — see the Mac + NAS token
rotation entry in `docs/porting-and-cutover.md` for the exact steps that
were run last time).

Per-host tokens (so a single leaked host credential doesn't expose the whole
fleet) are **not implemented yet** — tracked as a follow-up issue, not a gap
to work around today. Don't hand out the shared token to anything you don't
fully trust.

## Troubleshooting

**Relay host shows offline in the app:**

1. Check the laptop's own logs first — the daemon logs locally even when it
   can't reach the hub:
   ```bash
   tail -f ~/Library/Logs/drover/harnessd.err.log
   tail -f ~/Library/Logs/drover/harnessd.out.log
   ```
2. Check the funnel is actually up on the hub:
   ```bash
   tailscale funnel status
   ```
   If it's not listed as running, relay hosts have nothing to dial into —
   restart it with `tailscale funnel --bg 7080`.
3. Check the token. A relay host that started with a stale/rotated token
   will fail to register; re-run `enroll-host.sh` (it re-validates the token
   before touching launchd) or update `~/.drover/api_token` and reload:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.drover.harnessd.plist
   launchctl load ~/Library/LaunchAgents/com.drover.harnessd.plist
   ```

**Direct host (Mac/NAS) shows offline:** same log/token checks as above,
plus confirm the hub can actually reach `host:7081` on the LAN/tailnet
(direct hosts don't need the funnel at all — that's only for relay hosts and
the iOS app's off-tailnet path).
