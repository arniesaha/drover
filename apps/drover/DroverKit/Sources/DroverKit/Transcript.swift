import Foundation

// MARK: - TranscriptItem

/// One row of the rendered transcript. Most messages pass through 1:1, but
/// runs of consecutive thinking messages (assistant output whose payload
/// carries `thinking: true`) coalesce into a single `.thinkingRun` so a long
/// thinking turn renders as one collapsible block instead of a stack of
/// identical disclosure rows.
public enum TranscriptItem: Identifiable, Equatable, Sendable {
    case message(HarnessMessage)
    /// Always non-empty; ordered as received. `estimatedTokens` is the
    /// running total reported by the harness's `thinking_tokens` events,
    /// which are consumed into the run rather than rendered as rows.
    case thinkingRun([HarnessMessage], estimatedTokens: Int?)
    /// Consecutive `.status` messages. They are 48% of a real transcript and
    /// individually meaningless, so they collapse into one row that expands
    /// on demand. Always non-empty.
    case statusRun([HarnessMessage])
    /// Consecutive tool calls, each paired (by payload `tool_use_id`) with
    /// its result. Six `git` calls in a row are one line of transcript, not
    /// six — the run collapses to `6 steps · 42s · all clean` and expands on
    /// demand. Results attach in place as they stream in, so the run keeps
    /// its identity rather than being rebuilt. Always non-empty.
    case stepRun([ToolStep])

    /// A run is identified by its *first* message, so an in-progress run
    /// keeps a stable SwiftUI identity as later chunks join it — the row is
    /// updated in place rather than torn down and rebuilt mid-stream. A step
    /// row is identified by its action, so the row stays stable when the
    /// result attaches (see `case step`).
    public var id: String {
        switch self {
        case .message(let message): message.id
        case .thinkingRun(let run, _): run[0].id
        case .statusRun(let run): run[0].id
        case .stepRun(let steps): steps[0].id
        }
    }

    /// A raw message that remains part of this row even if prepended history
    /// extends a folded run and changes the row's rendered identity.
    public var anchorMessageID: String {
        switch self {
        case .message(let message): message.id
        case .thinkingRun(let run, _): run[0].id
        case .statusRun(let run): run[0].id
        case .stepRun(let steps): steps[0].action.id
        }
    }

    /// Resolves a stable raw-message anchor to the row that renders it after
    /// regrouping. This matters when pagination joins two folded runs: the
    /// original row ID disappears, but its raw message is still present.
    public static func rowID(
        containing messageID: String,
        in messages: [HarnessMessage]
    ) -> String? {
        let rowID = finalizeStreams(messages).rowIDs[messageID] ?? messageID
        return group(messages).first { $0.contains(messageID: rowID) }?.id
    }

    private func contains(messageID: String) -> Bool {
        switch self {
        case .message(let message):
            message.id == messageID
        case .thinkingRun(let run, _), .statusRun(let run):
            run.contains { $0.id == messageID }
        case .stepRun(let steps):
            steps.contains {
                $0.action.id == messageID || $0.result?.id == messageID
            }
        }
    }

    private func contains(streamID: String) -> Bool {
        switch self {
        case .message(let row):
            row.payload["text_stream_id"]?.stringValue == streamID
        case .thinkingRun(let run, _):
            run.contains { $0.payload["text_stream_id"]?.stringValue == streamID }
        default:
            false
        }
    }

    /// Folds the raw message list into render items. O(n); called from the
    /// view layer on each transcript change. Tool actions and results pair
    /// by payload `tool_use_id` into `.step` rows; actions without an id and
    /// results with no pending match fall through to `.message` — this is
    /// what keeps old recorded sessions (no `tool_use_id`) rendering.
    public static func group(_ messages: [HarnessMessage]) -> [TranscriptItem] {
        fold(messages).items
    }

