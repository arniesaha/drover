# Usability M5: Permission posture + structured-output UX

**Date:** 2026-08-03
**Status:** Approved (design), pending implementation plan

## Problem

Two usability failures observed in live fleet use:

1. **Permission prompts are invisible and fatal.** Structured (chat) Claude Code
   sessions are spawned headless (`claude -p --input-format stream-json
   --output-format stream-json`, `src/drover/server/harness/structured/claude.py:24-34`)
   with no permission flags. In headless mode, any tool that needs approval fails with
   "Claude requested permissions to use X, but you haven't granted it yet." Evidence:
   work-laptop session `harness-d5ba7c43-6d26-4c9a-aad5-8517b6445826` — 8+ consecutive
   failures calling `mcp__claude_ai_Linear__list_issues`, turn abandoned. The approval
   plumbing (harnessd `approval_prompt` events, `/permission` endpoint, iOS
   ApprovalBanner) exists end-to-end but never fires: the `control_request` mapping in
   `claude.py:106-119` is doc-derived, never verified live, and headless Claude denies
   instead of asking without the proper handshake.

2. **Codex/Gemini output is an undifferentiated text wall.** The iOS app is
   harness-agnostic and renders off event types — but only the Claude driver emits
   them. Codex reasoning items fall through to `type="status"` captions
   (`structured/codex.py:313-316`); Gemini emits one `assistant_output` blob per turn
   (`structured/gemini.py:235-286`, one-shot `-o json`). Additionally the iOS tool
   card falls back to rendering entire tool output as an uncollapsed title label
   because no driver populates `payload["tool"]`/`payload["result"]`
   (`apps/drover/Drover/Screens/Chat/MessageBubble.swift:115-121`). The
   scroll-to-bottom button gets stuck due to identified races (below).

## Decisions made

- **Permissions: option C** — ship auto (bypass) as the default now; surface
  approvals in Drover as a follow-up milestone (Part B, separate spec).
- **"Auto" means `--permission-mode bypassPermissions`** for structured Claude
  sessions — matches the posture already accepted for Codex
  (`--sandbox danger-full-access`) and Gemini (`--approval-mode yolo`). PTY/terminal
  sessions stay interactive, unchanged.
- **Collapse everything intermediate (option A):** thinking *and* tool/test runs
  render as compact collapsed cards with a one-line live status; only the final
  answer renders full-size.
- **Server-side normalization, not client heuristics:** fix drivers to emit the
  existing shared event vocabulary; the iOS app stays harness-agnostic.

## Design

### 1. Permission mode as a first-class session field

- `POST /sessions` accepts `permission_mode: "auto" | "ask"`, default `"auto"`.
  Stored as a session column (`harness/schema.py`, `harness/models.py`), proxied
  verbatim by central (`metrics.py` proxy helpers). The iOS client does not send
  it in this milestone — the server default applies; client plumbing
  (`NexusClient.createSession`, `LaunchModel`) arrives with Part B's picker.
- Claude structured driver: `auto` → append `--permission-mode bypassPermissions`
  to the spawn argv. `ask` → reject session creation with a clear error
  ("approval surfacing not yet implemented") until Part B lands. Reserving the
  field now avoids schema churn later.
- Codex/Gemini drivers ignore the field (already full-auto); PTY path ignores it.
- iOS session-creation UI: no new picker in this milestone — the field defaults
  server-side. (Picker ships with Part B when "ask" becomes real.)

**Part B (follow-up, out of scope here):** live-capture the
`control_request`/`can_use_tool` handshake on a host without a global
`defaultMode: auto` override, correct the doc-derived mapping in `claude.py`,
verify the iOS ApprovalBanner round-trip against it, then expose the
Auto/Ask picker at session creation. Tracked as its own spec + plan.

### 2. Driver normalization (server)

Shared target vocabulary (already defined, `structured/driver.py:22-34`):
`assistant_output` (+ `payload.thinking: true` for reasoning), `tool_action`
(`payload: {tool, tool_use_id, input}`), `tool_result`
(`payload: {tool, tool_use_id, result, exit_code?, status?}`).

- **Codex** (`codex.py`): map reasoning items (`item.type == "reasoning"` /
  `agent_reasoning`) → `assistant_output` + `thinking: true`. Map
  `command_execution` `item.started` → `tool_action` with
  `{tool: "shell", tool_use_id: item.id, input: {command}}`; `item.completed` →
  `tool_result` with `{tool, tool_use_id, result: aggregated_output, exit_code,
  status}`. Sandbox-denied commands are normal failed `tool_result`s (per
  `tests/fixtures/structured/FINDINGS.md` — codex exec has no approval channel).
