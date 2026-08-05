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
    /// A tool call paired (by payload `tool_use_id`) with its result. The
    /// result attaches in place when it streams in; the row keeps the
    /// action's identity so SwiftUI updates rather than rebuilds it.
    case step(action: HarnessMessage, result: HarnessMessage?)

    /// A run is identified by its *first* message, so an in-progress run
    /// keeps a stable SwiftUI identity as later chunks join it — the row is
    /// updated in place rather than torn down and rebuilt mid-stream. A step
    /// row is identified by its action, so the row stays stable when the
    /// result attaches (see `case step`).
    public var id: String {
        switch self {
        case .message(let message): message.id
        case .thinkingRun(let run, _): run[0].id
        case .step(let action, _): action.id
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

    /// ID of the row the newest message renders in. This is what auto-scroll
    /// must target; scrolling to `messages.last!.id` would miss when that id
    /// is folded into a run or a step.
    ///
    /// Deliberately *not* `group(messages).last?.id`: a `.step` row updates
    /// in place at its original index when its result arrives (so the row
    /// keeps the action's SwiftUI identity — see `pairsAcrossInterveningMessages`
    /// in TranscriptTests), so a result can be the newest message while its
    /// row sits earlier than messages that streamed in between the action
    /// and the result. `fold` tracks the id each message actually rendered
    /// into as it walks, so this stays correct in that case too.
    public static func latestRowID(of messages: [HarnessMessage]) -> String? {
        fold(messages).lastRenderedID
    }

    /// Shared walk behind `group` and `latestRowID` so both stay consistent
    /// with a single pass. O(n) per call; it already ran O(n) and callers
    /// are coalesced to ~8Hz, so this is fine.
    private static func fold(_ messages: [HarnessMessage]) -> (items: [TranscriptItem], lastRenderedID: String?) {
        var items: [TranscriptItem] = []
        items.reserveCapacity(messages.count)
        var run: [HarnessMessage] = []
        var runTokens: Int?
        /// tool_use_id -> index in `items` of the awaiting `.step` row.
        var pendingSteps: [String: Int] = [:]
        var lastRenderedID: String?

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

        for message in messages {
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
                run.append(message)
                lastRenderedID = run[0].id
                continue
            }
            if message.type == .toolAction,
               let toolUseID = message.payload["tool_use_id"]?.stringValue {
                flushRun()
                pendingSteps[toolUseID] = items.count
                items.append(.step(action: message, result: nil))
                lastRenderedID = message.id
                continue
            }
            if message.type == .toolResult,
               let toolUseID = message.payload["tool_use_id"]?.stringValue,
               let index = pendingSteps.removeValue(forKey: toolUseID),
               case .step(let action, nil) = items[index] {
                items[index] = .step(action: action, result: message)
                lastRenderedID = action.id
                continue
            }
            flushRun()
            items.append(.message(message))
            lastRenderedID = message.id
        }
        flushRun()
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
