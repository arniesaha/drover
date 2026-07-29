# Multi-host relay + top-notch UX — design

**Date:** 2026-07-28
**Status:** Approved (brainstorming session 2026-07-28)
**Cycle:** 1 of 3 (cycle 2: public release; cycle 3: OKF assessment — tracking issues filed separately)

## Goal

Add the work laptop (macOS, no Tailscale/VPN allowed) as a third harness host, reachable from anywhere, while keeping the iPhone app the single client for the whole fleet (Mac Mini + NAS + work laptop). In the same cycle, raise the app UX to the bar set by Claude Code `/remote-control` (mobile chat rendering) and Termius (mobile terminal ergonomics).

## Context / constraints

- Current topology is hub-and-spoke: the Drover server (hub) on the Mac Mini proxies all app traffic to per-host harnessd daemons. The app talks **only** to the hub — HTTP via `MetricsCollector._proxy_harness_request` (`src/drover/server/metrics.py`), terminal websockets via the socket bridge in `src/drover/server/web/app.py` (`_proxy_terminal_websocket`). No app-side connection changes are needed for a new host.
- The hub currently **dials in** to each harnessd (`local_url` / `tailscale_url` in the registry). A corporate laptop can never accept inbound connections from the home fleet, and cannot join the personal tailnet.
- harnessd already pushes events outbound to the hub (`EventPusher` → `{central_url}/harness/events`), so an outbound-only host is half-supported already.
- The user chose: outbound relay reachable from anywhere; harnessd-only on the laptop (no desktop client); Tailscale Funnel over Cloudflare (no new vendor).

## Architecture: relay mode

### harnessd (spoke) side

- New config: `relay: true` (alongside existing `central_url` and token). When set, harnessd opens **one persistent outbound WSS connection** to `{central_url}/harness/relay`, authenticated with the bearer token, identifying itself by `host_id`.
- Reconnect: jittered exponential backoff, retry forever. A rejected token is logged clearly and surfaced in local status output; the enroll flow (below) validates the token upfront so this is not hit silently.
- The local HTTP server keeps running in relay mode (local debugging, no behavior change for direct hosts).

### Multiplexed protocol (one socket, two frame families)

1. **Request/response:** hub → `{kind:"req", id, method, path, body}`; harnessd services it against the *same handlers as its HTTP server* and replies `{kind:"res", id, status, body}`. Every existing endpoint (session create/terminate/actions, auth flows, native sessions, transcript) works over relay with no per-endpoint code.
2. **Channels** (terminal attach): `{kind:"open", chan, path}` / `{kind:"data", chan, payload}` / `{kind:"close", chan}`. Channels interleave freely with requests on the same socket.

### Hub side

- `RelayManager` holds live relay connections keyed by `host_id`.
- Registry host row gains `connection_kind: "direct" | "relay"`.
- Endpoint resolution: if the host has a **live** relay connection, route via `RelayManager`; otherwise dial `local_url`/`tailscale_url` as today. `_proxy_terminal_websocket` gets a second upstream flavor: a relay channel instead of `socket.create_connection`. Client-facing bytes are identical.
- **Presence:** relay hosts are online iff their socket is connected — instant, trustworthy status flips that feed the UX resilience work. Direct hosts keep health-check-based presence. Fleet endpoint exposes `status` + `last_seen` per host.

### Public endpoint

- Tailscale Funnel on the Mac Mini fronts the hub at `https://<mac>.<tailnet>.ts.net` (443, TLS terminated by Tailscale, WSS passes through). The laptop's `central_url` points at the funnel URL; only outbound 443 from the corporate network.
- Security: the funnel URL is internet-reachable, so the bearer token is the only gate. Token remains mandatory on every route (already true). **Per-host tokens are a fast-follow issue, not a blocker.**

### Fleet assignments

