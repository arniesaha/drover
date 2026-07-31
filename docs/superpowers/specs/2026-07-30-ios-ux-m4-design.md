# iOS UX M4 — terminal interaction gaps + chat code/diff rendering

**Date:** 2026-07-30
**Milestone:** M4 (UX track) per `docs/superpowers/specs/2026-07-28-multihost-relay-ux-design.md`
**Baseline:** main `a3b3108`, NexusKit 154 tests / 13 suites green
**Previous handoff:** `docs/handoffs/2026-07-30-ios-fleet-ux-m3.md`

## Scope

The handoff described M4 as "Termius-style terminal accessory bar + chat
rendering polish", but part of that already shipped:

- SwiftTerm's built-in `TerminalAccessory` (Esc, Tab, Ctrl toggle,
  auto-repeating arrows) is already wired (`Drover/Screens/Terminal/TerminalView.swift`).
- Collapsible thinking runs (`ThinkingBlock`) and tool-call cards
  (`MessageBubble`'s `ToolCard`) already exist.

M4's actual scope is the remaining gaps:

**Terminal**
1. Copy — `TerminalBridge.clipboardCopy` is a no-op; selection copies nothing.
2. Paste — no affordance at all.
3. Pinch-zoom — no font scaling.
4. Sticky Ctrl — verify SwiftTerm's accessory Ctrl toggle behavior; work item
   only if it doesn't latch usably.

**Chat**
5. Fenced code blocks render as flattened plain text
   (`AttributedString(markdown:)` is inline-only).
6. ` ```diff ` fences get no diff treatment.
7. claude-code Edit/MultiEdit tool cards show raw JSON where a red/green diff
   would make phone approvals readable.

Out of scope (deliberate): syntax highlighting (no new dependencies), diff
extraction for codex/gemini payloads (raw-JSON fallback stays), bracketed
paste, M5 onboarding work.

## Design

### Chat — model layer (NexusKit)

- **`DisplayBlock` enum:** `.text(AttributedString)`,
  `.code(language: String?, code: String)`, `.diff([DiffLine])`.
  Fence tagged `diff` → `.diff`; any other fence → `.code`; prose between
  fences → `.text` via the existing inline-markdown parse.
- **`HarnessMessage.displayBlocks: [DisplayBlock]`** — computed once at
  decode, alongside (not replacing) `displayText`, for the same reason
  `displayText` exists: re-parsing per render pass saturates the main thread
  during long streams. Segmentation is a pure static function.
- **`DiffLine`:** `kind` (add / remove / hunk / context) + `text`, classified
  by line prefix (`+`, `-`, `@@`; everything else context).
- **`EditDiff` extractor:** from a claude-code `Edit` or `MultiEdit` tool
  payload, produce old/new line groups — `old_string` lines as removals,
  `new_string` lines as additions, per edit. No diff algorithm: an Edit
  payload already *is* an old→new pair. Any unrecognized shape → `nil`.

### Chat — view layer (Drover)

- **`CodeBlockView`:** monospaced, dark inset background, horizontal scroll,
  copy button, language caption when present.
- **`DiffBlockView`:** rows tinted by `DiffLine.kind` (green add / red remove /
  secondary hunk-and-context), monospaced, chrome shared with `CodeBlockView`.
- **`MessageBubble`:** `assistantBubble` iterates `displayBlocks` instead of a
  single `Text`. `ToolCard` shows `DiffBlockView` inside its "Details"
  disclosure when `EditDiff` extraction succeeds; otherwise today's raw JSON.

### Terminal (existing files only)

- **Copy:** implement `clipboardCopy` in `TerminalBridge` → decode UTF-8 →
  `UIPasteboard.general`.
- **Paste:** `sendPaste()` on the bridge (clipboard string → existing
  `TerminalWire.inputFrame`); "Paste" item in the toolbar ellipsis menu next
  to Interrupt/Terminate.
- **Pinch-zoom:** `UIPinchGestureRecognizer` on the SwiftTerm view scaling
  font size, clamped 9–24pt, persisted via `@AppStorage("terminalFontSize")`,
  applied at `makeUIView`. Font changes trigger SwiftTerm's cols/rows
  recalculation, whose existing `sizeChanged` delegate already sends the
  `resize` frame — PTY stays in sync with no new wire code.
- **Sticky Ctrl:** verification spike first; no speculative build.

## Data flow

- Chat: harness event → `HarnessMessage` decode → segmentation →
  `displayBlocks` → `MessageBubble`.
- Tool card: payload → `EditDiff?` → `DiffBlockView` or raw-JSON fallback.
- Copy: SwiftTerm selection → `clipboardCopy(content:)` → pasteboard.
- Paste: toolbar → bridge reads pasteboard → `input` frame.
- Pinch: gesture → font update → SwiftTerm `sizeChanged` → `resize` frame.

## Error handling

- **Unterminated fence** (mid-stream cutoff or malformed markdown): everything
  after the open fence renders as a code block. Deterministic, tested.
- **`EditDiff` nil** (other harness / unexpected shape / missing keys): card
  falls back to raw JSON disclosure. Never a broken card.
- **Paste with empty clipboard:** no-op. Paste sends raw text including
  newlines (Termius behavior).
- **Font size:** clamp guards runaway pinch; bad persisted value → default.

## Testing

- NexusKit unit tests carry the weight: segmentation (plain, single fence,
  multiple fences, language tags, `diff` tag, unterminated fence, fence-only
  message), `DiffLine` classification, `EditDiff` extraction (Edit, MultiEdit,
  malformed → nil).
- Views get a11y ids (e.g. `code-block-copy`, `tool-diff`) per M3's pattern;
  existing UI-test ids (`terminal-reconnecting`, etc.) untouched.
- Terminal bridge changes stay thin (delegate wiring + one method); verified
  in a live phone smoke pass, folded together with the still-pending M3 smoke
  checklist (`docs/handoffs/2026-07-30-ios-fleet-ux-m3.md` §1) into one
  device session at the end of the cycle.

## Success criteria

1. A fenced code block in an assistant reply renders monospaced on a dark
   inset with horizontal scroll and a working copy button.
2. A ` ```diff ` block renders with red/green line tinting.
3. A claude-code Edit approval card shows a readable old→new diff instead of
   raw JSON; codex/gemini cards are unchanged.
4. Terminal selection copy lands in the iOS clipboard; toolbar Paste types
   the clipboard into the PTY.
5. Pinch changes terminal font size smoothly, persists across app launches,
   and full-screen TUIs repaint correctly after the resize.
6. NexusKit suite green with new tests; no regressions in the existing 154.
