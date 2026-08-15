import Foundation

/// How full the model's context window is *right now*.
///
/// Claude reports one API call's prompt usage on each assistant message.
/// Codex's turn completion total spans every model call in the turn. Drover
/// enriches that completion with the native transcript's latest-request usage.
public struct ContextGauge: Sendable, Equatable {
    public let usedTokens: Int
    /// Nil when the newest provider-specific usage payload has no window.
    public let window: Int?

    /// The fraction alone. This sits in the chat screen's subtitle, beside an
    /// inline navigation title that has to share the same line, and a percent
    /// derived from the two numbers printed next to it bought nothing for the
    /// width it took: the title truncated instead ("Validating native
    /// discovery…"), which is the half a reader cannot reconstruct (#170).
    ///
    /// Dropping it also retires the bound this used to need. Turning a ratio
    /// into an Int had to guard against a window of 1 against a trillion
    /// tokens overflowing, and the guard existed only for a number that was
    /// redundant to begin with.
    public var text: String {
        guard let window, window > 0 else { return "ctx \(TokenCount.format(usedTokens))" }
        return "ctx \(TokenCount.format(usedTokens)) / \(TokenCount.format(window))"
    }

    public init?(messages: [HarnessMessage], harness: String? = nil) {
        let normalizedHarness = harness?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalizedHarness == "codex" {
            guard let context = Self.latestCodexContext(messages) else { return nil }
            usedTokens = context.usedTokens
            window = context.window
        } else {
            guard let used = Self.latestClaudePromptTokens(messages) else { return nil }
            usedTokens = used
            window = Self.latestClaudeWindow(messages)
        }
    }

    /// Newest-first: one assistant message's `usage` is one API call.
    /// A `result` payload is explicitly skipped -- its `usage` sibling is a
    /// per-request total, not a per-call one.
    private static func latestClaudePromptTokens(_ messages: [HarnessMessage]) -> Int? {
        for message in messages.reversed() {
            guard message.type == .assistantOutput,
                  message.payload["result"] == nil,
                  let usage = message.payload["usage"]?.objectValue else { continue }
            let input = usage["input_tokens"]?.numberValue ?? 0
            let cacheRead = usage["cache_read_input_tokens"]?.numberValue ?? 0
            let cacheCreation = usage["cache_creation_input_tokens"]?.numberValue ?? 0
            let total = Int((input + cacheRead + cacheCreation).rounded())
            if total > 0 { return total }
        }
        return nil
    }

    /// Only the newest completion is relevant. A missing precise value must not
    /// fall back to cumulative turn usage or an older, stale completion.
    private static func latestCodexContext(
        _ messages: [HarnessMessage]
    ) -> (usedTokens: Int, window: Int?)? {
        for message in messages.reversed() {
            guard message.type == .status,
                  message.payload["turn_complete"]?.boolValue == true else { continue }
            guard let input = nonnegativeInt(
                message.payload["context_input_tokens"]?.numberValue
            ) else { return nil }
            let window = positiveInt(message.payload["model_context_window"]?.numberValue)
            return (input, window)
        }
        return nil
    }

    private static func positiveInt(_ number: Double?) -> Int? {
        guard let value = nonnegativeInt(number), value > 0 else { return nil }
        return value
    }

    private static func nonnegativeInt(_ number: Double?) -> Int? {
        guard let number, number.isFinite else { return nil }
        let rounded = number.rounded()
        guard rounded >= 0, rounded < Double(Int.max) else { return nil }
        return Int(rounded)
    }

    /// `modelUsage` remains the one reliable source for the window itself.
    private static func latestClaudeWindow(_ messages: [HarnessMessage]) -> Int? {
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
