import Foundation

/// How full the model's context window is *right now*.
///
/// Claude reports one API call's prompt usage on each assistant message.
/// Codex reports cumulative input on turn completions, so its current prompt
/// pressure is the delta between consecutive completion totals.
public struct ContextGauge: Sendable, Equatable {
    public let usedTokens: Int
    /// Nil when the newest provider-specific usage payload has no window.
    public let window: Int?

    public var text: String {
        guard let window, window > 0 else { return "ctx \(TokenCount.format(usedTokens))" }
        let rawPercent = (Double(usedTokens) / Double(window) * 100).rounded()
        let percent = rawPercent >= Double(Int.max) ? Int.max : Int(rawPercent)
        return "ctx \(TokenCount.format(usedTokens)) / \(TokenCount.format(window)) · \(percent)%"
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

    /// Codex completion usage is cumulative. The newest minus the preceding
    /// valid sample is the latest prompt; a first or reset sample stands alone.
    private static func latestCodexContext(
        _ messages: [HarnessMessage]
    ) -> (usedTokens: Int, window: Int?)? {
        var samples: [(input: Int, window: Int?)] = []
        for message in messages.reversed() {
            guard message.type == .status,
                  message.payload["turn_complete"]?.boolValue == true,
                  let usage = message.payload["usage"]?.objectValue,
                  let input = positiveInt(usage["input_tokens"]?.numberValue) else { continue }
            let window = positiveInt(message.payload["model_context_window"]?.numberValue)
            samples.append((input, window))
            if samples.count == 2 { break }
        }

        guard let latest = samples.first else { return nil }
        guard samples.count == 2 else { return (latest.input, latest.window) }
        let previous = samples[1].input
        let used = latest.input >= previous ? latest.input - previous : latest.input
        return (used, latest.window)
    }

    private static func positiveInt(_ number: Double?) -> Int? {
        guard let number, number.isFinite else { return nil }
        let rounded = number.rounded()
        guard rounded > 0, rounded < Double(Int.max) else { return nil }
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