- **Gemini** (`gemini.py`): switch `-o json` → `-o stream-json`. **First step is a
  live probe** to capture the per-line shape (never exercised; the old auth
  blocker is resolved). Map thought/tool/text events into the shared vocabulary.
  Fallback if stream-json is unusable in the installed build: keep `-o json` but
  segment the response server-side into thinking/output messages — degraded but
  no more single blob.
- **Claude** (`claude.py`): populate `payload.tool` on `tool_result` by joining
  `tool_use_id` against the originating `tool_use` block, so the iOS card can
  title the result instead of dumping raw output.

New captured NDJSON fixtures for Codex reasoning and Gemini stream-json land in
`tests/fixtures/structured/` alongside the existing ones.

### 3. iOS rendering — collapsed step cards

- **StepCard** (evolution of ToolCard, `MessageBubble.swift:142-181`): a
  `tool_action` and its `tool_result` pair into one card, matched by
  `payload.tool_use_id` (pairing lives next to the existing thinking-run fold in
  `NexusKit .../Transcript.swift`). Collapsed by default: one line with tool
  name + live status (`running…` while unpaired → `✓ exit 0` / `✗ exit 1` or
  ok/failed on completion). Expanded: full input and result, rendered through the
  existing `DisplayBlock`/`CodeBlockView`/`DiffBlockView`/`EditDiff` pipeline.
  Unpaired results (history edge cases) degrade to today's single-message card
  but with the new `payload.tool` title fix.
- **ThinkingBlock** (existing): unchanged; now triggers for all three harnesses
  because drivers emit the `thinking` flag. Collapsed by default.
- **Final answer**: `assistant_output` without the thinking flag renders
  full-size, as today.

### 4. Scroll fixes (`ChatView.swift`)

- **Unpin only on user intent:** flip `isPinnedToBottom = false` from scroll
  *gestures* (`onScrollPhaseChange` — user-initiated drag/deceleration), not from
  geometry drift. Content growth (new tall row) can no longer silently disable
  auto-scroll mid-stream. The geometry check remains only to *re-pin* when the
  user returns to the bottom.
- **Button re-pins explicitly:** tap sets `isPinnedToBottom = true` immediately,
  scrolls, and re-issues one `scrollTo` after a short settle delay to absorb
  late-measuring lazy rows (diff blocks). Button visibility keys off the pin
  state, so it can no longer stay stuck on screen.
- **Initial position:** `defaultScrollAnchor(.bottom)` (+ initial scroll on first
  batch), so opening a session with backfilling history starts at the bottom.
- Collapsed step cards shrink typical row heights, further reducing exposure.

### 5. Error handling

- Driver mapping failures (unrecognized item/event shapes) degrade to existing
  behavior (`status`/`raw` messages) — never drop events.
- Gemini stream-json parse errors on any line degrade that line to `raw`
  (existing `ProcessDriver` behavior, `driver.py:180-204`).
- `permission_mode: "ask"` → 4xx with actionable message; unknown values → 4xx.
- Old-app-new-server and new-app-old-server both work: the field is optional
  with a server-side default, and StepCard pairing degrades to single cards
  when payload keys are absent (old recorded sessions).

### 6. Testing

- **Server:** unit tests for each driver mapping against captured fixtures
  (Codex reasoning + command pairing; Gemini stream-json; Claude `payload.tool`
  join); session-creation tests for `permission_mode` (default, explicit auto,
  ask-rejected, bad value); spawn-argv assertions for `bypassPermissions`.
- **iOS:** NexusKit tests for action/result pairing (paired, unpaired, out of
  order, historical replay); MockNetworkTests stay serialized per existing rule.
  UITest: scroll-down button re-pins and disappears after tap with a tall diff
  in the transcript.
- **Live:** deploy to mac + work laptop; verify the Linear-MCP scenario runs
  un-prompted on the work laptop; Gemini + Codex sessions show thinking blocks
  and step cards on the phone; combined with the pending M3/M4 phone smoke.

## Out of scope

- Part B approval surfacing (own spec).
- Any event-schema redesign (v2 step hierarchy) — rejected, YAGNI.
- PTY/terminal permission handling (trust-gate seed handling already covers it).
- Per-host permission policy config (revisit with Part B if needed).
