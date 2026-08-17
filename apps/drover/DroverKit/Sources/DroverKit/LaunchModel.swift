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

    // MARK: Working-directory completion state

    /// How long typing must pause before the host is asked to complete it.
    /// Injectable so tests need not spend the real interval per keystroke.
    private let completionDebounce: Duration
    /// Directories the selected host offered for the current text.
    public private(set) var liveCompletions: [String] = []
    /// True once a completion request failed outright (504/502/transport).
    /// Not set for a host that answered with an empty list.
    public private(set) var isCompletionHostUnreachable = false
    /// Bumped by every keystroke and host change. A response carrying an old
    /// value is stale by definition and is dropped: cancellation usually
    /// beats it, but a response already in flight when `cancel()` lands would
    /// otherwise overwrite newer results.
    private var completionGeneration = 0
    private var completionTask: Task<Void, Never>?
    /// Untagged suggestion path -> does it exist on `hostID`. Absent means
    /// "not asked yet", and absent shows the path — a broken network must
    /// leave the list as it was, not empty it.
    private var untaggedExistence: [String: Bool] = [:]
    private var existenceTask: Task<Void, Never>?
    private var existenceTaskHostID: String?

    /// Harness names the server can run in a structured (turn-based) mode;
    /// anything else (currently just "shell") only supports a raw PTY.
    static let structuredCapableHarnesses: Set<String> = [
        "claude-code", "codex", "agy", "deepseek-harness"
    ]
    static let interactiveAuthHarnesses: Set<String> = ["claude-code", "codex", "agy"]

    public var hostID: String {
        didSet {
            guard oldValue != hostID else { return }
            // Everything the old host answered about its filesystem is now
            // wrong: which favorites exist there, and which directories the
            // typed text could complete to.
            hostDidChangeForSuggestions()
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
    /// The typed working directory. Every change reschedules the debounced
    /// completion request — see `scheduleCompletion()`.
    public var cwd: String = "" {
        didSet {
            guard oldValue != cwd else { return }
            scheduleCompletion()
        }
    }
    public var prompt: String = ""
    public var promptAttachments: [TurnAttachment] = []
    public private(set) var launchError: String?

    public init(
        client: DroverClient,
        snapshot: HarnessSnapshot?,
        store: HarnessModelCatalogStore = HarnessModelCatalogStore(),
        completionDebounce: Duration = .milliseconds(250)
    ) {
        self.client = client
        self.snapshot = snapshot
        self.completionDebounce = completionDebounce
        self.runPreferences = HarnessModelCatalogState(client: client, store: store)
        let hosts = (snapshot?.hosts ?? []).filter { $0.status == "online" || $0.status == "stale" }
        let firstHost = hosts.first { $0.status == "online" } ?? hosts.first
        self.hostID = firstHost?.id ?? ""
        self.harness = Self.defaultHarness(for: firstHost?.harnesses ?? [])
        self.runPreferences.select(hostID: hostID, harness: harness)
    }

    /// Online and stale hosts, plus the selected host if it just went offline.
    /// Keeping that one offline row prevents a refresh from silently moving
    /// the user's selection to a different machine.
    public var availableHosts: [HostSummary] {
        (snapshot?.hosts ?? []).filter {
            $0.status == "online" || $0.status == "stale"
                || ($0.id == hostID && $0.status == "offline")
        }
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
        if isHostOffline {
            return "Host is offline. Wait for it to reconnect before launching."
        }
        return nil
    }

    /// True when a host and harness are selected and the host is not offline.
    public var canLaunch: Bool {
        selectedHost != nil && !hostID.isEmpty && !harness.isEmpty && !isHostOffline
    }

    /// The selected host's enabled harnesses, structured-capable ones first.
    public var availableHarnesses: [String] {
        Self.ordered(harnesses(forHostID: hostID))
    }

    /// What the suggestions menu shows: curated paths first, then whatever
    /// the host's filesystem completed the typed text to, deduplicated by
    /// exact path.
    ///
    /// Curated entries lead because they are the directories this fleet
    /// actually works in; a sibling on disk that happens to sort earlier is
    /// not a better guess than one the user launched from last week. A
    /// directory that is both keeps the curated position and appears once.
    public var cwdSuggestions: [String] {
        var seen = Set<String>()
        var merged: [String] = []
        for path in curatedSuggestions + liveCompletions where seen.insert(path).inserted {
            merged.append(path)
        }
        return merged
    }

    /// Favorites and recent working directories for the selected host,
    /// narrowed to those the typed text is a prefix of.
    ///
    /// Host-tagged entries are scoped by their tag. Untagged ones — a
    /// favorite the config named no host for — used to pass on every host,
    /// which is how a NAS path ended up offered on a Linux laptop where it
    /// does not exist. They now pass only until `verifyCuratedSuggestions()`
    /// hears back that the selected host does not have them; a path the host
    /// was never asked about, or could not be asked about, still shows.
    public var curatedSuggestions: [String] {
        let typed = cwd.trimmingCharacters(in: .whitespacesAndNewlines)
        return (snapshot?.cwdSuggestions ?? [])
            .filter { suggestion in
                if let taggedHostID = suggestion.hostID { return taggedHostID == hostID }
                return untaggedExistence[suggestion.path] != false
            }
            .map(\.path)
            .filter { typed.isEmpty || $0.lowercased().hasPrefix(typed.lowercased()) }
    }

    /// The one line under the field explaining why live completion is quiet.
    /// Nil while a request is merely in flight, and nil for a host that
    /// answered with no matches — an empty answer is an answer.
    public var cwdSuggestionsHint: String? {
        isCompletionHostUnreachable ? "Can't reach the host — showing saved paths only" : nil
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

    // MARK: - Working-directory completion

    /// Asks the selected host, once, which untagged suggestions it actually
    /// has, and drops the ones it does not. Call it when the sheet appears
    /// and whenever the host changes.
    ///
    /// Host-tagged suggestions need no round trip — the server already knows
    /// where it saw them. Only the untagged ones are ambiguous, and they go
    /// in a single batched request rather than one call per favorite.
    ///
    /// A failed check leaves every untagged path visible. Showing a path that
    /// turns out not to exist costs the user one failed launch; hiding the
    /// only path they use because the network blinked costs them the feature.
    public func verifyCuratedSuggestions() async {
        let host = hostID
        guard !host.isEmpty else { return }
        if let inFlight = existenceTask, existenceTaskHostID == host {
            await inFlight.value
            return
        }

        let paths = untaggedSuggestionPaths
        guard !paths.isEmpty else { return }

        let task = Task { @MainActor in
            do {
                let exists = try await self.client.pathsExist(hostID: host, paths: paths)
                guard host == self.hostID else { return }
                self.untaggedExistence = exists
            } catch {
                guard host == self.hostID else { return }
                self.untaggedExistence = [:]
            }
        }
        existenceTask = task
        existenceTaskHostID = host
        await task.value
        if existenceTaskHostID == host {
            existenceTask = nil
            existenceTaskHostID = nil
        }
    }

    /// Awaits the debounced completion currently scheduled, if any. Tests
    /// use this instead of racing a wall clock.
    func settleCompletion() async {
        await completionTask?.value
    }

    private var untaggedSuggestionPaths: [String] {
        var seen = Set<String>()
        return (snapshot?.cwdSuggestions ?? [])
            .filter { $0.hostID == nil }
            .map(\.path)
            .filter { seen.insert($0).inserted }
    }

    private func hostDidChangeForSuggestions() {
        untaggedExistence = [:]
        liveCompletions = []
        isCompletionHostUnreachable = false
        scheduleCompletion()
    }

    /// Supersedes any pending or in-flight completion and, unless the field
    /// is empty, schedules a fresh one a debounce interval from now.
    ///
    /// An empty field is answered locally: the curated list is the whole
    /// answer, and there is nothing to complete.
    private func scheduleCompletion() {
        completionTask?.cancel()
        completionGeneration &+= 1
        let generation = completionGeneration
        let typed = cwd.trimmingCharacters(in: .whitespacesAndNewlines)
        let host = hostID

        guard !typed.isEmpty, !host.isEmpty else {
            completionTask = nil
            liveCompletions = []
            isCompletionHostUnreachable = false
            return
        }

        let debounce = completionDebounce
        completionTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(for: debounce)
            } catch {
                return  // superseded before the pause elapsed
            }
            guard let self, generation == self.completionGeneration else { return }
            await self.fetchCompletions(path: typed, hostID: host, generation: generation)
        }
    }

    private func fetchCompletions(path: String, hostID host: String, generation: Int) async {
        do {
            let completion = try await client.completePath(hostID: host, path: path)
            guard generation == completionGeneration, host == hostID else { return }
            liveCompletions = completion.entries.map(\.path)
            isCompletionHostUnreachable = false
        } catch {
            guard generation == completionGeneration, host == hostID else { return }
            // A request the next keystroke tore down is not a failed one.
            if let droverError = error as? DroverError, droverError.isCancellation { return }
            liveCompletions = []
            // A 404 means the host does not support path completion (older release),
            // which is distinct from being unreachable (502, 504, transport failure).
            // When unsupported, we keep the unreachable hint quiet and let saved paths show.
            if case DroverError.unavailable = error {
                isCompletionHostUnreachable = false
            } else if case DroverError.httpStatus(let code, _) = error, code == 404 {
                isCompletionHostUnreachable = false
            } else {
                isCompletionHostUnreachable = true
            }
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