- Mac Mini: direct (localhost).
- NAS: direct for now; **NAS → relay migration is a follow-up issue** (retires the SSH-tunnel flakiness class; mitigation path for #11).
- Work laptop: relay.

## UX track (iOS)

### Fleet legibility (sessions screen becomes fleet-first)

- Sessions grouped by host. Host header: name, status dot (green live / amber reconnecting / gray offline with "last seen …"), `relay` badge only when informative.
- Session row: harness identity (icon + name from `HarnessPresentation`), working directory or task title, status chip (running / waiting on you / gated / exited), compact `TokenUsageSummary` (tokens + context-window fill).
- Sessions waiting on input sort to the top of their host group. The one-glance test: "what is every agent doing, and does any of them need me?"

### Connection resilience (explicit, never silent — three layers)

1. **App ↔ hub unreachable:** persistent banner with auto-retry + manual retry; list stays rendered from last-known state, dimmed, never blanked (extends the 2026-07-13 chat-blanking fix).
2. **Hub up, host offline:** host group stays visible, grayed, last-seen shown; sessions marked stale, never vanish.
3. **Stream/terminal drop:** auto-reconnect with backoff + thin "reconnecting…" pill; scrollback preserved (builds on the 2026-07-13 terminal reconnect work).
- No infinite spinners: every loading state times out into a retriable error affordance.

### Terminal polish (Termius bar)

- Keyboard accessory bar: `Esc`, `Tab`, sticky `Ctrl`, arrow cluster, `/`, `|`, `-`, `~`, plus 1–2 configurable slots.
- Pinch-to-zoom font size, persisted; monospace theme matching app light/dark.
- Long-press selection & copy from scrollback; tap-and-hold paste.
- Out of scope (YAGNI): split panes, SSH key management, multi-tab terminal.

### Chat polish (`/remote-control` mobile bar)

- Collapsible thinking runs with duration label (builds on landed coalescing).
- Tool calls as compact cards (name + one-line summary; expandable args/result).
- Markdown: horizontally scrolling code blocks with copy button; diff rendering (green/red) for edit-tool results.
- Queued-turn affordance polished; `TokenUsageSummary` readout in chat header.

### Onboarding

- **App:** first-run screen (server URL + token) with a connectivity check that distinguishes wrong URL / bad token / Local Network permission denied (the 2026-07-13 physical-iPhone lesson), each with its specific fix. Same diagnostics reachable from Settings.
- **Host:** single `drover host enroll` command (curl-able script) — installs harnessd, writes config (`central_url`, token, `relay` flag), registers launchd/systemd, validates the token upfront. Docs cover all three host shapes: Mac direct, NAS direct/systemd, laptop relay.

## Error handling at the seams

- Relay request timeout → 502 to the app; host marked degraded, not hung.
- Relay drop mid-terminal → channel close → reconnecting pill → reattach when socket returns.
- Funnel outage → laptop backs off indefinitely; host shows offline + last-seen; nothing crashes.
- `EventPusher` queue-full/drop behavior unchanged.

## Testing

- **Unit:** frame mux (id correlation, interleaved channels, close semantics); harnessd reconnect/backoff; `RelayManager` routing + presence flips.
- **Integration:** hub + harnessd in-process with relay on — create session → events arrive → terminal attach over channel → terminate. Existing direct-mode suite as regression.
- **iOS:** NexusKit unit tests for host-grouping/status view models (respect the single-root-suite MockURLProtocol serialization rule); sim E2E on a fresh install per the established runbook.
- **Live acceptance:** enroll the work laptop through the funnel, drive one of its sessions from the iPhone **on cellular**. This single test exercises relay + funnel + presence + UX, and doubles as the #12/#6 phone verification.

## Milestones

- **M0** — land the in-flight harness-presentation + token-usage work (currently uncommitted on main).
- **M1** — relay protocol: harnessd outbound client + hub `RelayManager` + registry `connection_kind` + proxy routing, with tests.
- **M2** — funnel on Mac Mini, `drover host enroll`, work laptop live; cellular phone verify (closes #12, #6).
- **M3** — fleet-first sessions screen + three resilience layers.
- **M4** — terminal accessory bar/gestures + chat rendering polish.
- **M5** — onboarding diagnostics + enroll docs.

## Trailing tracking issues (filed at cycle start, executed later)

1. **Cycle 2 — public release:** update #4 with the *rename-everything* decision — `nexus.*` span keys and `nexus_handoff` → `drover.*` with a one-time lakehouse migration, DroverKit rename, docs debranding, Traycer positioning, sanitization sweep, flip public.
2. **Cycle 3 — OKF assessment doc:** map OKF v0.1 (Google Cloud, June 2026 — markdown + YAML frontmatter bundles, one concept per file, only `type` required) against each Drover context artifact — handoffs, briefs/memory, long-term memory, episodic, skills, evals, context-performance metrics, traces — with an adopt/adapt/skip verdict per artifact. Adoption work only if verdicts justify it.
3. **Follow-up — NAS → relay migration** (retire SSH tunnels; #11 mitigation path).
4. **Follow-up — per-host tokens** for the funnel-exposed hub.
