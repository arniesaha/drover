# Chat Copy / Image Attachments / Heading Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Long-press copy on chat bubbles, image attachments that reach the harness agents, and `###` headings rendered as real headings.

**Architecture:** Spec: `docs/superpowers/specs/2026-08-04-chat-copy-images-headings-design.md`. Three independent changes. Headings extend the existing `DisplayBlock.segment()` line-walk. Copy is a SwiftUI `.contextMenu` on `MessageBubble`. Images ride the existing turn POST as base64 JSON (hub forwards verbatim, zero hub route changes); harnessd writes them to `~/.drover/attachments/<session_id>/` and appends `[Attached image: <path>]` lines to the turn text (works for all drivers); the claude driver additionally gets real base64 image content blocks on stdin.

**Tech Stack:** SwiftUI/iOS 18 app + NexusKit SPM package (macOS 14-capable, `swift test` works on this Mac), Python 3.12 stdlib `http.server` daemon, pytest.

## Global Constraints

- Repo root: `/Volumes/M2 1/drover` (space in path — always quote).
- NexusKit tests: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test`. `DisplayBlocksTests` is **XCTest**; `ClientTests`/`ChatModelTests` use **Swift Testing** (`@Test`, `#expect`, `MockURLProtocol`) — match each file's existing style.
- Python tests: `cd "/Volumes/M2 1/drover" && uv run pytest tests/<file> -x -q`.
- CI gate runs `black` — run `uv run black src tests` before every Python commit.
- Segmentation happens once at decode, never in view bodies (existing invariant).
- Image wire shape: `{"text": str, "images": [{"media_type": str, "data_base64": str}]}`.
- Attachment size cap server-side: 10 MB decoded per image. Supported media types: image/jpeg, image/png, image/gif, image/webp.
- Commit after every task; end git commit messages with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `DisplayBlock.heading` segmentation (NexusKit)

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/DisplayBlocks.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/DisplayBlocksTests.swift`

**Interfaces:**
- Produces: `DisplayBlock.heading(level: Int, content: AttributedString)` — new enum case; `segment()` emits it for `#{1,6} ` lines outside fences. Task 2 renders it.

- [ ] **Step 1: Write failing tests** (XCTest style, append to `DisplayBlocksTests`):

```swift
func testHeadingLineBecomesHeadingBlock() {
    let blocks = DisplayBlock.segment("### Results\nbody")
    XCTAssertEqual(blocks.count, 2)
    guard case .heading(let level, let content) = blocks[0] else {
        return XCTFail("expected .heading, got \(blocks[0])")
    }
    XCTAssertEqual(level, 3)
    XCTAssertEqual(String(content.characters), "Results")
}

func testHeadingLevelsOneThroughSix() {
    for level in 1...6 {
        let hashes = String(repeating: "#", count: level)
        let blocks = DisplayBlock.segment("\(hashes) T")
        guard case .heading(let parsed, _) = blocks[0] else {
            return XCTFail("level \(level): expected .heading")
        }
        XCTAssertEqual(parsed, level)
    }
}

func testSevenHashesStaysProse() {
    let blocks = DisplayBlock.segment("####### not a heading")
    guard case .text = blocks[0] else { return XCTFail("expected .text") }
}

func testHashWithoutSpaceStaysProse() {
    let blocks = DisplayBlock.segment("#hashtag")
    guard case .text = blocks[0] else { return XCTFail("expected .text") }
}

func testHashInsideFenceStaysCode() {
    let blocks = DisplayBlock.segment("```\n# comment\n```")
    guard case .code(_, let code) = blocks[0] else { return XCTFail("expected .code") }
    XCTAssertEqual(code, "# comment")
}

func testInlineMarkdownInsideHeadingParsed() {
    let blocks = DisplayBlock.segment("## **bold** title")
    guard case .heading(_, let content) = blocks[0] else { return XCTFail() }
    XCTAssertEqual(String(content.characters), "bold title")
}

func testHeadingBetweenProseSplitsBlocks() {
    let blocks = DisplayBlock.segment("intro\n## Section\noutro")
    XCTAssertEqual(blocks.count, 3)
    guard case .text = blocks[0], case .heading = blocks[1], case .text = blocks[2] else {
        return XCTFail("expected text/heading/text, got \(blocks)")
    }
}
```

- [ ] **Step 2: Run to verify failure**: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter DisplayBlocksTests` — expect compile error (`heading` case missing).

