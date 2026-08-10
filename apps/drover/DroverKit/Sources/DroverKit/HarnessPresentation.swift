import Foundation

public struct HarnessPresentation: Sendable, Equatable {
    public let harness: String
    public let name: String
    public let symbolName: String

    public init(_ harness: String) {
        self.harness = harness
        switch harness {
        case "claude-code":
            name = "Claude"
            symbolName = "brain"
        case "codex":
            name = "Codex"
            symbolName = "chevron.left.forwardslash.chevron.right"
        case "agy":
            name = "Antigravity"
            symbolName = "sparkles"
        case "shell":
            name = "Shell"
            symbolName = "terminal"
        default:
            name = harness.isEmpty ? "Session" : harness
            symbolName = "terminal"
        }
    }
}
