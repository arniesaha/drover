import Foundation
import Observation

/// All logic for the "launch a new session" sheet: which host/harness/cwd/
/// prompt the user has picked, and posting `createSession` against
/// `DroverClient`. Kept free of SwiftUI so it's unit-testable — `LaunchView`
/// only renders this state and forwards user actions.
@MainActor
@Observable
public final class LaunchModel {
    private let client: DroverClient
    private let snapshot: HarnessSnapshot?

    /// Harness names the server can run in a structured (turn-based) mode;
    /// anything else (currently just "shell") only supports a raw PTY.
    static let structuredCapableHarnesses: Set<String> = ["claude-code", "codex", "gemini"]

    public var hostID: String {
        didSet {
            guard oldValue != hostID else { return }
            let newHarnesses = harnesses(forHostID: hostID)
            // Keep the user's pick when the new host also offers it; only
            // reset to the new host's default when it's no longer valid.
            if !newHarnesses.contains(harness) {
                harness = Self.defaultHarness(for: newHarnesses)
            }
        }
    }
    public var harness: String
    public var cwd: String = ""
    public var prompt: String = ""
    public var promptAttachments: [TurnAttachment] = []
    public var selectedModel: String = ""
    public var thinkingEffort: String = ""
    public private(set) var launchError: String?

    public init(client: DroverClient, snapshot: HarnessSnapshot?) {
        self.client = client
        self.snapshot = snapshot
        let firstHost = (snapshot?.hosts ?? []).first { $0.status == "online" }
        self.hostID = firstHost?.id ?? ""
        self.harness = Self.defaultHarness(for: firstHost?.harnesses ?? [])
    }

    /// Online hosts only — offline hosts can't accept a new session.
    public var availableHosts: [HostSummary] {
        (snapshot?.hosts ?? []).filter { $0.status == "online" }
    }

    /// The selected host's enabled harnesses, structured-capable ones first.
    public var availableHarnesses: [String] {
        Self.ordered(harnesses(forHostID: hostID))
    }

    /// Suggestion paths scoped to the selected host — host-tagged entries
    /// from other hosts are dropped, host-agnostic favorites always pass.
    /// Mirrors the web client's datalist filtering.
    public var cwdSuggestions: [String] {
        (snapshot?.cwdSuggestions ?? [])
            .filter { $0.hostID == nil || $0.hostID == hostID }
            .map(\.path)
    }

    /// False only for "shell" — every other harness runs in structured mode.
    public var isStructured: Bool {
        harness != "shell"
    }

    /// Local snapshots lack auth capability metadata, so only known
    /// interactive providers expose the sign-in flow.
    public var supportsInteractiveAuth: Bool {
        Self.structuredCapableHarnesses.contains(harness)
    }

    public var supportsThinkingEffort: Bool {
        HarnessRunPreferences.supportsThinkingEffort(harness)
    }

    /// Posts `createSession` for the current selection. On success returns
    /// the new session id; on failure sets `launchError` (server-authored
    /// text when available) and returns nil.
    public func launch() async -> String? {
        let mode = isStructured ? "structured" : "pty"
        let trimmedPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let effectivePrompt = (isStructured && !trimmedPrompt.isEmpty) ? trimmedPrompt : nil
        let effectiveImages = isStructured ? promptAttachments : []
        let trimmedCwd = cwd.trimmingCharacters(in: .whitespacesAndNewlines)
        let effectiveCwd = trimmedCwd.isEmpty ? nil : trimmedCwd
        let effectiveModel = isStructured ? HarnessRunPreferences.optional(selectedModel) : nil
        let effectiveThinking = (isStructured && supportsThinkingEffort)
            ? HarnessRunPreferences.optional(thinkingEffort)
            : nil

        do {
            let sessionID = try await client.createSession(
                hostID: hostID, harness: harness, mode: mode,
                prompt: effectivePrompt, cwd: effectiveCwd,
                images: effectiveImages,
                model: effectiveModel,
                thinkingEffort: effectiveThinking)
            launchError = nil
            return sessionID
        } catch {
            launchError = Self.errorMessage(for: error)
            return nil
        }
    }

    // MARK: - Private helpers

    private func harnesses(forHostID id: String) -> [String] {
        (snapshot?.hosts ?? []).first { $0.id == id }?.harnesses ?? []
    }

    /// "claude-code" if the host offers it, else the first structured-capable
    /// harness, else whatever the host offers first (e.g. shell-only hosts).
    private static func defaultHarness(for harnesses: [String]) -> String {
        if harnesses.contains("claude-code") { return "claude-code" }
        return ordered(harnesses).first ?? ""
    }

    /// Stable-partitions structured-capable harnesses ahead of the rest,
    /// preserving each group's original relative order.
    private static func ordered(_ harnesses: [String]) -> [String] {
        harnesses.filter { structuredCapableHarnesses.contains($0) }
            + harnesses.filter { !structuredCapableHarnesses.contains($0) }
    }

    private static func errorMessage(for error: Error) -> String {
        switch error {
        case DroverError.badRequest(let message), DroverError.conflict(let message):
            return message
        case DroverError.unauthorized:
            return "token rejected — check Settings"
        default:
            return "\(error)"
        }
    }
}