    /// ID of the row the newest raw message updates. Scrolling to
    /// `messages.last!.id` would miss when that id is folded into a run or a
    /// step, but pinned bottom scrolling must use the final grouped item's ID.
    ///
    /// Deliberately *not* `group(messages).last?.id`: a `.step` row updates
    /// in place at its original index when its result arrives (so the row
    /// keeps the action's SwiftUI identity — see `pairsAcrossInterveningMessages`
    /// in TranscriptTests), so a result can be the newest message while its
    /// row sits earlier than messages that streamed in between the action
    /// and the result. `fold` tracks the id each message actually rendered
    /// into as it walks, so this stays correct in that case too.
    public static func latestRowID(of messages: [HarnessMessage]) -> String? {
        if let latest = messages.last, latest.payload["text_stream_id"] != nil {
            return rowID(containing: latest.id, in: messages)
        }
        return fold(messages).lastRenderedID
    }

    /// Fold provider block deltas and snapshots by identity before grouping.
    /// Event IDs and sequences remain distinct in the transport and history.
    /// The first delta owns the row; a snapshot replaces text and adds usage.
    private static func finalizeStreams(_ messages: [HarnessMessage]) -> (rows: [HarnessMessage], rowIDs: [String: String]) {
        var result: [HarnessMessage] = []
        var streams: [String: Int] = [:]
        var rowIDs: [String: String] = [:]
        var pending: [String: Set<Int>] = [:]

        func key(_ streamID: String, thinking: Bool) -> String {
            "\(streamID)|\(thinking)"
        }
        var usageRows: [String: [Int]] = [:]
        for message in messages {
            if message.type == .assistantOutput,
               let streamID = message.payload["text_stream_id"]?.stringValue {
                let streamKey = key(streamID, thinking: message.isThinking)
                let separator = streamID.lastIndex(of: ":")
                let messageID = separator.map { String(streamID[..<$0]) }
                let blockKey = messageID.map { key($0, thinking: message.isThinking) }
                var target = streams[streamKey]
                // 0.6.7 assigned singleton snapshots index 0 even when text
                // streamed at index 1 after thinking. Recover only a unique
                // unfinished block of the same message and content kind.
                if target == nil, message.payload["text_stream_final"]?.boolValue == true,
                   streamID.hasSuffix(":0"), let blockKey,
                   let candidates = pending[blockKey], candidates.count == 1 {
                    target = candidates.first
                }
                if let index = target {
                    streams[streamKey] = index
                    rowIDs[message.id] = result[index].id
                    if message.payload["text_stream_final"]?.boolValue == true {
                        result[index].text = message.text
                    } else if result[index].payload["text_stream_final"]?.boolValue != true {
                        result[index].text += message.text
                    }
                    let canonicalStreamID = result[index].payload["text_stream_id"]
                    result[index].payload.merge(message.payload) { _, new in new }
                    result[index].payload["text_stream_id"] = canonicalStreamID
                    if message.payload["text_stream_final"]?.boolValue == true {
                        result[index].payload.removeValue(forKey: "text_stream_delta")
                        if let blockKey { pending[blockKey]?.remove(index) }
                    }
                } else {
                    streams[streamKey] = result.count
                    rowIDs[message.id] = message.id
                    if message.payload["text_stream_final"]?.boolValue != true, let blockKey {
                        pending[blockKey, default: []].insert(result.count)
                    }
                    if let separator = streamID.lastIndex(of: ":") {
                        usageRows[String(streamID[..<separator]), default: []].append(result.count)
                    }
                    var row = message
                    row.type = .assistantOutput
                    result.append(row)
                }
                continue
            }
            if message.type == .status,
               let messageID = message.payload["usage_message_id"]?.stringValue,
               let usage = message.payload["usage"] {
                for index in usageRows[messageID] ?? [] {
                    result[index].payload["usage"] = usage
                }
            }
            result.append(message)
        }
        let rows = result.map { row in
            guard row.payload["text_stream_id"] != nil else { return row }
            return HarnessMessage(
                id: row.id, seq: row.seq, type: row.type, role: row.role,
                text: row.text, turnID: row.turnID, timestamp: row.timestamp,
                payload: row.payload
            )
        }
        return (rows, rowIDs)
    }

