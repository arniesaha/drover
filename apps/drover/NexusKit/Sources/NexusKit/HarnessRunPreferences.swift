import Foundation

public enum HarnessRunPreferences {
    public static let thinkingEfforts = ["low", "medium", "high", "xhigh", "max"]

    public static func supportsThinkingEffort(_ harness: String) -> Bool {
        harness == "claude-code" || harness == "codex"
    }

    public static func modelSuggestions(for harness: String) -> [String] {
        switch harness {
        case "claude-code":
            return ["sonnet", "opus", "fable"]
        case "codex":
            return ["gpt-5.6-sol", "gpt-5.5"]
        case "gemini":
            return ["gemini-2.5-pro", "gemini-2.5-flash"]
        default:
            return []
        }
    }

    public static func optional(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
