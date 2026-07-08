# Headless Harness Protocol — Design

Status: approved design, pending implementation plan
Date: 2026-07-06

## Why

The web terminal cockpit has three structural problems: keystroke latency
across two WebSocket hops, broken multi-byte glyphs from byte-frame splitting
in the proxy, and inconsistent Enter/submit behavior because Claude Code,
Codex, and Gemini CLI each want different line endings from their interactive
TUIs. All three are symptoms of wrapping interactive TUIs in a terminal
emulator over the network.

The goal (per `docs/meta-harness-mvp.md`) is a streamlined native experience
across all three CLIs with Nexus as the context plane. The chosen direction is
a native Swift app (iPhone + Mac). This spec covers the **backend foundation**
that app consumes: driving the CLIs in their headless/JSON modes so sessions
become structured message streams instead of terminal byte streams.

### Program decomposition (this is spec 1 of 3)

1. **Headless harness protocol** (this spec) — structured sessions in
   harnessd + normalized message schema + continuous event flow to central.
2. **Swift app MVP** — unified session timeline, launch-with-prompt, native
   chat view with permission buttons, SwiftTerm escape hatch to the existing
   PTY WebSocket.
3. **Context-plane features** — project-brief injection at launch,
   cross-harness handoff (activating `source_session_id`/`handoff_mode`),
   live cross-session awareness via an MCP tool.

### Decisions already made

- **Headless-first + terminal escape hatch.** Structured sessions are the
  primary mode; raw PTY sessions remain unchanged as fallback.
- **Own normalizer over first-party JSON modes** (Claude `stream-json`,
  Codex JSON exec/protocol mode, Gemini headless mode) — no third-party ACP
  adapters in the loop. The schema is deliberately ACP-shaped so a future
  migration is cheap.
- This work obsoletes the previously discussed "attention signals" feature:
  session state (`working` / `needs_you` / `done` / `errored`) falls out of
  the protocol for free.

## Architecture

A second session kind, `structured`, lands beside the existing PTY kind in
harnessd. Launching one spawns the CLI in its machine mode, holding stdin
open for follow-up turns where the CLI supports it (Claude, Codex), falling
back to per-turn respawn-with-resume where it doesn't (likely Gemini
initially). The driver interface hides this difference.

Each driver translates its CLI's stream into one normalized message schema —
the vocabulary `src/nexus/server/harness/events.py` already sketches —
sourced from protocol truth (`normalized_source: "structured"`), not PTY
regex. Permission requests are first-class messages answered with a
structured reply. "Enter to submit" stops existing as a concept: a turn is an
API call.

Event flow becomes **push-based**, fixing the current gap where events reach
central only while a browser terminal is attached: the daemon batches
structured-session messages and POSTs them to central every ~2 s and on every
turn boundary, over the shared-token auth (bearer, deployed 2026-07-05).
Central ingests into its registry; the context plane stays continuously
current with zero scraping. Clients read history via REST and subscribe to
live updates via one authed WebSocket per session on central — the same
two-hop path as today, but carrying a few JSON messages per turn instead of
per-keystroke frames, so hop latency stops mattering.

The daemon's local registry remains the source of truth; central's copy is a
mirror for serving clients and the context plane.

## Normalized message schema

One message = one JSON object:

```json
{
  "event_id": "uuid",
  "session_id": "harness-…",
  "seq": 42,
  "ts": "2026-07-06T12:00:00Z",
  "type": "assistant_output | user_input | tool_action | tool_result |
           approval_prompt | approval_response | status | error | raw",
  "role": "assistant | user | system | tool",
  "text": "renderable text (markdown allowed)",
  "payload": { "…driver-specific structured detail…" },
  "turn_id": "uuid of the user turn this belongs to"
}
```

- `seq` is a per-session monotonic integer assigned by the daemon; clients
  use it for ordering and gap detection.
- `approval_prompt.payload` carries `request_id`, the tool name, and the
  proposed input; `approval_response` echoes `request_id` with
  `decision: allow | deny` and optional note.
