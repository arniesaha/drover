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

    public init?(messages: [HarnessMessage], harness: String? = nil) {
        guard let used = Self.latestPromptTokens(messages, harness: harness) else { return nil }
        usedTokens = used
        window = Self.latestWindow(messages)
    }

    /// Newest-first: one assistant message's `usage` is one API call.
    /// A `result` payload is explicitly skipped -- its `usage` sibling is a
    /// per-request total, not a per-call one.
    private static func latestPromptTokens(_ messages: [HarnessMessage], harness: String?) -> Int? {
        let normalizedHarness = harness?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let window = Self.latestWindow(messages)
        for message in messages.reversed() {
            guard message.type == .assistantOutput,
                  message.payload["result"] == nil,
                  let usage = message.payload["usage"]?.objectValue else { continue }
            if normalizedHarness == "codex" && window == nil {
                return nil
            }
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