    /// Shared walk behind `group` and `latestRowID` so both stay consistent
    /// with a single pass. O(n) per call; it already ran O(n) and callers
    /// are coalesced to ~8Hz, so this is fine.
    private static func fold(_ messages: [HarnessMessage]) -> (items: [TranscriptItem], lastRenderedID: String?) {
        var items: [TranscriptItem] = []
        items.reserveCapacity(messages.count)
        var run: [HarnessMessage] = []
        var runTokens: Int?
        /// tool_use_id -> where its step sits: which `.stepRun` item, and
        /// which step within it. Two levels because a result can arrive long
        /// after its run stopped accepting new actions.
        var pendingSteps: [String: (item: Int, step: Int)] = [:]
        /// Index of the `.stepRun` still accepting actions, or nil once any
        /// non-step message has broken the run.
        var openStepRun: Int?
        var lastRenderedID: String?

        var statusRun: [HarnessMessage] = []

        func flushRun() {
            guard !run.isEmpty else { return }
            items.append(.thinkingRun(run, estimatedTokens: runTokens))
            run = []
            runTokens = nil
        }

        func flushStatus() {
            guard !statusRun.isEmpty else { return }
            items.append(.statusRun(statusRun))
            statusRun = []
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

        for message in finalizeStreams(messages).rows {
            // First branch on purpose: these are consumed into a run, and
            // must never flush a buffer or break the run they describe.
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
            if message.isThinking {
                flushStatus()
                openStepRun = nil
                run.append(message)
                lastRenderedID = run[0].id
                continue
            }
            if message.type == .toolAction,
               let toolUseID = message.payload["tool_use_id"]?.stringValue {
                flushRun()
                flushStatus()
                if let index = openStepRun, case .stepRun(var steps) = items[index] {
                    steps.append(ToolStep(action: message))
                    items[index] = .stepRun(steps)
                    pendingSteps[toolUseID] = (index, steps.count - 1)
                    lastRenderedID = steps[0].id
                } else {
                    items.append(.stepRun([ToolStep(action: message)]))
                    openStepRun = items.count - 1
                    pendingSteps[toolUseID] = (items.count - 1, 0)
                    lastRenderedID = message.id
                }
                continue
            }
            if message.type == .toolResult,
               let toolUseID = message.payload["tool_use_id"]?.stringValue,
               let slot = pendingSteps.removeValue(forKey: toolUseID),
               case .stepRun(var steps) = items[slot.item],
               steps.indices.contains(slot.step) {
                steps[slot.step].result = message
                items[slot.item] = .stepRun(steps)
                lastRenderedID = steps[0].id
                continue
            }
            if message.type == .status {
                flushRun()
                openStepRun = nil
                statusRun.append(message)
                lastRenderedID = statusRun[0].id
                continue
            }
            flushRun()
            flushStatus()
            openStepRun = nil
            items.append(.message(message))
            lastRenderedID = message.id
        }
        flushRun()
        flushStatus()
        return (items, lastRenderedID)
    }
}

extension HarnessMessage {
    /// A thinking chunk: assistant output flagged `thinking: true` by the
    /// harness driver.
    public var isThinking: Bool {
        type == .assistantOutput && payload["thinking"]?.boolValue == true
    }

    /// Telemetry about the adjacent thinking run, not an event in its own
    /// right: 286 of these landed in one real 741-message session.
    public var isThinkingTokens: Bool {
        type == .status && payload["subtype"]?.stringValue == "thinking_tokens"
    }

    /// Running total, not a delta — callers keep the max across a run.
    public var estimatedThinkingTokens: Int? {
        payload["estimated_tokens"]?.numberValue.map { Int($0.rounded()) }
    }
}