- `raw` preserves unparseable driver lines verbatim — protocol drift in a CLI
  update degrades to ugly, never to silent loss.
- Messages ride the existing `harness_events` table (`event_type` =
  normalized type, `payload` = full message). No new table.

## Session model changes

`harness_sessions` gains two columns:

- `awaiting TEXT NULL` — `"input"` (turn complete, agent idle),
  `"approval"` (pending permission request), or NULL (working / terminal
  state). Derived exclusively from protocol events.
- `last_activity TIMESTAMP NULL` — bumped on every appended message.

Lifecycle `status` values are unchanged (`starting`, `running`, `completed`,
`terminated`, `errored`). Client-visible state is derived:
`working` = running + awaiting NULL; `needs_you` = running + awaiting set;
`done` / `errored` from status. Existing PTY sessions leave both columns NULL
and render exactly as today.

## Component boundaries

- `src/nexus/server/harness/structured/driver.py` — `StructuredDriver`
  protocol (`start`, `send_turn`, `answer_permission`, `interrupt`, `close`),
  normalized message dataclasses, and the per-session pump thread that reads
  driver output, assigns `seq`, appends to the registry, and updates
  `awaiting`/`last_activity`.
- `structured/claude.py`, `structured/codex.py`, `structured/gemini.py` —
  one driver per CLI: spawn command construction + "parse their NDJSON, emit
  ours". Nothing else knows CLI specifics.
- **Daemon** (`harness/daemon.py`): `POST /sessions` gains
  `mode: "structured"` and `prompt`; new `POST /sessions/<id>/turns`,
  `POST /sessions/<id>/permission`; event-push loop batching to central.
- **Central** (`server/web/app.py` + `metrics.py` collector): ingest endpoint
  `POST /harness/events` for daemon batches (bearer-authed, idempotent by
  `event_id`); `GET /harness/sessions/<id>/messages?after_seq=N`;
  `WS /harness/sessions/<id>/stream`; the sessions list payload gains
  `awaiting` + `last_activity`.

## Error handling

- CLI exits mid-turn, malformed stream, or stdin write failure → session
  `errored`, `last_error` set, one final `error` message emitted. Driver
  failures never take down the daemon.
- Event push retries with exponential backoff; idempotent ingest means
  central outages lose nothing (daemon registry is authoritative).
- Unanswered `approval_prompt` stays `needs_you` indefinitely — no auto-deny.
- Interrupt maps to each CLI's native cancel mechanism (or process signal),
  emitting a `status` message either way.

## Testing

- **Driver units**: golden NDJSON fixtures captured from the real CLIs in
  Task 0, fed through each normalizer; assert normalized output including
  malformed-line → `raw` behavior.
- **Route tests**: real HTTP servers per the existing
  `test_harness_daemon.py` / `test_metrics.py` loopback pattern, including
  auth on every new route.
- **End-to-end**: a fake "CLI" script replaying a golden stream, driven
  through launch → turn → permission → completion, asserting events arrive on
  central and `awaiting`/`last_activity` transition correctly.
- Real-CLI smoke tests are manual (need API credentials), run at Task 0 and
  final verification.

## Task 0 — verify machine modes of the installed CLIs

Before any implementation: probe the actual binaries on the Mac Mini
(`claude`, `codex`, `gemini`) for their current headless capabilities —
exact flags for bidirectional streaming, permission-request surfacing, and
session resume — and capture golden fixtures. The design assumes
bidirectional NDJSON for Claude (`--input-format/--output-format
stream-json`), a JSON exec/protocol mode for Codex, and (worst case)
per-turn respawn with resume for Gemini. Flag names in this spec are
indicative; Task 0 output is authoritative and feeds the fixtures.

## Out of scope

- The Swift app (spec 2) and all context-plane features (spec 3).
- Web-UI adoption of the message stream.
- Event-push for PTY sessions (attach-time mirroring stays as-is).
- ACP adoption (schema stays ACP-shaped; revisit if adapters mature).
- OTLP/span pipeline changes.