- [ ] **Step 3: Implement.** In `DisplayBlocks.swift`, add the case to the enum:

```swift
case heading(level: Int, content: AttributedString)
```

In `segment()`'s line loop, add a branch after the fence checks (the final `else` currently appends to `proseLines`):

```swift
} else if inFence {
    codeLines.append(line)
} else if let heading = headingBlock(from: trimmed) {
    flushProse()
    blocks.append(heading)
} else {
    proseLines.append(line)
}
```

And the helper below `parseInlineMarkdown`:

```swift
/// A prose line of the form `#{1..6} title` becomes a heading block; the
/// title still gets the inline markdown parse. No space after the hashes,
/// or 7+ hashes, means it's ordinary prose (matches CommonMark ATX rules).
private static func headingBlock(from trimmed: String) -> DisplayBlock? {
    let hashes = trimmed.prefix(while: { $0 == "#" })
    guard (1...6).contains(hashes.count) else { return nil }
    let rest = trimmed.dropFirst(hashes.count)
    guard rest.first == " " else { return nil }
    let title = rest.trimmingCharacters(in: .whitespaces)
    guard !title.isEmpty else { return nil }
    return .heading(level: hashes.count, content: parseInlineMarkdown(title))
}
```

Update the doc comment on `segment()` to mention heading lines.

- [ ] **Step 4: Run full package tests**: `swift test` — all pass (other switches over `DisplayBlock` live only in the app target, not the package, so nothing else breaks here).

- [ ] **Step 5: Commit**: `git add -A apps/drover/NexusKit && git commit -m "feat(ios): segment ATX headings into DisplayBlock.heading"`

---

### Task 2: Render headings + long-press copy (app target)

**Files:**
- Modify: `apps/drover/Drover/Screens/Chat/MessageBubble.swift`

**Interfaces:**
- Consumes: `DisplayBlock.heading(level:content:)` from Task 1; `message.text` (raw markdown source, existing).

- [ ] **Step 1: Handle the new case.** In `assistantBubble`'s `ForEach` switch, add:

```swift
case .heading(let level, let content):
    Text(content)
        .font(headingFont(level))
        .padding(.top, 2)
```

and a private helper:

```swift
private func headingFont(_ level: Int) -> Font {
    switch level {
    case 1, 2: return .title3.bold()
    case 3: return .headline
    default: return .subheadline.bold()
    }
}
```

- [ ] **Step 2: Add copy context menus.** On the assistant bubble's padded `VStack` (after `.background(...)`) and on the user bubble's `Text` (after `.background(...)`):

```swift
.contextMenu {
    Button {
        UIPasteboard.general.string = message.text
    } label: {
        Label("Copy", systemImage: "doc.on.doc")
    }
}
```

`UIPasteboard` needs `import UIKit` at the top of the file (app target is iOS-only).

- [ ] **Step 3: Build check.** The Xcode project file is gitignored; verify the package still builds (`swift build` in NexusKit) and, if a simulator build is available in this session, build the app; otherwise visual verification happens in the final smoke task.

- [ ] **Step 4: Commit**: `git add apps/drover/Drover && git commit -m "feat(ios): render markdown headings and long-press copy on chat bubbles"`

---

### Task 3: `TurnAttachment` + client wire format (NexusKit)

**Files:**
- Create: `apps/drover/NexusKit/Sources/NexusKit/TurnAttachment.swift`
- Modify: `apps/drover/NexusKit/Sources/NexusKit/NexusClient.swift:112-118` (sendTurn), `:211-222` (request timeout)
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/ClientTests.swift`

**Interfaces:**
- Produces: `public struct TurnAttachment: Sendable, Equatable { public var mediaType: String; public var data: Data }` and `NexusClient.sendTurn(sessionID:text:images:)` (`images: [TurnAttachment] = []`). Task 4 consumes both.

- [ ] **Step 1: Write failing tests** (Swift Testing style; follow the `MockURLProtocol.handler` pattern already in `ClientTests.swift` — handler receives the `URLRequest`, return `(202, Data(#"{"turn_id": "turn-1"}"#.utf8))`):

