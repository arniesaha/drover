# Chat UX: long-press copy, image attachments, heading rendering

**Date:** 2026-08-04 · **Status:** approved

Three user-visible changes to the Drover iOS chat experience, one of which
(images) spans the full stack down to the harness drivers.

## 1. Long-press copy on chat bubbles

**Problem.** `MessageBubble` attaches no context menu, so long-pressing a
reply does nothing.

**Design.** Add `.contextMenu { Button("Copy", systemImage: "doc.on.doc") }`
to the assistant bubble and the user bubble in
`apps/drover/Drover/Screens/Chat/MessageBubble.swift`. Copy puts the
message's **raw markdown source** (`message.text`) on `UIPasteboard.general`
— code fences included — not the rendered `AttributedString`. Tool cards,
status captions, thinking blocks, and raw disclosures are unchanged.

## 2. Image attachments forwarded to the harness

**Problem.** The composer is text-only; there is no way to get an image in
front of a harness agent.

**Wire path today** (all verified): Composer → `ChatModel.sendTurn()` →
`NexusClient.sendTurn` `POST /harness/sessions/{id}/turns` `{"text": ...}` →
hub `proxy_harness_session_action` forwards the JSON body **verbatim**
(direct dial or relay) → harnessd `_create_turn` reads only `text` →
`structured.send_turn` → driver.

**Design.**

- **Composer** (`Composer.swift`): paperclip button → `PhotosPicker`
  (PhotosUI, multiple selection allowed, images only). Picked images are
  downscaled to ≤1568 px long edge and JPEG-encoded at ~0.7 quality
  (typically 200–800 KB), shown as a removable thumbnail strip above the
  text field. Send is enabled when there is text **or** at least one image.
- **ChatModel**: holds `pendingAttachments: [TurnAttachment]`
  (`media_type`, `data`). Attachments ride the existing 409
  turn-queueing path: a queued turn keeps its attachments and they are
  sent when the queue drains. Cleared on successful send.
- **NexusClient**: `sendTurn(sessionID:text:images:)` posts
  `{"text": "...", "images": [{"media_type": "image/jpeg",
  "data_base64": "..."}]}`. Requests carrying images use a 60 s
  `timeoutInterval` (default stays 15 s).
- **Hub**: no route changes. Bump the forwarded timeout for the `turns`
  action to 60 s in `proxy_harness_session_action` so large bodies survive
  the hub→host hop.
- **harnessd `_create_turn`** (`daemon.py`): decode `images[]`
  (base64 → bytes, cap 10 MB decoded per image, 400 on bad base64), write
  each to `~/.drover/attachments/<session_id>/<turn_id>-<n>.<ext>`
  (extension from `media_type`; directory created on demand, never inside
  the session cwd/worktree so repos stay clean). Append
  `\n\n[Attached image: <absolute path>]` per image to the turn text, then
  pass a structured attachment list to `send_turn`. The existing
  "400 if text empty" check relaxes to "400 if text empty **and** no
  images" — an image-only turn's text is just the attachment lines.
- **Drivers.** `manager.send_turn(session_id, text, turn_id=None,
  images=None)` where `images` is `[(path, media_type, bytes)]`:
  - **claude** (`structured/claude.py`): in addition to the path line in the
    text block, append `{"type": "image", "source": {"type": "base64",
    "media_type": ..., "data": ...}}` entries to the stream-json user
    message content array, so the model sees the image without a Read call.
  - **codex / gemini**: prompt is argv-only; the path line in the text is
    the whole mechanism. No driver change beyond accepting the parameter.
- **Echo/display**: the emitted `user_input` StructuredMessage payload
  gains `attachments: [{"path": ..., "media_type": ...}]`; the user bubble
  renders a paperclip caption ("1 image" / "N images"). No thumbnail
  persistence in the transcript.

**Limits.** Relay-connected hosts cap websocket frames at 8 MiB
(`MAX_FRAME_BYTES`), so client-side downscaling is the primary defense;
base64 of a downscaled JPEG stays well under it. Direct-dial hosts have no
cap. Multiple images per turn are allowed; the client keeps the combined
payload under ~6 MB by refusing further attachments past that.

**Out of scope:** camera capture, non-image files, pasting images into the
text field, rendering received images in the transcript.

## 3. Render `###` headings instead of literal hashes

**Problem.** `DisplayBlock.parseInlineMarkdown` uses
`interpretedSyntax: .inlineOnlyPreservingWhitespace`, which ignores
block-level markdown — `### Title` renders as literal hashes.

**Design.** Extend `DisplayBlock.segment()` (NexusKit `DisplayBlocks.swift`),
which already walks lines to split code fences, to recognize heading lines
**outside fences**: `^#{1,6} ` (hash run + space, leading whitespace
trimmed). Each becomes a new case
`.heading(level: Int, AttributedString)` — the remainder of the line runs
through the existing inline markdown parse so `### **bold** title` still
works. `MessageBubble` renders headings with weights: 1–2 → `.title3.bold()`,
3 → `.headline`, 4–6 → `.subheadline.bold()`. Lists, blockquotes, and
tables remain inline-only (out of scope until they bother someone).
`displayText` (legacy whole-message string) is unchanged.

## Testing

- **NexusKit**: `DisplayBlocksTests` — heading detection levels 1–6, `#` in
  code fences stays literal, `#hashtag` (no space) stays literal, inline
  markdown inside headings. `ChatModelTests` — attachments survive
  queueing, cleared after send. `ClientTests` — body shape with and
  without images.
- **Python** (`tests/`): `_create_turn` writes attachment files and
  augments text; bad base64 → 400; claude driver content array includes
  image blocks (build the real message, don't grep); codex/gemini receive
  path-augmented text only.
- **Manual**: sim/device smoke — send an image to a claude session on
  mac-mini, agent describes it; long-press copy; `###` renders as heading.
