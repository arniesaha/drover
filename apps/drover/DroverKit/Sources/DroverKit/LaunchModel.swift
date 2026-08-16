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
    /// True only while a `/harness` request this model owns is in flight.
    private(set) public var isFetchingSnapshot: Bool = false
    private(set) public var snapshot: HarnessSnapshot?
    /// Why the last snapshot fetch failed, if it did. The sheet renders this —
    /// a failed fetch otherwise leaves it with no hosts and no explanation.
    public private(set) var snapshotError: String?
    /// The single in-flight fetch. Concurrent callers await it rather than
    /// starting a second request, so the spinner is raised and lowered once.
    private var fetchTask: Task<Void, Never>?
    public let runPreferences: HarnessModelCatalogState

    /// Harness names the server can run in a structured (turn-based) mode;
    /// anything else (currently just "shell") only supports a raw PTY.
    static let structuredCapableHarnesses: Set<String> = [
        "claude-code", "codex", "agy", "deepseek-harness"
    ]
    static let interactiveAuthHarnesses: Set<String> = ["claude-code", "codex", "agy"]

    public var hostID: String {
        didSet {
            guard oldValue != hostID else { return }
            let newHarnesses = harnesses(forHostID: hostID)
            // Keep the user's pick when the new host also offers it; only
            // reset to the new host's default when it's no longer valid.
            if !newHarnesses.contains(harness) {
                let replacement = Self.defaultHarness(for: newHarnesses)
                if replacement != harness {
                    harness = replacement
                    return
                }
            }
            runPreferences.select(hostID: hostID, harness: harness)
        }
    }
    public var harness: String {
        didSet {
            guard oldValue != harness else { return }
            runPreferences.select(hostID: hostID, harness: harness)
        }
    }
    public var cwd: String = ""
    public var prompt: String = ""
    public var promptAttachments: [TurnAttachment] = []
    public private(set) var launchError: String?

    public init(
        client: DroverClient,
        snapshot: HarnessSnapshot?,
        store: HarnessModelCatalogStore = HarnessModelCatalogStore()
    ) {
        self.client = client
        self.snapshot = snapshot
        self.runPreferences = HarnessModelCatalogState(client: client, store: store)
        let hosts = (snapshot?.hosts ?? []).filter { $0.status == "online" || $0.status == "stale" }
        let firstHost = hosts.first { $0.status == "online" } ?? hosts.first
        self.hostID = firstHost?.id ?? ""
        self.harness = Self.defaultHarness(for: firstHost?.harnesses ?? [])
        self.runPreferences.select(hostID: hostID, harness: harness)
    }

    /// Online and stale hosts — offline hosts can't accept a new session.
    public var availableHosts: [HostSummary] {
        (snapshot?.hosts ?? []).filter { $0.status == "online" || $0.status == "stale" }
    }

    /// The host matching `hostID` in the snapshot, if present.
    public var selectedHost: HostSummary? {
        (snapshot?.hosts ?? []).first { $0.id == hostID }
    }

    /// True when the selected host is stale (heartbeats stopped).
    public var isHostStale: Bool {
        selectedHost?.status == "stale"
    }

    /// True when the selected host is offline.
    public var isHostOffline: Bool {
        selectedHost?.status == "offline"
    }

    /// A human-facing warning when launching against a stale host.
    public var hostWarning: String? {
        if isHostStale {
            return "Host is stale (heartbeats stopped). Sessions may fail to start."
        }
        return nil
    }

    /// True when a host and harness are selected and the host is not offline.
    public var canLaunch: Bool {
        !hostID.isEmpty && !harness.isEmpty && !isHostOffline
    }

    /// The selected host's enabled harnesses, structured-capable ones first.
    public var availableHarnesses: [String] {
        Self.ordered(harnesses(forHostID: hostID))
    }

    /// Suggestion paths scoped to the selected host — host-tagged entries
    /// from other hosts are dropped, untagged ones always pass. A favorite is
    /// tagged when the config names a host for it and untagged when it does
    /// not, which is how a path that exists on one host stays off the others.
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
        Self.interactiveAuthHarnesses.contains(harness)
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
        let effectiveModel = isStructured ? runPreferences.modelOverride : nil
        let effectiveThinking = isStructured ? runPreferences.thinkingEffortOverride : nil

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

    // MARK: - Snapshot loading

    /// Fetches the fleet snapshot only when the sheet opened without one —
    /// the deep-link and cold-start paths that pass `snapshot: nil`.
    ///
    /// A snapshot already in hand is authoritative, empty `cwdSuggestions`
    /// included: the server has no recent sessions to suggest, and asking it
    /// again cannot change that.
    public func loadSnapshotIfNeeded() async {
        guard snapshot == nil else { return }
        await refreshSnapshot()
    }

    /// Re-reads `/harness`. Single-flight: a caller arriving while a fetch is
    /// in flight awaits that one instead of racing a second request, so the
    /// spinner is never cleared out from under a fetch that is still running.
    public func refreshSnapshot() async {
        if let inFlight = fetchTask {
            await inFlight.value
            return
        }

        let task = Task { @MainActor in
            do {
                let fresh = try await self.client.snapshot()
                self.adopt(fresh)
                self.snapshotError = nil
            } catch {
                self.snapshotError = Self.errorMessage(for: error)
            }
        }
        fetchTask = task
        isFetchingSnapshot = true
        await task.value
        fetchTask = nil
        isFetchingSnapshot = false
    }

    /// Installs a freshly fetched snapshot and re-derives the defaults `init`
    /// could not. A sheet that opened before the fleet snapshot arrived starts
    /// with an empty `hostID`, which keeps Launch disabled forever; a snapshot
    /// that no longer lists the selected host would do the same. Either way
    /// the selection is unusable, so it is replaced. A selection the new
    /// snapshot still offers is the user's and stays put.
    private func adopt(_ fresh: HarnessSnapshot) {
        snapshot = fresh
        guard !availableHosts.contains(where: { $0.id == hostID }) else { return }
        let firstHost = availableHosts.first { $0.status == "online" } ?? availableHosts.first
        hostID = firstHost?.id ?? ""
        harness = Self.defaultHarness(for: firstHost?.harnesses ?? [])
        runPreferences.select(hostID: hostID, harness: harness)
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