```swift
@Test func sendTurnWithoutImagesOmitsImagesKey() async throws {
    // capture request body via MockURLProtocol, decode JSON,
    // #expect(json["images"] == nil), #expect(json["text"] as? String == "hi")
}

@Test func sendTurnEncodesImagesAsBase64() async throws {
    // client.sendTurn(sessionID: "s", text: "look",
    //                 images: [TurnAttachment(mediaType: "image/jpeg", data: Data([0xFF, 0xD8]))])
    // decode body: images[0]["media_type"] == "image/jpeg",
    // images[0]["data_base64"] == Data([0xFF, 0xD8]).base64EncodedString()
}
```

Write them fully — mirror the neighboring tests' exact mock/capture idiom (read the file first; note the serialization rule comment at its top).

- [ ] **Step 2: Verify failure**: `swift test --filter ClientTests` — compile error (no `images:` parameter).

- [ ] **Step 3: Implement.** New file `TurnAttachment.swift`:

```swift
import Foundation

/// One image attached to an outgoing turn, already downscaled/encoded by
/// the UI layer. `mediaType` is a MIME type the server maps to a file
/// extension (image/jpeg, image/png, image/gif, image/webp).
public struct TurnAttachment: Sendable, Equatable {
    public var mediaType: String
    public var data: Data

    public init(mediaType: String, data: Data) {
        self.mediaType = mediaType
        self.data = data
    }
}
```

`NexusClient.sendTurn` becomes:

```swift
public func sendTurn(sessionID: String, text: String,
                     images: [TurnAttachment] = []) async throws -> String {
    var payload: [String: Any] = ["text": text]
    if !images.isEmpty {
        payload["images"] = images.map {
            ["media_type": $0.mediaType, "data_base64": $0.data.base64EncodedString()]
        }
    }
    let body = try JSONSerialization.data(withJSONObject: payload)
    let path = "/harness/sessions/\(encodePathComponent(sessionID))/turns"
    let data = try await request(path: path, method: "POST", body: body,
                                 timeout: images.isEmpty ? nil : 60)
    let decoded = try decode(TurnResponse.self, from: data)
    return decoded.turnID
}
```

`request` gains a timeout parameter (default preserves current behavior everywhere else):

```swift
private func request(path: String, method: String, body: Data?,
                     timeout: TimeInterval? = nil) async throws -> Data {
    ...
    urlRequest.timeoutInterval = timeout ?? 15
```

- [ ] **Step 4: Run**: `swift test` — all pass.
- [ ] **Step 5: Commit**: `git add -A apps/drover/NexusKit && git commit -m "feat(ios): turn attachments on the client wire (base64 images + 60s timeout)"`

---

### Task 4: `ChatModel` attachment state incl. 409 queueing (NexusKit)

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/ChatModel.swift:174-220`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/ChatModelTests.swift`

**Interfaces:**
- Consumes: `TurnAttachment`, `client.sendTurn(sessionID:text:images:)` from Task 3.
- Produces: `public var pendingAttachments: [TurnAttachment]` (UI binds to it, Task 5); send now allowed when text is empty but attachments exist.

