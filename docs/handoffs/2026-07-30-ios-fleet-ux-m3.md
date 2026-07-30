# Handoff — iOS fleet UX M3 (fleet-first sessions + resilience) close-out

**Date:** 2026-07-30
**Main:** `28bad8e` (merge of PR #19), tree clean, NexusKit suite 154 tests / 13 suites green
**Spec:** `docs/superpowers/specs/2026-07-28-multihost-relay-ux-design.md` (UX track, milestone M3)
**Plan:** `docs/superpowers/plans/2026-07-30-ios-fleet-ux-m3.md`
**Previous handoff:** `docs/handoffs/2026-07-30-relay-cycle.md`

---

## What shipped (PR #19, 13 commits, base `9e1118a`)

Executed via subagent-driven development (fresh implementer + reviewer per task,
final whole-branch review clean, zero Critical/Important findings at merge).

- **Fleet-first sessions screen** — sessions grouped by host. Section headers:
  presence dot (green online / orange stale / gray offline), host name, `relay`
  badge, "last seen …" for non-online hosts. Waiting-on-you sessions sort to the
  top of their host group; offline/stale groups render dimmed; hosts with zero
  active sessions still appear; sessions on hosts the hub no longer lists get a
  synthesized offline group (never vanish). Global "Finished" disclosure kept.
- **Resilience layer 1 (app ↔ hub)** — three mutually exclusive surfaces:
  never-loaded → "Connecting…" / retriable full-screen error; loaded+unreachable
  → persistent banner with Retry over a dimmed, never-blanked last-known list;
  loaded+reachable action errors (e.g. continue-session failure) → inline red
  section. 15s HTTP request cap. No infinite spinners.
- **Row status chips** — Running / Waiting on you / Needs approval / Exited /
  Error (replaced the redundant per-row host capsule).
- **Shared `ReconnectingPill`** for chat + terminal; chat pill gained
  `chat-reconnecting` a11y id (`terminal-reconnecting` preserved for UI tests).
- **Model layer (NexusKit)** — `HostSummary` decodes `connection_kind` /
  `last_seen_at` with three-way `presence` (relay hosts: socket-truth
  online/offline; direct hosts: online/stale after 45s); `SessionStore` gained
  pure `hostGroups(hosts:sessions:)` + `hasLoadedOnce`.

### Bug fixes surfaced by the work

- **`WireDate` now parses the hub's `str(datetime)` wire format** (space
  separator, optional fraction/offset, naive⇒UTC). Before this, `last_activity`
  / `last_seen_at` were **silently nil on device** — relative timestamps in the
  app never populated against the real server.
- Host `capabilities.harnesses` decode keeps per-element leniency (one malformed
  entry no longer wipes the host's whole harness list).

### Post-merge state

- Primary checkout `main` rebased onto origin; the stray duplicate commit
  `37a8569` (a subagent committed to the wrong checkout during Task 7) was
  auto-dropped by rebase as previously-applied. Worktree + feature branch
  (local and remote) deleted after merge.
- `xcodegen generate` re-run on main; package tests green there. App is
  deploy-ready from `apps/drover/Drover.xcodeproj`.

---

## Leftover work

### 1. Live smoke check on the phone (with the M3 deploy)
Not yet done — mocks covered the logic, no eyeball pass yet:
- [ ] Host sections ordered online → stale → offline; `relay` badge on work-laptop
- [ ] Stale/offline groups dimmed, "last seen …" text present
- [ ] Waiting session pinned to top of its group with "Waiting on you" chip
- [ ] Stop the hub briefly → unreachable banner appears, list stays rendered
      (dimmed, not blanked), Retry + auto-poll recover
- [ ] Timestamps actually populate now (WireDate fix — they were blank before)

### 2. Background notifications — decision pending (#20)
Investigated 2026-07-30: **not a code bug**. The BGAppRefresh path is correctly
wired (registration, plist keys, reschedule, Keychain `AfterFirstUnlock`), but
BGAppRefresh is discretionary — iOS runs it opportunistically (hours apart,
never after force-quit / with Background App Refresh off), so timely
"needs you" alerts with the app closed are architecturally impossible on it.
Free personal dev team ⇒ no `aps-environment` ⇒ direct APNs unavailable.
Options (pick one):
1. **Hub-side push relay via ntfy/Pushover/Telegram** — recommended; hub already
   derives `awaiting` transitions at event ingest (`web/app.py::_derive_awaiting`);
   server-only change, near-instant, no Apple entitlements.
2. Paid Apple Developer Program + real APNs (also unlocks the time-sensitive
   entitlement removed earlier).
3. Accept BGAppRefresh limits (document; keep Background App Refresh on, don't
   force-quit).

### 3. iOS UX M4 + M5 — unplanned
- **M4:** Termius-style terminal accessory bar (Esc/Tab/sticky Ctrl/arrows,
  pinch-zoom, copy/paste) + chat rendering polish (collapsible thinking runs,
  tool-call cards, code-block/diff rendering).
- **M5:** onboarding diagnostics (first-run URL+token screen distinguishing
  wrong-URL / bad-token / Local-Network-denied) + enroll docs.
Write each as its own plan against post-M3 code.

### 4. Tracked follow-ups from M3
- **#17** — session-row token usage needs a hub-side rollup (usage exists only
  in per-message payloads; add last-usage columns at event ingest, additive
  migration like `connection_kind`, then decode + render in `SessionRow`).
- **#18** — `LaunchModel.availableHosts` filters `status == "online"`, so stale
  hosts silently disappear from the launch picker.

### 5. Carry-forwards from the relay cycle (unchanged, see previous handoff)
- **Per-host tokens** — still the top engineering priority (shared bearer token
  on a public funnel URL ⇒ host impersonation risk).
- NAS → relay migration + #11 (systemd unit not authored).
- First green CI run after the `mcp>=1.2,<2` pin still unverified;
  `relay_protocol.py` pre-existing black violation.
- Cycle 2 (public release, #4/#5) and Cycle 3 (OKF assessment) unstarted.

---

## Notes for the next session

- The `.superpowers/sdd` ledger for this plan was deleted with the worktree per
  process; git history + this doc are the record.
- Repo test baseline is now **154 tests / 13 suites** (`apps/drover/README.md`
  updated).
- `SessionStore.working` is currently unconsumed by views (kept deliberately:
  public, tested, symmetric with `needsYou`/`finished`; M4/M5 may use it).
