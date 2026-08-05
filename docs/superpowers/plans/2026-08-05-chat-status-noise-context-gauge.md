# Chat Status Noise and Context Gauge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the 48% of the chat transcript that is status noise, fix a context gauge that reads ~58x high, and stop repeated send taps from creating duplicate turns.

**Architecture:** All four units are iOS-only — every payload already reaches the phone, so no daemon, hub, or wire change. The transcript folding and the gauge math live in NexusKit as pure, unit-tested logic; the app target only renders what those produce.

**Tech Stack:** Swift 6, SwiftUI, swift-testing (`@Test`/`#expect`), SwiftPM for NexusKit, XcodeGen for the app target.

**Spec:** `docs/superpowers/specs/2026-08-05-chat-status-noise-context-gauge-design.md`

## Global Constraints

- Deployment target iOS 18.0 (`apps/drover/project.yml`).
- `Drover.xcodeproj` is generated and gitignored. After **adding** any file under `apps/drover/Drover/`, run `xcodegen generate` from `apps/drover/`. NexusKit source and test files need no regen.
- NexusKit test loop: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter <name>`
- App build: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`
- Tests in `ChatModelTests` share the process-global `MockURLProtocol.handler`; that suite is `@Suite(.serialized)` and must stay that way.
- `ChatModel` is `@MainActor`. Guard state must be read and written before the first `await` so the check is atomic against other main-actor tasks.

## File Structure

| File | Responsibility |
|---|---|
| `NexusKit/Sources/NexusKit/Transcript.swift` | Modify: fold status runs, route thinking tokens; `TranscriptItem` gains a case and an associated value |
| `NexusKit/Sources/NexusKit/ContextGauge.swift` | Create: live context math from the message list |
| `NexusKit/Sources/NexusKit/ChatModel.swift` | Modify: `isSending` guard, `contextGauge` accessor |
| `NexusKit/Tests/NexusKitTests/TranscriptTests.swift` | Modify: existing `.thinkingRun` assertions gain the new parameter; new fold tests |
| `NexusKit/Tests/NexusKitTests/ContextGaugeTests.swift` | Create: gauge math, incl. the 9,145,279 regression fixture |
| `NexusKit/Tests/NexusKitTests/ChatModelTests.swift` | Modify: single-flight tests |
| `Drover/Screens/Chat/SessionEventsRow.swift` | Create: collapsed status-run row (**needs `xcodegen generate`**) |
| `Drover/Screens/Chat/ThinkingBlock.swift` | Modify: token count in the label |
| `Drover/Screens/Chat/ChatView.swift` | Modify: render the new case, toolbar gauge |
| `Drover/Screens/Chat/Composer.swift` | Modify: disable + spinner while sending |

---

### Task 1: Route thinking tokens into thinking runs

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/Transcript.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/TranscriptTests.swift`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `TranscriptItem.thinkingRun([HarnessMessage], estimatedTokens: Int?)` — the case gains a second associated value. `HarnessMessage.isThinkingTokens: Bool` and `HarnessMessage.estimatedThinkingTokens: Int?`.

- [ ] **Step 1: Update existing `.thinkingRun` assertions to the new shape**

Every current `.thinkingRun([...])` in `TranscriptTests.swift` becomes `.thinkingRun([...], estimatedTokens: nil)`. There are four, in `coalescesConsecutiveThinkingIntoOneRun`, `nonThinkingMessageEndsARun` (two on one line), and `trailingRunIsFlushed`:

```swift
#expect(items == [.thinkingRun([t1, t2], estimatedTokens: nil), .message(answer)])
#expect(items == [.thinkingRun([t1], estimatedTokens: nil), .message(tool), .thinkingRun([t2], estimatedTokens: nil)])
#expect(items.last == .thinkingRun([t1], estimatedTokens: nil))
```

- [ ] **Step 2: Write the failing tests**

Add to `TranscriptTests.swift`, inside `@Suite struct TranscriptGroupingTests`:

```swift
private func thinkingTokens(_ id: String, seq: Int, estimated: Int) -> HarnessMessage {
    HarnessMessage(id: id, seq: seq, type: .status, text: "thinking_tokens",
                   payload: ["subtype": .string("thinking_tokens"),
                             "estimated_tokens": .number(Double(estimated))])
}

@Test func thinkingTokensNeverRenderAsTheirOwnRow() {
    let t1 = thinking("t1", seq: 1)
    let tok = thinkingTokens("k1", seq: 2, estimated: 150)
    let items = TranscriptItem.group([t1, tok])
    #expect(items == [.thinkingRun([t1], estimatedTokens: 150)])
}

@Test func thinkingTokensDoNotBreakARun() {
    let t1 = thinking("t1", seq: 1)
    let tok = thinkingTokens("k1", seq: 2, estimated: 50)
    let t2 = thinking("t2", seq: 3)
    let items = TranscriptItem.group([t1, tok, t2])
    #expect(items == [.thinkingRun([t1, t2], estimatedTokens: 50)])
}