- [ ] **Step 1: Write failing tests** (match `ChatModelTests`' existing fake-client idiom — it already stubs `sendTurn`; extend the stub to record `images` and to optionally throw `NexusError.conflict("turn already in flight")`):

  1. `sendTurnPassesAttachmentsAndClearsThem` — set `composerText = "hi"`, `pendingAttachments = [att]`, await `sendTurn()`, expect recorded images `== [att]`, `pendingAttachments.isEmpty`.
  2. `imageOnlyTurnSends` — empty composer, one attachment, await `sendTurn()`, expect a send with `text == ""` and one image.
  3. `attachmentsSurviveConflictQueueing` — stub throws conflict once; after `sendTurn()`, `pendingAttachments.isEmpty` (moved to queue); then simulate the turn-complete status message (same helper the existing queue test uses) and expect the retried send to carry the attachment.

- [ ] **Step 2: Verify failure**: `swift test --filter ChatModelTests`.

- [ ] **Step 3: Implement.** In `ChatModel`:

```swift
public var pendingAttachments: [TurnAttachment] = []
private var queuedAttachments: [TurnAttachment] = []
```

`sendTurn()` changes (guard + send + conflict branch):

```swift
public func sendTurn() async {
    let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
    let images = pendingAttachments
    guard !text.isEmpty || !images.isEmpty else { return }
    do {
        _ = try await client.sendTurn(sessionID: sessionID, text: text, images: images)
        composerText = ""
        pendingAttachments = []
        hint = nil
    } catch NexusError.conflict(let message) where message == "turn already in flight" {
        queuedTurn = queuedTurn.map { "\($0)\n\(text)" } ?? text
        queuedAttachments.append(contentsOf: images)
        composerText = ""
        pendingAttachments = []
        hint = "Queued — sends when the current response finishes."
    } catch {
        applyHint(for: error, action: "send")
    }
}
```

`dispatchQueuedTurnIfComplete` pops both (`queuedTurn` may be nil when only images are queued — change the guard to `queuedTurn != nil || !queuedAttachments.isEmpty`), and `sendQueued` takes the popped attachments:

```swift
private func dispatchQueuedTurnIfComplete(_ message: HarnessMessage) {
    guard message.type == .status,
          message.payload["turn_complete"]?.boolValue == true,
          queuedTurn != nil || !queuedAttachments.isEmpty
    else { return }
    let text = queuedTurn ?? ""
    let images = queuedAttachments
    queuedTurn = nil
    queuedAttachments = []
    Task { await sendQueued(text, images: images) }
}

private func sendQueued(_ text: String, images: [TurnAttachment]) async {
    do {
        _ = try await client.sendTurn(sessionID: sessionID, text: text, images: images)
        hint = nil
    } catch NexusError.conflict(let message) where message == "turn already in flight" {
        queuedTurn = queuedTurn.map { "\(text)\n\($0)" } ?? (text.isEmpty ? nil : text)
        queuedAttachments = images + queuedAttachments
    } catch {
        composerText = composerText.isEmpty ? text : "\(text)\n\(composerText)"
        pendingAttachments = images + pendingAttachments
        applyHint(for: error, action: "send")
    }
}
```

- [ ] **Step 4: Run**: `swift test` — all pass.
- [ ] **Step 5: Commit**: `git add -A apps/drover/NexusKit && git commit -m "feat(ios): pending/queued image attachments in ChatModel"`

---

### Task 5: Composer picker UI + user-bubble caption (app target)

**Files:**
- Modify: `apps/drover/Drover/Screens/Chat/Composer.swift`, `apps/drover/Drover/Screens/Chat/ChatView.swift:58` (Composer call site), `apps/drover/Drover/Screens/Chat/MessageBubble.swift` (user bubble)
- Create: `apps/drover/Drover/Screens/Chat/ImageDownscaler.swift`

**Interfaces:**
- Consumes: `ChatModel.pendingAttachments` (Task 4), `TurnAttachment` (Task 3).
- Produces: UI only.

- [ ] **Step 1: `ImageDownscaler.swift`:**

```swift
import UIKit

/// Downscales picked images to the vision-model sweet spot before they ride
/// a JSON body over cellular/relay (8 MiB frame cap on relay hosts).
enum ImageDownscaler {
    static func jpegData(from original: Data,
                         maxDimension: CGFloat = 1568,
                         quality: CGFloat = 0.7) -> Data? {
        guard let image = UIImage(data: original) else { return nil }
        let scale = min(1, maxDimension / max(image.size.width, image.size.height))
        guard scale < 1 else { return image.jpegData(compressionQuality: quality) }
        let target = CGSize(width: image.size.width * scale,
                            height: image.size.height * scale)
        let resized = UIGraphicsImageRenderer(size: target).image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        return resized.jpegData(compressionQuality: quality)
    }
}
```

- [ ] **Step 2: Composer.** Add `import PhotosUI`, a binding `@Binding var attachments: [TurnAttachment]` (`import NexusKit`), `@State private var pickerItems: [PhotosPickerItem] = []`. Layout: paperclip `PhotosPicker` button left of the text field; a horizontal thumbnail strip above the input row when `!attachments.isEmpty` (thumbnail via `UIImage(data:)`, 44 pt, tap-to-remove ✕ badge, `accessibilityIdentifier("composer-attachment")`). Send button enabled when there is trimmed text **or** an attachment. Convert picked items in `.onChange(of: pickerItems)`:

```swift
.onChange(of: pickerItems) { _, items in
    guard !items.isEmpty else { return }
    pickerItems = []
    Task {
        for item in items {
            guard let raw = try? await item.loadTransferable(type: Data.self),
                  let jpeg = ImageDownscaler.jpegData(from: raw) else { continue }
            let combined = attachments.reduce(0) { $0 + $1.data.count }
            guard combined + jpeg.count <= 6 * 1024 * 1024 else { continue }
            attachments.append(TurnAttachment(mediaType: "image/jpeg", data: jpeg))
        }
    }
}
```

The `PhotosPicker` button: `PhotosPicker(selection: $pickerItems, maxSelectionCount: 4, matching: .images) { Image(systemName: "paperclip").font(.title3) }` with `accessibilityLabel("Attach image")`.

- [ ] **Step 3: ChatView call site** becomes:

```swift
Composer(text: $model.composerText,
         attachments: $model.pendingAttachments) { Task { await model.sendTurn() } }
```

- [ ] **Step 4: User bubble caption.** In `MessageBubble.userBubble`, show a paperclip line when the echoed `user_input` payload carries attachments (server adds this in Task 7):

```swift
private var attachmentCount: Int {
    if case .array(let items)? = message.payload["attachments"] { return items.count }
    return 0
}
```

and inside the bubble, above/below the text, when `attachmentCount > 0`:

```swift
Label(attachmentCount == 1 ? "1 image" : "\(attachmentCount) images",
      systemImage: "paperclip")
    .font(.caption2)
```

(wrap text+label in a trailing-aligned `VStack` since the bubble currently holds a bare `Text`). Note `message.displayText` stays the empty string for image-only turns — acceptable; the paperclip label is the content.

- [ ] **Step 5: Build + commit**: `swift build` in NexusKit; `git add apps/drover/Drover && git commit -m "feat(ios): photo attachments in composer + attachment caption in sent bubbles"`

---

### Task 6: harnessd saves attachments and augments turn text

**Files:**
- Modify: `src/drover/server/harness/daemon.py` — `DaemonState` (~line 1142, next to `worktrees_dir`) and `_create_turn` (:1691-1718); module-level helper + imports (`base64`, `binascii` — check existing imports first)
- Test: `tests/test_harness_daemon.py`

**Interfaces:**
- Consumes: existing `structured.send_turn(session_id, text)`.
- Produces: `save_turn_attachments(attachments_dir: Path, session_id: str, images: list) -> list[dict]` returning `[{"path": str, "media_type": str, "data_b64": str}]`; `_create_turn` calls `structured.send_turn(session_id, text, images=saved or None)` (Task 7 adds that parameter — in THIS task keep the call two-arg and only append paths to text, so the daemon works standalone; the `images=` kwarg is added in Task 7's step 6).

- [ ] **Step 1: Write failing tests** (follow `test_structured_turn_appends_user_input_and_seq_is_monotonic` at `tests/test_harness_daemon.py:1661` — `_start_test_server(tmp_path)`, create a `FAKE_STRUCTURED_CLI` claude-code session, answer the approval, then post turns; set `state.attachments_dir = tmp_path / "attachments"` right after `_start_test_server`):

```python
ONE_PX_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakebody").decode()

def test_turn_with_image_saves_file_and_appends_path(tmp_path):
    # ... session setup as in test at :1661 ...
    status, body = _json_request(
        f"{base_url}/sessions/{sid}/turns",
        payload={
            "text": "look at this",
            "images": [{"media_type": "image/png", "data_base64": ONE_PX_PNG_B64}],
        },
    )
    assert status == 202
    saved = list((tmp_path / "attachments" / sid).glob("*.png"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"\x89PNG\r\n\x1a\nfakebody"

    def _has_augmented_user_input():
        return any(
            e.event_type == "user_input"
            and "look at this" in e.payload.get("text", "")
            and f"[Attached image: {saved[0]}]" in e.payload.get("text", "")
            for e in state.registry.list_events(sid)
        )
    _wait_until(_has_augmented_user_input)

def test_image_only_turn_is_accepted(tmp_path):
    # same setup; payload has no "text", one image; expect 202 and a
    # user_input event whose text is exactly the attachment line

def test_turn_with_bad_base64_is_rejected(tmp_path):
    # payload={"text": "x", "images": [{"media_type": "image/png", "data_base64": "!!!"}]}
    # expect HTTPError 400 with "invalid base64" in error; no files under attachments dir

def test_turn_with_unsupported_media_type_is_rejected(tmp_path):
    # media_type "application/pdf" -> 400 "unsupported media_type"

def test_empty_turn_still_rejected(tmp_path):
    # payload={"text": ""} (no images) -> 400 "text or images required"
```

Write them fully with the session-setup boilerplate copied from the `:1661` test (including the `finally:` teardown).

- [ ] **Step 2: Verify failure**: `uv run pytest tests/test_harness_daemon.py -x -q -k "image or empty_turn"` — new tests fail (402 saved files missing / 400 not raised), existing pass.

- [ ] **Step 3: Implement.** `DaemonState` gains (next to `worktrees_dir`):

```python
attachments_dir: Path = field(
    default_factory=lambda: Path.home() / ".drover" / "attachments"
)
```

Module-level helper (near `worktree` helpers; add `import base64, binascii` if absent):

```python
_ATTACHMENT_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def save_turn_attachments(
    attachments_dir: Path, session_id: str, images: list
) -> list[dict[str, str]]:
    """Decode and persist per-turn images; raises ValueError on any bad
    entry (nothing is half-written before validation of that entry)."""
    saved: list[dict[str, str]] = []
    target = attachments_dir / session_id
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise ValueError(f"images[{index}] must be an object")
        media_type = str(image.get("media_type") or "")
        extension = _ATTACHMENT_EXTENSIONS.get(media_type)
        if extension is None:
            raise ValueError(f"unsupported media_type: {media_type!r}")
        encoded = str(image.get("data_base64") or "")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ValueError(f"invalid base64 in images[{index}]") from exc
        if not data:
            raise ValueError(f"images[{index}] is empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"images[{index}] exceeds {MAX_ATTACHMENT_BYTES} bytes")
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{uuid4().hex[:12]}-{index + 1}.{extension}"
        path.write_bytes(data)
        saved.append(
            {"path": str(path), "media_type": media_type, "data_b64": encoded}
        )
    return saved
```

`_create_turn` body between `body = self._read_json() or {}` and the `try:` becomes:

```python
body = self._read_json() or {}
text = str(body.get("text") or "").strip()
images = body.get("images") or []
if not text and not images:
    self._write_json(
        {"error": "text or images required"}, status=HTTPStatus.BAD_REQUEST
    )
    return
try:
    saved = save_turn_attachments(
        self.server.state.attachments_dir, session_id, images
    )
except ValueError as exc:
    self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
    return
for item in saved:
    line = f"[Attached image: {item['path']}]"
    text = f"{text}\n\n{line}" if text else line
```

(`uuid4` is already imported in daemon.py — verify, else add.)

- [ ] **Step 4: Run**: `uv run pytest tests/test_harness_daemon.py -x -q` — all pass. `uv run black src tests`.
- [ ] **Step 5: Commit**: `git add src/drover/server/harness/daemon.py tests/test_harness_daemon.py && git commit -m "feat(harnessd): accept base64 image attachments on turns"`

---

### Task 7: images through manager to drivers (claude gets real image blocks)

**Files:**
- Modify: `src/drover/server/harness/structured/manager.py:185-200`, `structured/claude.py:222-232`, `structured/codex.py:123`, `structured/gemini.py:152`, `src/drover/server/harness/daemon.py` (`_create_turn` call site)
- Test: `tests/test_structured_manager.py`, `tests/test_structured_claude.py`

**Interfaces:**
- Consumes: `saved` list shape from Task 6 (`[{"path", "media_type", "data_b64"}]`).
- Produces: `StructuredSessionManager.send_turn(session_id, text, images=None)`; driver signature `send_turn(self, text, turn_id, images=None)` across claude/codex/gemini; `user_input` payload gains `attachments: [{"path", "media_type"}]`.

- [ ] **Step 1: Failing tests.**

`tests/test_structured_claude.py` (follow its existing driver-construction idiom — the codex test at the runbook builds drivers with `(argv, None, lambda m: None)`; the claude tests capture `send_line` output):

```python
def test_send_turn_with_images_appends_image_blocks():
    sent = []
    driver = ClaudeDriver(["claude"], None, lambda m: None)
    driver.send_line = lambda obj: sent.append(obj)  # or the file's existing capture idiom
    driver.send_turn(
        "look",
        "turn-1",
        images=[{"path": "/tmp/a.png", "media_type": "image/png", "data_b64": "QUJD"}],
    )
    content = sent[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }

def test_send_turn_without_images_unchanged():
    # content == [{"type": "text", "text": "look"}] exactly
```

`tests/test_structured_manager.py` (follow its fake-driver idiom):

```python
def test_send_turn_forwards_images_and_records_attachments():
    # fake driver records send_turn kwargs; manager.send_turn(sid, "t", images=[...])
    # assert driver got images; assert emitted user_input payload["attachments"]
    # == [{"path": "/tmp/a.png", "media_type": "image/png"}]  (no data_b64 leak)
```

- [ ] **Step 2: Verify failure**: `uv run pytest tests/test_structured_claude.py tests/test_structured_manager.py -x -q`.

- [ ] **Step 3: Implement drivers.** `claude.py`:

```python
def send_turn(self, text: str, turn_id: str, images: list | None = None) -> None:
    del turn_id  # not part of Claude's wire shape; caller-side bookkeeping only
    content: list[dict] = [{"type": "text", "text": text}]
    for image in images or []:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["media_type"],
                    "data": image["data_b64"],
                },
            }
        )
    self.send_line(
        {"type": "user", "message": {"role": "user", "content": content}}
    )
```

`codex.py:123` and `gemini.py:152` signatures become `def send_turn(self, text: str, turn_id: str, images: list | None = None) -> None:` with `del images  # path lines in the text are the whole mechanism here` as the first line (keep existing bodies).

- [ ] **Step 4: Implement manager.** `manager.py`:

```python
def send_turn(
    self, session_id: str, text: str, images: list | None = None
) -> str:
    entry = self._require_entry(session_id)
    if entry.awaiting == "approval":
        raise PermissionError("approval pending; answer it first")
    turn_id = f"turn-{uuid4()}"
    # Dispatch first: Codex/Gemini raise RuntimeError here ("turn
    # already in flight" / "driver is closed") when a turn can't be
    # accepted, and we must not record a user_input event for a turn
    # that was never actually sent.
    entry.driver.send_turn(text, turn_id, images=images)
    payload: dict = {}
    if images:
        payload["attachments"] = [
            {"path": i["path"], "media_type": i["media_type"]} for i in images
        ]
    entry.driver.emit(
        StructuredMessage(
            type="user_input", role="user", text=text, turn_id=turn_id,
            payload=payload,
        )
    )
    return turn_id
```

- [ ] **Step 5: Wire the daemon call site** (from Task 6): `structured.send_turn(session_id, text)` → `structured.send_turn(session_id, text, images=saved or None)`.

- [ ] **Step 6: Run the whole harness suite**: `uv run pytest tests/test_structured_claude.py tests/test_structured_codex.py tests/test_structured_gemini.py tests/test_structured_manager.py tests/test_structured_driver.py tests/test_structured_e2e.py tests/test_harness_daemon.py -q` — all pass. `uv run black src tests`.
- [ ] **Step 7: Commit**: `git commit -am "feat(harness): forward turn images to drivers; claude gets base64 image blocks"`

---

### Task 8: hub timeout bump for turn forwards

**Files:**
- Modify: `src/drover/server/metrics.py:584-607` (`proxy_harness_session_action`)
- Test: `tests/test_metrics.py` (only if an existing test asserts the forwarded timeout — check; otherwise no new test for a constant)

**Interfaces:** none new.

- [ ] **Step 1:** In `proxy_harness_session_action`, forward turns with a longer budget so image bodies survive the hub→host hop (relay floor already exists):

```python
timeout_s = 60.0 if action == "turns" else 15.0
```

and pass `timeout_s=timeout_s` to the `_harness_request(...)` call (read the exact current call first; keep other actions at the default).

- [ ] **Step 2:** `uv run pytest tests/test_metrics.py -q -k harness` — pass. `uv run black src`.
- [ ] **Step 3: Commit**: `git commit -am "feat(hub): 60s forward timeout for harness turn posts (image bodies)"`

---

### Task 9: full verification + push

- [ ] **Step 1:** `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test` — full package green.
- [ ] **Step 2:** `cd "/Volumes/M2 1/drover" && uv run pytest -x -q` — full Python suite green (long; use `-x` to fail fast).
- [ ] **Step 3:** `uv run black --check src tests` — clean (CI gates on this).
- [ ] **Step 4:** Review `git log --oneline origin/main..main`, then `git push origin main`.
- [ ] **Step 5:** Note for deploy: Mac harnessd restart picks up daemon changes (editable install); NAS deploy via the `ssh -tt` runbook path; app needs an Xcode build to the phone. Manual smoke: send an image to a claude session, long-press-copy a reply, confirm `###` renders as a heading.
