import Foundation

// MARK: - TranscriptItem

/// One row of the rendered transcript. Most messages pass through 1:1, but
/// runs of consecutive thinking messages (assistant output whose payload
/// carries `thinking: true`) coalesce into a single `.thinkingRun` so a long
/// thinking turn renders as one collapsible block instead of a stack of
/// identical disclosure rows.
public enum TranscriptItem: Identifiable, Equatable, Sendable {
    case message(HarnessMessage)
    /// Always non-empty; ordered as received.
    case thinkingRun([HarnessMessage])

    /// A run is identified by its *first* message, so an in-progress run
    /// keeps a stable SwiftUI identity as later chunks join it — the row is
    /// updated in place rather than torn down and rebuilt mid-stream.
    public var id: String {
        switch self {
        case .message(let message): message.id
        case .thinkingRun(let run): run[0].id
        }
    }

    /// Folds the raw message list into render items. O(n); called from the
    /// view layer on each transcript change.
    public static func group(_ messages: [HarnessMessage]) -> [TranscriptItem] {
        var items: [TranscriptItem] = []
        items.reserveCapacity(messages.count)
        var run: [HarnessMessage] = []

        func flushRun() {
            guard !run.isEmpty else { return }
            items.append(.thinkingRun(run))
            run = []
        }

        for message in messages {
            if message.isThinking {
                run.append(message)
            } else {
                flushRun()
                items.append(.message(message))
            }
        }
        flushRun()
        return items
    }

    /// ID of the row the newest message renders in — the message's own id,
    /// unless it's part of a trailing thinking run, in which case the run's
    /// first id (see `id`). This is what auto-scroll must target; scrolling
    /// to `messages.last!.id` would miss when that id is folded into a run.
    public static func latestRowID(of messages: [HarnessMessage]) -> String? {
        guard let last = messages.last else { return nil }
        guard last.isThinking else { return last.id }
        var runStart = last
        for message in messages.reversed().dropFirst() {
            guard message.isThinking else { break }
            runStart = message
        }
        return runStart.id
    }
}

extension HarnessMessage {
    /// A thinking chunk: assistant output flagged `thinking: true` by the
    /// harness driver.
    public var isThinking: Bool {
        type == .assistantOutput && payload["thinking"]?.boolValue == true
    }
}
