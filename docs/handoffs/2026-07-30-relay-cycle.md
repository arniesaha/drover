# Handoff — Multi-host relay cycle (Cycle 1a) close-out

**Date:** 2026-07-30
**Main:** `9e1118a` (tree clean, suite 1037 passing)
**Spec:** `docs/superpowers/specs/2026-07-28-multihost-relay-ux-design.md`
**Plan:** `docs/superpowers/plans/2026-07-28-multihost-relay.md`
**SDD ledger:** `.superpowers/sdd/2026-07-28-multihost-relay/progress.md` (gitignored)

---

## What shipped

Outbound-relay multi-host support, so a harness host with **no inbound reachability**
(work laptop, no Tailscale/VPN) can join the fleet by dialing **one persistent outbound
WSS** to the hub. The hub multiplexes existing request/response and terminal-attach
semantics over that single socket. The iOS app was unchanged for relay — it talks only to
the hub, and relay hosts are indistinguishable from direct hosts at the app layer.

Delivered (Plan Tasks 1–12 + final whole-branch review + 2 fix waves):

- **harnessd `--relay`** — outbound WSS client (`relay_client.py`), skinny `drover-harnessd`
  entrypoint only; reconnect backoff, single write-lock over the hub socket, loopback dispatch.
- **Hub `RelayManager`** (`relay_manager.py`) + `/harness/relay` endpoint (`web/app.py`) —
  socket hijack, bounded write path, drop-oldest channel queue, ping keepalive, last-rx
  presence watchdog.
- **Routing choke point** (`metrics.py::_harness_request`) — relay if `is_live` else direct
  dial; **no direct fallback for relay hosts** (provably non-bypassable, verified by test).
- **Terminal attach over relay channels** with hub-side **event mirroring** (relay PTY
  sessions get a populated hub event log → chat view renders).
- **Socket-truth fleet presence** — `is_live` overrides stored `connection_kind`.
- `connection_kind` column/field/param (additive migration; old hubs ignore it).
- **Frame protocol** (`relay_protocol.py`): hello / req / res / open / opened / open_error
  / data / close; masked client-role ws helpers; `MAX_FRAME_BYTES = 8 MiB`.
- **Ops:** `scripts/enroll-host.sh` (upfront token validation, relay-only by design),
  `scripts/launchd/com.drover.harnessd-relay.plist.template`, `docs/multi-host.md`.
- **In-process e2e** (`tests/test_relay_e2e.py`): real hub + harnessd + RelayClient + /bin/cat PTY.

## Live deploy state (Task 13)

- **Hub + Mac harnessd** redeployed on the Mac Mini (launchd), clean restart.
- **Tailscale Funnel** up at `https://arnabs-mac-mini.tailb3dd58.ts.net` →
  proxy `127.0.0.1:7080`. Token gate holds on the public path: **401 without / 200 with**.
- **Fleet:** `mac-mini | online | direct`, `nas | online | direct`,
  `work-laptop | online | relay`. All three healthy.
- **Relay request round-trip verified** hub→laptop
  (`GET /harness/hosts/work-laptop/native-sessions` returned real claude-code sessions).
- **App verified:** latest iOS build installed on the physical iPhone; drove several real
  sessions on work-laptop through the app — create / terminal / chat / terminate flow
  **broadly aligned**.

### Task 13 residual (small)

- [ ] **claude-code auth flow verify** (the #12 piece) — confirm sign-in flow works
      end-to-end against the fresh laptop host before closing #12.
- [ ] **Presence-flip test** — close laptop lid / unload agent → app flips offline in
      ~30–80s → reopen flips online. (Nice-to-have confirmation, not blocking.)
- [ ] **Close #6 and #12** with verification notes once auth-flow confirmed.
- [ ] **File the trailing issues** (see "Next" below) — commands staged in Plan Task 13 Step 6.

---

## Carry-forwards / known debt

- **Single shared bearer token is the only gate on a public funnel URL.** Any token holder
  can claim any `host_id` (newest-wins attach → silent impersonation). Documented in
  `docs/multi-host.md` security section. **Fix = per-host tokens (tracked follow-up).**
  This is now a *live* exposure, not theoretical — the hub is publicly reachable.
- **CI green unverified** since the `mcp>=1.2,<2` pin (mcp 2.0 dropped `mcp.server.fastmcp`).
  Production venv confirmed running mcp 1.28.1; first green CI run after the pin still needs eyes.
- **`relay_protocol.py` pre-existing black violation** — only fails PR-based CI runs.
- **NAS `drover-harnessd` systemd unit not authored** — NAS is still direct; docs give the
  uv-run shape but the unit doesn't exist. Blocks NAS→relay migration.
- Deferred minors are itemized per-task in the SDD ledger.

---

## Next — candidate workstreams (see chat for ranked recommendation)

1. **Per-host tokens** (security) — retire the shared-token impersonation risk now that the
   fleet is publicly exposed via funnel. *Highest engineering priority.*
2. **Plan 2 — iOS UX M3–M5** (the original "top-notch UX" goal): fleet-first sessions screen,
   three resilience layers, Termius-bar terminal, /remote-control-bar chat, onboarding
   diagnostics. *Highest user-facing value.* Write against current app code + real relay presence.
3. **NAS → relay migration + #11** (NAS boot races) — author the systemd unit, flip NAS to
   `--relay`, retire the SSH-tunnel class. Reliability.
4. **Cycle 2 — public release** (#4, #5): full nexus→drover rename incl. `nexus.*` span keys +
   one-time lakehouse migration, DroverKit rename, docs debranding, sanitization, flip public.
5. **Cycle 3 — OKF v0.1 assessment doc** — map OKF against handoffs, memory, long-term/episodic,
   skills, evals, context-performance, traces; adopt/adapt/skip verdict per artifact.
   Parallelizable as a research task.

## Related memory
- `relay-cycle-code-complete.md` — cycle status + carry-forwards
- `harness-auth-flows-landed.md` — #12 detail + MockNetworkTests serialization rule
- `mac-cutover-done.md`, `issues-batch-2026-07-22.md`