@Test func thinkingTokensKeepTheMaxAcrossTheRun() {
    // estimated_tokens is a running total; out-of-order deltas must not
    // lower the number already reached.
    let t1 = thinking("t1", seq: 1)
    let items = TranscriptItem.group([
        t1,
        thinkingTokens("k1", seq: 2, estimated: 50),
        thinkingTokens("k2", seq: 3, estimated: 1_200),
        thinkingTokens("k3", seq: 4, estimated: 900),
    ])
    #expect(items == [.thinkingRun([t1], estimatedTokens: 1_200)])
}

@Test func thinkingTokensAfterARunClosesAttachToThatRun() {
    let t1 = thinking("t1", seq: 1)
    let answer = output("a", seq: 2)
    let tok = thinkingTokens("k1", seq: 3, estimated: 700)
    let items = TranscriptItem.group([t1, answer, tok])
    #expect(items == [.thinkingRun([t1], estimatedTokens: 700), .message(answer)])
}

@Test func thinkingTokensWithNoRunAtAllAreDropped() {
    let tok = thinkingTokens("k1", seq: 1, estimated: 500)
    #expect(TranscriptItem.group([tok]).isEmpty)
    #expect(TranscriptItem.latestRowID(of: [tok]) == nil)
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter TranscriptGroupingTests`
Expected: compile failure — `.thinkingRun` takes one argument, and `isThinkingTokens` does not exist.

- [ ] **Step 4: Change the case and add the message helpers**

In `Transcript.swift`, change the enum case:

```swift
    /// Always non-empty; ordered as received. `estimatedTokens` is the
    /// running total reported by the harness's `thinking_tokens` events,
    /// which are consumed into the run rather than rendered as rows.
    case thinkingRun([HarnessMessage], estimatedTokens: Int?)
```

Update `var id` for the new shape:

```swift
        case .thinkingRun(let run, _): run[0].id
```

Extend the `HarnessMessage` extension at the bottom of the file:

```swift
    /// Telemetry about the adjacent thinking run, not an event in its own
    /// right: 286 of these landed in one real 741-message session.
    public var isThinkingTokens: Bool {
        type == .status && payload["subtype"]?.stringValue == "thinking_tokens"
    }

    /// Running total, not a delta — callers keep the max across a run.
    public var estimatedThinkingTokens: Int? {
        payload["estimated_tokens"]?.numberValue.map { Int($0.rounded()) }
    }
```

- [ ] **Step 5: Route the events inside `fold`**

In `fold`, add the token accumulator next to `run` and rewrite `flushRun`:

```swift
        var run: [HarnessMessage] = []
        var runTokens: Int?

        func flushRun() {
            guard !run.isEmpty else { return }
            items.append(.thinkingRun(run, estimatedTokens: runTokens))
            run = []
            runTokens = nil
        }

        /// Raise the count on the most recently emitted run — for tokens that
        /// arrive after their run has already been flushed.
        func attachTokensToLastRun(_ value: Int) {
            for index in items.indices.reversed() {
                if case .thinkingRun(let messages, let existing) = items[index] {
                    items[index] = .thinkingRun(messages,
                                                estimatedTokens: max(existing ?? 0, value))
                    return
                }
            }
        }
```

Then, as the **first** branch of the `for message in messages` loop — before the `isThinking` check, so these never flush or break anything:

```swift
            if message.isThinkingTokens {
                guard let value = message.estimatedThinkingTokens else { continue }
                if run.isEmpty {
                    attachTokensToLastRun(value)
                } else {
                    runTokens = max(runTokens ?? 0, value)
                    lastRenderedID = run[0].id
                }
                continue
            }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter TranscriptGroupingTests`
Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 7: Extract the number formatter so both callers share it**

`TokenUsageSummary.format` is `private static`, and the thinking label plus
the Task 4 gauge both need it. Create
`apps/drover/NexusKit/Sources/NexusKit/TokenCount.swift`:

```swift
import Foundation

/// Compact token formatting shared by the usage footer, the thinking row,
/// and the context gauge: 158148 -> "158.1K", 1000000 -> "1M".
public enum TokenCount {
    public static func format(_ value: Int) -> String {
        let absolute = abs(value)
        if absolute >= 1_000_000 { return compact(Double(value) / 1_000_000, suffix: "M") }
        if absolute >= 1_000 { return compact(Double(value) / 1_000, suffix: "K") }
        return "\(value)"
    }

    private static func compact(_ value: Double, suffix: String) -> String {
        let rounded = (value * 10).rounded() / 10
        if rounded.truncatingRemainder(dividingBy: 1) == 0 {
            return "\(Int(rounded))\(suffix)"
        }
        return String(format: "%.1f%@", rounded, suffix)
    }
}
```

In `TokenUsageSummary.swift`, delete the private `format` and `compact`
methods and replace both call sites (in `compactText` and `contextText`)
with `TokenCount.format(...)`.

- [ ] **Step 8: Fix the one app-target call site**

`ChatView.swift:152` destructures the old shape. Change:

```swift
        case .thinkingRun(let run, let estimatedTokens):
            ThinkingBlock(
                run: run,
                estimatedTokens: estimatedTokens,
                isStreaming: isNewest && (model.messages.last?.isThinking ?? false)
            )
```

And add the parameter to `ThinkingBlock` (`ThinkingBlock.swift`), replacing the uninformative "Thought for a bit":

```swift
struct ThinkingBlock: View {
    let run: [HarnessMessage]
    let estimatedTokens: Int?
    /// The newest run keeps streaming into this row; label it accordingly.
    let isStreaming: Bool
    @State private var isExpanded = false

    private var label: String {
        if isStreaming { return "Thinking…" }
        guard let estimatedTokens, estimatedTokens > 0 else { return "Thought for a bit" }
        return "Thought for \(TokenCount.format(estimatedTokens)) tokens"
    }
```

and use `Text(label)` in place of `Text(isStreaming ? "Thinking…" : "Thought for a bit")`.

- [ ] **Step 9: Run the full NexusKit suite, then build the app**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test`
Expected: PASS. `ModelsTests` still expects `"in 18.2K | out 67 | cache 5K | reason 59"` — identical output proves the extraction was behaviour-preserving.

Run: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`
Expected: `BUILD SUCCEEDED`. This task edits app-target files, so build here rather than discovering a broken call site two tasks later. No `xcodegen` needed — `TokenCount.swift` is in NexusKit, not under `Drover/`. If `ThinkingBlock` is constructed in a preview or UI test, the compiler names the file; add `estimatedTokens: nil` there.

- [ ] **Step 10: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit/Sources/NexusKit/Transcript.swift \
        apps/drover/NexusKit/Sources/NexusKit/TokenCount.swift \
        apps/drover/NexusKit/Sources/NexusKit/TokenUsageSummary.swift \
        apps/drover/NexusKit/Tests/NexusKitTests/TranscriptTests.swift \
        apps/drover/Drover/Screens/Chat/ThinkingBlock.swift \
        apps/drover/Drover/Screens/Chat/ChatView.swift
git commit -m "feat(ios): fold thinking_tokens into the thinking run label"
```

---

### Task 2: Fold consecutive status messages into a run

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/Transcript.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/TranscriptTests.swift`

**Interfaces:**
- Consumes: `HarnessMessage.isThinkingTokens` from Task 1.
- Produces: `TranscriptItem.statusRun([HarnessMessage])`, id = first message's id.

- [ ] **Step 1: Write the failing tests**

Add a new suite to `TranscriptTests.swift`:

```swift
@Suite struct StatusFoldingTests {
    private func status(_ id: String, seq: Int, _ text: String) -> HarnessMessage {
        HarnessMessage(id: id, seq: seq, type: .status, text: text,
                       payload: ["subtype": .string(text)])
    }
    private func output(_ id: String, seq: Int) -> HarnessMessage {
        HarnessMessage(id: id, seq: seq, type: .assistantOutput, text: "answer")
    }

    @Test func consecutiveStatusMessagesCollapseIntoOneRun() {
        let s1 = status("s1", seq: 1, "hook_started")
        let s2 = status("s2", seq: 2, "hook_response")
        let s3 = status("s3", seq: 3, "init")
        #expect(TranscriptItem.group([s1, s2, s3]) == [.statusRun([s1, s2, s3])])
    }

    @Test func aNonStatusMessageBreaksTheRun() {
        let s1 = status("s1", seq: 1, "hook_started")
        let answer = output("a", seq: 2)
        let s2 = status("s2", seq: 3, "init")
        #expect(TranscriptItem.group([s1, answer, s2])
                == [.statusRun([s1]), .message(answer), .statusRun([s2])])
    }

    @Test func statusRunIdentityIsItsFirstMessage() {
        let s1 = status("s1", seq: 1, "hook_started")
        let s2 = status("s2", seq: 2, "init")
        #expect(TranscriptItem.group([s1]).first?.id == "s1")
        #expect(TranscriptItem.group([s1, s2]).first?.id == "s1")
    }

    @Test func latestRowIDTargetsTheRunStartForAStatusTail() {
        let answer = output("a", seq: 1)
        let s1 = status("s1", seq: 2, "hook_started")
        let s2 = status("s2", seq: 3, "init")
        #expect(TranscriptItem.latestRowID(of: [answer, s1, s2]) == "s1")
    }

    @Test func aStatusMessageEndsAThinkingRun() {
        let t1 = HarnessMessage(id: "t1", seq: 1, type: .assistantOutput,
                                text: "hmm", payload: ["thinking": .bool(true)])
        let s1 = status("s1", seq: 2, "init")
        #expect(TranscriptItem.group([t1, s1])
                == [.thinkingRun([t1], estimatedTokens: nil), .statusRun([s1])])
    }

    @Test func thinkingTokensAreNeverPartOfAStatusRun() {
        let tok = HarnessMessage(id: "k1", seq: 1, type: .status, text: "thinking_tokens",
                                 payload: ["subtype": .string("thinking_tokens"),
                                           "estimated_tokens": .number(50)])
        let s1 = status("s1", seq: 2, "init")
        #expect(TranscriptItem.group([tok, s1]) == [.statusRun([s1])])
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter StatusFoldingTests`
Expected: compile failure — `.statusRun` does not exist.

- [ ] **Step 3: Add the case**

In `Transcript.swift`:

```swift
    /// Consecutive `.status` messages. They are 48% of a real transcript and
    /// individually meaningless, so they collapse into one row that expands
    /// on demand. Always non-empty.
    case statusRun([HarnessMessage])
```

and in `var id`:

```swift
        case .statusRun(let run): run[0].id
```

- [ ] **Step 4: Fold them**

In `fold`, add the buffer and flush beside the thinking ones:

```swift
        var statusRun: [HarnessMessage] = []

        func flushStatus() {
            guard !statusRun.isEmpty else { return }
            items.append(.statusRun(statusRun))
            statusRun = []
        }
```

Add a status branch immediately before the final fall-through, and add `flushStatus()` everywhere `flushRun()` is already called (the `.toolAction` branch, the fall-through, and the end-of-loop flush). The `isThinking` branch must also flush the status buffer:

```swift
            if message.isThinking {
                flushStatus()
                run.append(message)
                lastRenderedID = run[0].id
                continue
            }
```

The new status branch, placed after the tool-result branch and before the fall-through:

```swift
            if message.type == .status {
                flushRun()
                statusRun.append(message)
                lastRenderedID = statusRun[0].id
                continue
            }
```

The fall-through and the end of the function become:

```swift
            flushRun()
            flushStatus()
            items.append(.message(message))
            lastRenderedID = message.id
        }
        flushRun()
        flushStatus()
        return (items, lastRenderedID)
```

The `.toolAction` branch gains `flushStatus()` next to its existing `flushRun()`. Leave the tool-**result** branch alone: it attaches to an earlier row and deliberately does not flush, which is what `pairsAcrossInterveningMessages` pins.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter "StatusFoldingTests|TranscriptGroupingTests|StepPairingTests"`
Expected: PASS. Note `thinkingFlagOnlyCountsForAssistantOutput` now expects a `.statusRun`, not a `.message` — update it:

```swift
        #expect(TranscriptItem.group([odd]) == [.statusRun([odd])])
```

- [ ] **Step 6: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit/Sources/NexusKit/Transcript.swift \
        apps/drover/NexusKit/Tests/NexusKitTests/TranscriptTests.swift
git commit -m "feat(ios): fold consecutive status messages into one row"
```

---

### Task 3: Render the status run

**Files:**
- Create: `apps/drover/NexusKit/Sources/NexusKit/SessionEventSummary.swift`
- Create: `apps/drover/NexusKit/Tests/NexusKitTests/SessionEventSummaryTests.swift`
- Create: `apps/drover/Drover/Screens/Chat/SessionEventsRow.swift`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`

**Interfaces:**
- Consumes: `TranscriptItem.statusRun([HarnessMessage])` from Task 2.
- Produces: `SessionEventSummary.title(for: [HarnessMessage]) -> String`, `SessionEventSummary.detail(for: HarnessMessage) -> String`, and `SessionEventsRow(run: [HarnessMessage])`.

The label logic lives in NexusKit, not the view: `MessageBubble` sets the
precedent that chat views are "purely presentational", and it is the only
way the spec's single-event requirement can be tested.

- [ ] **Step 1: Write the failing tests**

Create `apps/drover/NexusKit/Tests/NexusKitTests/SessionEventSummaryTests.swift`:

```swift
import Foundation
import Testing
@testable import NexusKit

@Suite struct SessionEventSummaryTests {
    private func status(_ text: String, payload: [String: JSONValue] = [:]) -> HarnessMessage {
        HarnessMessage.fixture(seq: 1, type: .status, text: text, payload: payload)
    }

    @Test func singleEventShowsItsNameNotACount() {
        #expect(SessionEventSummary.title(for: [status("init")]) == "init")
    }

    @Test func multipleEventsShowACount() {
        let run = [status("hook_started"), status("hook_response"), status("init")]
        #expect(SessionEventSummary.title(for: run) == "3 session events")
    }

    @Test func namelessEventFallsBackToAGenericLabel() {
        #expect(SessionEventSummary.title(for: [status("")]) == "session event")
    }

    @Test func hookDetailShowsNameAndOutcome() {
        let message = status("hook_response", payload: [
            "hook_name": .string("SessionStart:startup"),
            "outcome": .string("success"),
        ])
        #expect(SessionEventSummary.detail(for: message)
                == "SessionStart:startup — success")
    }

    @Test func taskDetailShowsItsDescription() {
        let message = status("task_started", payload: [
            "description": .string("Phase 0 over Tailscale"),
        ])
        #expect(SessionEventSummary.detail(for: message)
                == "task_started — Phase 0 over Tailscale")
    }

    @Test func notificationDetailShowsSummaryAndState() {
        let message = status("task_notification", payload: [
            "summary": .string("Read NAS output"),
            "status": .string("completed"),
        ])
        #expect(SessionEventSummary.detail(for: message)
                == "task_notification — Read NAS output (completed)")
    }

    @Test func progressDetailShowsElapsedSeconds() {
        let message = status("tool_progress", payload: [
            "tool_name": .string("Bash"),
            "elapsed_time_seconds": .number(30),
        ])
        #expect(SessionEventSummary.detail(for: message) == "Bash running — 30s")
    }

    @Test func detailFallsBackToTheBareKind() {
        #expect(SessionEventSummary.detail(for: status("vcs_state_changed"))
                == "vcs_state_changed")
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter SessionEventSummaryTests`
Expected: compile failure — `SessionEventSummary` does not exist.

- [ ] **Step 3: Implement it**

Create `apps/drover/NexusKit/Sources/NexusKit/SessionEventSummary.swift`:

```swift
import Foundation

/// Labels for a folded status run. Pure string-building so the row that
/// renders it stays presentational and this stays testable.
public enum SessionEventSummary {
    public static func title(for run: [HarnessMessage]) -> String {
        guard run.count > 1 else {
            return run.first.map(name) ?? "session event"
        }
        return "\(run.count) session events"
    }

    /// Surface the one field that makes each event worth reading; fall back
    /// to the bare kind when a payload has nothing useful.
    public static func detail(for message: HarnessMessage) -> String {
        let kind = name(message)
        if let hook = message.payload["hook_name"]?.stringValue {
            let outcome = message.payload["outcome"]?.stringValue ?? "ran"
            return "\(hook) — \(outcome)"
        }
        if let description = message.payload["description"]?.stringValue {
            return "\(kind) — \(description)"
        }
        if let summary = message.payload["summary"]?.stringValue {
            let state = message.payload["status"]?.stringValue ?? ""
            return state.isEmpty ? "\(kind) — \(summary)" : "\(kind) — \(summary) (\(state))"
        }
        if let elapsed = message.payload["elapsed_time_seconds"]?.numberValue {
            let tool = message.payload["tool_name"]?.stringValue ?? "tool"
            return "\(tool) running — \(Int(elapsed))s"
        }
        return kind
    }

    private static func name(_ message: HarnessMessage) -> String {
        message.text.isEmpty ? "session event" : message.text
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter SessionEventSummaryTests`
Expected: PASS, all eight.

- [ ] **Step 5: Create the view**

`apps/drover/Drover/Screens/Chat/SessionEventsRow.swift`:

```swift
import SwiftUI
import NexusKit

/// One collapsed row per status run (consecutive status messages, grouped by
/// `TranscriptItem.group`). Deliberately recessive and styled to match
/// `ThinkingBlock` so every fold in the transcript reads as one family.
struct SessionEventsRow: View {
    let run: [HarnessMessage]
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "gearshape")
                    Text(SessionEventSummary.title(for: run))
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("session-events-row")
            .accessibilityLabel(isExpanded ? "Collapse session events" : "Expand session events")

            if isExpanded {
                HStack(alignment: .top, spacing: 10) {
                    RoundedRectangle(cornerRadius: 1)
                        .fill(.secondary.opacity(0.35))
                        .frame(width: 2)
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(run) { message in
                            Text(SessionEventSummary.detail(for: message))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
```

- [ ] **Step 6: Render it from ChatView**

In `ChatView.row(for:isNewest:)`, add the case:

```swift
        case .statusRun(let run):
            SessionEventsRow(run: run)
```

- [ ] **Step 7: Regenerate the project (new file added)**

Run: `cd "/Volumes/M2 1/drover/apps/drover" && xcodegen generate`
Expected: `Loaded project`/`Created project at …/Drover.xcodeproj`. Skipping this makes the next step fail with "cannot find 'SessionEventsRow' in scope".

- [ ] **Step 8: Build the app**

Run: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`
Expected: `BUILD SUCCEEDED`.

- [ ] **Step 9: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit/Sources/NexusKit/SessionEventSummary.swift \
        apps/drover/NexusKit/Tests/NexusKitTests/SessionEventSummaryTests.swift \
        apps/drover/Drover/Screens/Chat/SessionEventsRow.swift \
        apps/drover/Drover/Screens/Chat/ChatView.swift
git commit -m "feat(ios): collapsed session-events row for status runs"
```

---

### Task 4: Fix the context gauge

**Files:**
- Create: `apps/drover/NexusKit/Sources/NexusKit/ContextGauge.swift`
- Create: `apps/drover/NexusKit/Tests/NexusKitTests/ContextGaugeTests.swift`
- Modify: `apps/drover/NexusKit/Sources/NexusKit/ChatModel.swift`
- Modify: `apps/drover/NexusKit/Sources/NexusKit/TokenUsageSummary.swift`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`

**Interfaces:**
- Consumes: `TokenCount.format` from Task 1.
- Produces: `ContextGauge(messages:)?` with `usedTokens: Int`, `window: Int?`, `text: String`; and `ChatModel.contextGauge: ContextGauge?`.

- [ ] **Step 1: Write the failing tests**

Create `apps/drover/NexusKit/Tests/NexusKitTests/ContextGaugeTests.swift`:

```swift
import Foundation
import Testing
@testable import NexusKit

/// The numbers here are copied from a real session
/// (harness-571701ec, 2026-08-05) where the shipped code displayed
/// "ctx 9.1M / 1M (914%)" against a 1M window.
@Suite struct ContextGaugeTests {
    private func assistant(seq: Int, input: Int, cacheRead: Int, cacheCreation: Int) -> HarnessMessage {
        HarnessMessage.fixture(
            seq: seq, type: .assistantOutput,
            payload: ["usage": .object([
                "input_tokens": .number(Double(input)),
                "cache_read_input_tokens": .number(Double(cacheRead)),
                "cache_creation_input_tokens": .number(Double(cacheCreation)),
            ])]
        )
    }

    private func result(seq: Int, cumulativeCacheRead: Int, window: Int) -> HarnessMessage {
        HarnessMessage.fixture(
            seq: seq, type: .status, text: "turn complete",
            payload: ["result": .object([
                "modelUsage": .object([
                    "claude-opus-5[1m]": .object([
                        "inputTokens": .number(211),
                        "cacheReadInputTokens": .number(Double(cumulativeCacheRead)),
                        "cacheCreationInputTokens": .number(120_147),
                        "contextWindow": .number(Double(window)),
                    ])
                ])
            ])]
        )
    }

    @Test func usesTheLatestAssistantCallNotTheLifetimeCounter() {
        // modelUsage says 9,145,279; the real prompt was 158,148.
        let messages = [
            assistant(seq: 733, input: 2, cacheRead: 154_527, cacheCreation: 2_446),
            result(seq: 740, cumulativeCacheRead: 9_024_921, window: 1_000_000),
            assistant(seq: 741, input: 2, cacheRead: 156_973, cacheCreation: 1_173),
        ]
        let gauge = ContextGauge(messages: messages)
        #expect(gauge?.usedTokens == 158_148)
        #expect(gauge?.window == 1_000_000)
        #expect(gauge?.text == "ctx 158.1K / 1M · 16%")
    }

    @Test func dropsWhenTheSessionCompacts() {
        let before = [assistant(seq: 1, input: 16, cacheRead: 1_059_493, cacheCreation: 5_809)]
        let after = before + [assistant(seq: 2, input: 2, cacheRead: 156_973, cacheCreation: 1_173)]
        #expect(ContextGauge(messages: before)?.usedTokens == 1_065_318)
        #expect(ContextGauge(messages: after)?.usedTokens == 158_148)
    }

    @Test func showsOverHundredPercentRatherThanClamping() {
        let messages = [
            assistant(seq: 1, input: 16, cacheRead: 1_059_493, cacheCreation: 5_809),
            result(seq: 2, cumulativeCacheRead: 9_024_921, window: 1_000_000),
        ]
        #expect(ContextGauge(messages: messages)?.text == "ctx 1.1M / 1M · 107%")
    }

    @Test func omitsTheDenominatorUntilAWindowIsKnown() {
        let messages = [assistant(seq: 1, input: 2, cacheRead: 156_973, cacheCreation: 1_173)]
        let gauge = ContextGauge(messages: messages)
        #expect(gauge?.window == nil)
        #expect(gauge?.text == "ctx 158.1K")
    }

    @Test func isNilWhenNoPerCallUsageExists() {
        // Gemini's `stats` shape carries no per-call usage.
        let gemini = HarnessMessage.fixture(
            seq: 1, type: .assistantOutput,
            payload: ["stats": .object(["models": .object([:])])]
        )
        #expect(ContextGauge(messages: [gemini]) == nil)
        #expect(ContextGauge(messages: []) == nil)
    }

    @Test func ignoresResultUsageWhichIsAlsoCumulative() {
        // result.usage summed 7,468,690 at num_turns=100 -- not a gauge.
        let resultWithUsage = HarnessMessage.fixture(
            seq: 1, type: .status, text: "turn complete",
            payload: ["result": .object([
                "usage": .object([
                    "input_tokens": .number(181),
                    "cache_read_input_tokens": .number(7_373_739),
                    "cache_creation_input_tokens": .number(94_770),
                ])
            ])]
        )
        #expect(ContextGauge(messages: [resultWithUsage]) == nil)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter ContextGaugeTests`
Expected: compile failure — `ContextGauge` does not exist.

- [ ] **Step 3: Implement `ContextGauge`**

Create `apps/drover/NexusKit/Sources/NexusKit/ContextGauge.swift`:

```swift
import Foundation

/// How full the model's context window is *right now*.
///
/// The obvious sources are both wrong, and both were tried in production:
/// `result.modelUsage` accumulates over the session (10 turns of ~900K cache
/// reads rendered as "9.1M / 1M"), and `result.usage` accumulates over one
/// request's internal turns (7.47M at num_turns=100). Only an individual
/// assistant message's `usage` describes a single API call's prompt, which
/// is what "context used" means -- and it is the only one that falls when
/// the session compacts.
public struct ContextGauge: Sendable, Equatable {
    public let usedTokens: Int
    /// Nil until the first `result` payload arrives with a window.
    public let window: Int?

    public var text: String {
        guard let window, window > 0 else { return "ctx \(TokenCount.format(usedTokens))" }
        let percent = Int((Double(usedTokens) / Double(window) * 100).rounded())
        return "ctx \(TokenCount.format(usedTokens)) / \(TokenCount.format(window)) · \(percent)%"
    }

    public init?(messages: [HarnessMessage]) {
        guard let used = Self.latestPromptTokens(messages) else { return nil }
        usedTokens = used
        window = Self.latestWindow(messages)
    }

    /// Newest-first: one assistant message's `usage` is one API call.
    /// A `result` payload is explicitly skipped -- its `usage` sibling is a
    /// per-request total, not a per-call one.
    private static func latestPromptTokens(_ messages: [HarnessMessage]) -> Int? {
        for message in messages.reversed() {
            guard message.payload["result"] == nil,
                  let usage = message.payload["usage"]?.objectValue else { continue }
            let input = usage["input_tokens"]?.numberValue ?? 0
            let cacheRead = usage["cache_read_input_tokens"]?.numberValue ?? 0
            let cacheCreation = usage["cache_creation_input_tokens"]?.numberValue ?? 0
            let total = Int((input + cacheRead + cacheCreation).rounded())
            if total > 0 { return total }
        }
        return nil
    }

    /// `modelUsage` remains the one reliable source for the window itself.
    private static func latestWindow(_ messages: [HarnessMessage]) -> Int? {
        for message in messages.reversed() {
            guard let result = message.payload["result"]?.objectValue,
                  let modelUsage = result["modelUsage"]?.objectValue else { continue }
            var window: Int?
            for entry in modelUsage.values {
                guard let value = entry.objectValue?["contextWindow"]?.numberValue else { continue }
                window = max(window ?? 0, Int(value.rounded()))
            }
            if let window { return window }
        }
        return nil
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter ContextGaugeTests`
Expected: PASS, all seven.

- [ ] **Step 5: Stop `TokenUsageSummary` reporting a bogus context**

The per-message footer must no longer print the cumulative figure. In
`TokenUsageSummary.parse`, delete these two lines:

```swift
        let modelTotals = parseModelUsage(modelUsage)
        let context = modelTotals.context ?? sum(input, cached)
```

and return nil for both context fields:

```swift
        // Live context is a property of the whole message list, not of one
        // payload -- see ContextGauge. Both aggregates available here are
        // cumulative and were the source of the "9.1M / 1M" reading.
        return (input, output, cached, reasoning, nil, nil)
```

Do **not** keep a `?? sum(input, cached)` fallback: that would leave
`contextTokens` non-nil, and `contextText` would start printing again the
moment any payload carried a window.

Then delete the now-unused `parseModelUsage` function. Keep the `modelUsage`
local — it is still part of the `guard usage != nil || modelUsage != nil`
liveness check, and Swift will warn if you drop it.

Update the now-stale expectation in `ModelsTests.claudeUsageSummaryFormatsContextWindow`:

```swift
    let summary = TokenUsageSummary(message: message)
    #expect(summary?.compactText == "in 6K | out 64 | cache 49.2K")
    // Context now comes from ContextGauge over the whole message list --
    // a single result payload cannot describe live context (see
    // ContextGaugeTests.ignoresResultUsageWhichIsAlsoCumulative).
    #expect(summary?.contextText == nil)
```

Rename that test to `claudeUsageSummaryReportsTotalsButNotContext`.

- [ ] **Step 6: Expose it on ChatModel**

In `ChatModel.swift`, beside the other public accessors:

```swift
    /// Live context pressure for the header gauge; nil when the harness
    /// reports no per-call usage.
    public var contextGauge: ContextGauge? { ContextGauge(messages: messages) }
```

- [ ] **Step 7: Show it in the toolbar**

In `ChatView.toolbarContent`, replace the `.principal` item so the gauge sits under the harness name:

```swift
        ToolbarItem(placement: .principal) {
            VStack(spacing: 0) {
                Label(model.harnessPresentation.name,
                      systemImage: model.harnessPresentation.symbolName)
                    .labelStyle(.titleAndIcon)
                    .font(.headline)
                    .accessibilityIdentifier("chat-harness-title")
                if let gauge = model.contextGauge {
                    Text(gauge.text)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("chat-context-gauge")
                }
            }
        }
```

- [ ] **Step 8: Run the full suite and build**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test`
Expected: PASS.

Run: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`
Expected: `BUILD SUCCEEDED`. No `xcodegen` needed — no file was added under `Drover/`.

- [ ] **Step 9: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit/Sources/NexusKit/ContextGauge.swift \
        apps/drover/NexusKit/Sources/NexusKit/ChatModel.swift \
        apps/drover/NexusKit/Sources/NexusKit/TokenUsageSummary.swift \
        apps/drover/NexusKit/Tests/NexusKitTests/ContextGaugeTests.swift \
        apps/drover/NexusKit/Tests/NexusKitTests/ModelsTests.swift \
        apps/drover/Drover/Screens/Chat/ChatView.swift
git commit -m "fix(ios): context gauge read a lifetime counter as a gauge"
```

---

### Task 5: Single-flight sends

**Files:**
- Modify: `apps/drover/NexusKit/Sources/NexusKit/ChatModel.swift:178-202`
- Modify: `apps/drover/Drover/Screens/Chat/Composer.swift`
- Modify: `apps/drover/Drover/Screens/Chat/ChatView.swift`
- Test: `apps/drover/NexusKit/Tests/NexusKitTests/ChatModelTests.swift`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ChatModel.isSending: Bool` (public, read-only).

- [ ] **Step 1: Write the failing tests**

Add to `ChatModelTests.swift`, inside the existing `@Suite(.serialized) struct ChatModelTests`:

```swift
/// Thread-safe counter: `MockURLProtocol.handler` runs off the main actor.
private final class RequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func bump() { lock.lock(); count += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}

@Test @MainActor func concurrentSendsIssueExactlyOneRequest() async throws {
    // Regression: nine taps during a cellular stall produced nine accepted
    // turns (seq 876, 878-885, all "Yes") because sendTurn had no guard.
    let counter = RequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        Thread.sleep(forTimeInterval: 0.3)   // hold the request in flight
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "Yes"

    async let first: Void = model.sendTurn()
    async let second: Void = model.sendTurn()
    async let third: Void = model.sendTurn()
    _ = await (first, second, third)

    #expect(counter.value == 1)
    #expect(model.composerText == "")
    #expect(model.isSending == false)
}

@Test @MainActor func guardClearsSoTheNextSendStillWorks() async throws {
    let counter = RequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        return (202, Data(#"{"turn_id": "t1"}"#.utf8))
    }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "first"
    await model.sendTurn()
    model.composerText = "second"
    await model.sendTurn()
    #expect(counter.value == 2)
}

@Test @MainActor func failedSendReArmsAndKeepsTheText() async throws {
    MockURLProtocol.transportError = URLError(.notConnectedToInternet)
    defer { MockURLProtocol.transportError = nil }
    let model = ChatModel(client: client(), sessionID: "s1")
    model.composerText = "Yes"
    await model.sendTurn()
    #expect(model.composerText == "Yes")   // retry without retyping
    #expect(model.isSending == false)      // not wedged
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter ChatModelTests`
Expected: compile failure on `model.isSending`; `concurrentSendsIssueExactlyOneRequest` would otherwise report `counter.value == 3`.

- [ ] **Step 3: Add the guard**

In `ChatModel.swift`, add the property beside `pendingApproval`/`hint`:

```swift
    /// True while a turn POST is in flight. The composer disables on this,
    /// so a slow network cannot be mistaken for a dead send button --
    /// which is what produced nine duplicate turns from one message.
    public private(set) var isSending = false
```

Then guard `sendTurn()`. `isSending` is set before the first `await`, so on `@MainActor` the check is atomic against other tasks:

```swift
    public func sendTurn() async {
        guard !isSending else { return }
        let text = composerText.trimmingCharacters(in: .whitespacesAndNewlines)
        let images = pendingAttachments
        guard !text.isEmpty || !images.isEmpty else { return }
        isSending = true
        defer { isSending = false }
        do {
```

Leave the body below unchanged — the failure path still preserves `composerText` and attachments for retry.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test --filter ChatModelTests`
Expected: PASS.

- [ ] **Step 5: Reflect it in the composer**

In `Composer.swift`, add the input and use it for both the disabled state and the icon:

```swift
struct Composer: View {
    @Binding var text: String
    @Binding var attachments: [TurnAttachment]
    let isSending: Bool
    let onSend: () -> Void
```

and replace the send button:

```swift
                Button(action: onSend) {
                    if isSending {
                        ProgressView()
                            .frame(width: 30, height: 30)
                    } else {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.title)
                    }
                }
                .disabled(isEmpty || isSending)
                .accessibilityLabel(isSending ? "Sending" : "Send")
                .accessibilityIdentifier("composer-send")
```

- [ ] **Step 6: Pass it in from ChatView**

At the `Composer(...)` call site in `ChatView.swift`, add the argument:

```swift
                isSending: model.isSending,
```

- [ ] **Step 7: Build**

Run: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`
Expected: `BUILD SUCCEEDED`. If `Composer` is constructed anywhere else (previews, UI tests), the compiler names the file — add `isSending: false` there.

- [ ] **Step 8: Run everything**

Run: `cd "/Volumes/M2 1/drover/apps/drover/NexusKit" && swift test`
Expected: PASS.

Run: `cd "/Volumes/M2 1/drover/apps/drover" && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`
Expected: app unit and UI tests pass.

- [ ] **Step 9: Commit**

```bash
cd "/Volumes/M2 1/drover"
git add apps/drover/NexusKit/Sources/NexusKit/ChatModel.swift \
        apps/drover/NexusKit/Tests/NexusKitTests/ChatModelTests.swift \
        apps/drover/Drover/Screens/Chat/Composer.swift \
        apps/drover/Drover/Screens/Chat/ChatView.swift
git commit -m "fix(ios): single-flight sends so stalls cannot duplicate turns"
```

---

## Manual verification on device

The unit tests cover the logic; these need eyes, and the first two are the
ones the phone screenshots showed:

1. A chat with hooks and background tasks shows `⚙ n session events ›` rows, not a wall of `hook_started` / `thinking_tokens` captions.
2. The header reads a plausible `ctx …/1M · …%` — never millions against a 1M window.
3. A thinking row reads `Thought for N tokens`.
4. With Airplane Mode on, the send button shows a spinner and is not tappable; turning the network back on sends exactly **one** turn.
5. A failed send leaves the typed text in the composer.

## Out of scope

Both deferred by the spec, both cheap once these payloads are read:
`tool_progress.elapsed_time_seconds` onto tool cards stuck on "running…",
and pairing `task_started`/`task_notification` by `task_id` into task rows.
