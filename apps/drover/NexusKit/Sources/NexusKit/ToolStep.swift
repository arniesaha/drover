import Foundation

/// One tool call paired with its result. `result` is nil while the call is
/// still running — results arrive on their own event and pair back by
/// `tool_use_id`, sometimes after other messages have streamed in between.
public struct ToolStep: Sendable, Equatable, Identifiable {
    public var action: HarnessMessage
    public var result: HarnessMessage?

    public init(action: HarnessMessage, result: HarnessMessage? = nil) {
        self.action = action
        self.result = result
    }

    /// The run keeps the *action's* identity so a row updates in place when
    /// its result attaches rather than being torn down and rebuilt mid-stream.
    public var id: String { action.id }

    public var isRunning: Bool { result == nil }

    /// A step still running has not failed. Only a returned result can fail,
    /// which is what keeps a long-running command from flashing red.
    public var isFailed: Bool {
        guard let result else { return false }
        return Self.isFailure(result)
    }

    /// Single source of truth for "did this tool call fail" — the collapsed
    /// run summary and the expanded row must never disagree about it.
    public static func isFailure(_ result: HarnessMessage) -> Bool {
        let exitCode = result.payload["exit_code"]?.numberValue.map { Int($0) }
        let status = result.payload["status"]?.stringValue
        return (exitCode ?? 0) != 0
            || ["failed", "error"].contains(status)
            || result.payload["is_error"]?.boolValue == true
    }

    public var duration: TimeInterval? {
        guard let start = action.timestamp, let end = result?.timestamp else { return nil }
        let elapsed = end.timeIntervalSince(start)
        return elapsed >= 0 ? elapsed : nil
    }
}

/// The one-line labels every fold in the transcript wears.
///
/// There are exactly three fold species — tool steps, thinking runs and
/// status runs — and they all collapse the same way, so a long transcript
/// reads as a few fold rows plus full-size answers rather than a wall.
/// Keeping all three labels here is what stops them drifting into three
/// different voices.
public enum FoldSummary {
    /// `6 steps · 42s · all clean`
    public static func steps(_ steps: [ToolStep]) -> String {
        guard !steps.isEmpty else { return "no steps" }

        var parts = ["\(steps.count) step\(steps.count == 1 ? "" : "s")"]

        // Wall-clock across the whole run, not the sum of each step: tool
        // calls can overlap, and the number the reader cares about is how
        // long they waited.
        if let start = steps.compactMap({ $0.action.timestamp }).min(),
           let end = steps.compactMap({ $0.result?.timestamp }).max(),
           end > start {
            parts.append(duration(end.timeIntervalSince(start)))
        }

        let failures = steps.filter(\.isFailed).count
        if steps.contains(where: \.isRunning) {
            parts.append("running…")
        } else if failures > 0 {
            parts.append("\(failures) failed")
        } else {
            parts.append("all clean")
        }

        return parts.joined(separator: " · ")
    }

    /// `Thought for 12s · 1.2K tokens`
    public static func thinking(run: [HarnessMessage], estimatedTokens: Int?, isStreaming: Bool) -> String {
        if isStreaming { return "Thinking…" }

        var parts: [String] = []
        if let start = run.compactMap(\.timestamp).min(),
           let end = run.compactMap(\.timestamp).max(),
           end > start {
            parts.append("Thought for \(duration(end.timeIntervalSince(start)))")
        } else {
            parts.append("Thought for a bit")
        }
        if let estimatedTokens, estimatedTokens > 0 {
            parts.append("\(TokenCount.format(estimatedTokens)) tokens")
        }
        return parts.joined(separator: " · ")
    }

    /// `3 status updates · last: indexed 1,204 files`
    public static func status(run: [HarnessMessage]) -> String {
        guard let last = run.last else { return "no updates" }
        let detail = SessionEventSummary.detail(for: last)
        guard run.count > 1 else { return detail }
        return "\(run.count) status updates · last: \(detail)"
    }

    /// `0.3s` · `42s` · `1m 18s`. Sub-10s keeps a decimal because the
    /// difference between a 0.2s and a 3s command is the interesting part.
    public static func duration(_ seconds: TimeInterval) -> String {
        if seconds < 10 {
            return String(format: "%.1fs", seconds)
        }
        if seconds < 60 {
            return "\(Int(seconds.rounded()))s"
        }
        let total = Int(seconds.rounded())
        return "\(total / 60)m \(total % 60)s"
    }
}
