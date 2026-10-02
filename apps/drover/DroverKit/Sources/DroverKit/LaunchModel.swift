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
    /// True once the host answered that it has no completion routes — a
    /// release older than path completion. Unlike unreachability this does
    /// not pass on its own, so it gets its own words rather than silence (#232).
    public private(set) var isCompletionUnsupported = false
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

    public var hostID: String {
        didSet {
            guard oldValue != hostID else { return }
            // Everything the old host answered about its filesystem is now
            // wrong: which favorites exist there, and which directories the
            // typed text could complete to.
            hostDidChangeForSuggestions()
            // Keep the user's pick when the new host can also launch it; only
            // reset to the new host's default when it's no longer valid.
            if !reconcileHarness() {
                selectRunPreferences()
            }
        }
    }
    public var harness: String {
        didSet {
            guard oldValue != harness else { return }
            selectRunPreferences()
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
        self.harness = firstHost?.launchableOffers.first?.name ?? ""
        selectRunPreferences()
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

    /// True when the selected host is not offline and advertises a launch
    /// mode for the selected harness.
    public var canLaunch: Bool {
        selectedHost != nil && !hostID.isEmpty && !harness.isEmpty && !isHostOffline
            && controls.launchMode != nil
    }

    /// The selected host's launchable harnesses, structured-capable ones first.
    public var availableHarnesses: [String] {
        (selectedHost?.launchableOffers ?? []).map(\.name)
    }

    /// The selected host's label for a harness ID, from its envelope; the raw
    /// ID when the host predates envelope display names.
    public func harnessLabel(_ name: String) -> String {
        selectedHost?.offer(named: name)?.label ?? name
    }

    /// Harnesses the selected host lists but this app cannot launch, each
    /// with the explanation the sheet shows (and VoiceOver reads).
    public var unavailableHarnesses: [(name: String, reason: String)] {
        (selectedHost?.unavailableOffers ?? []).map { ($0.offer.name, $0.reason) }
    }

    /// Why the selected host offers nothing to launch, e.g. a pre-capability
    /// Drover host that needs upgrading. Nil when something can launch.
    public var harnessUnavailableReason: String? {
        guard let selectedHost else { return nil }
        return selectedHost.launchUnavailableReason
    }

    /// Every launch-sheet control for the current selection. Derived from the
    /// snapshot on each read, so a refresh can never leave a control enabled
    /// for a selection the host no longer advertises.
    public var controls: HarnessControls {
        HarnessControls(offer: selectedHost?.offer(named: harness))
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
        if isCompletionHostUnreachable {
            return "Can't reach the host — showing saved paths only"
        }
        if isCompletionUnsupported {
            return "This host doesn't support path completion yet — showing saved paths only"
        }
        return nil
    }

    /// True when the selection launches in structured mode — the only mode
    /// with a starting prompt. Structured wins when both are advertised.
    public var isStructured: Bool {
        controls.launchMode == .structured
    }

    /// Sign-in is offered only when the host advertises interactive auth.
    public var supportsInteractiveAuth: Bool {
        controls.offersSignIn
    }

    /// Model and effort pickers need an advertised catalog and a structured
    /// launch to apply them to.
    public var showsRunPreferences: Bool {
        isStructured && controls.showsModelControls
    }

    /// Attachments ride the starting prompt, so they need structured mode
    /// and an advertised MIME type for what the app produces (JPEG).
    public var canAttachImages: Bool {
        isStructured && controls.acceptsImageAttachments
    }

    /// Posts `createSession` for the current selection. On success returns
    /// the new session id; on failure sets `launchError` (server-authored
    /// text when available) and returns nil.
    public func launch() async -> String? {
        // Fail closed: never ask a host for a mode it did not advertise.
        guard canLaunch, let launchMode = controls.launchMode else {
            launchError = harnessUnavailableReason ?? HarnessCapabilityCopy.noLaunchableHarness
            return nil
        }
        let isStructured = launchMode == .structured
        let trimmedPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let effectivePrompt = (isStructured && !trimmedPrompt.isEmpty) ? trimmedPrompt : nil
        let effectiveImages = canAttachImages ? promptAttachments : []
        let trimmedCwd = cwd.trimmingCharacters(in: .whitespacesAndNewlines)
        let effectiveCwd = trimmedCwd.isEmpty ? nil : trimmedCwd
        let effectiveModel = showsRunPreferences ? runPreferences.modelOverride : nil
        let effectiveThinking = showsRunPreferences ? runPreferences.thinkingEffortOverride : nil

        do {
            let sessionID = try await client.createSession(
                hostID: hostID, harness: harness, mode: launchMode.rawValue,
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
    ///
    /// The harness gets the same treatment: a refresh that withdraws its
    /// launch mode (host upgraded, downgraded, or disabled it) moves the
    /// selection rather than leaving Launch enabled for it, and a refresh
    /// that changes what it advertises re-selects run preferences.
    private func adopt(_ fresh: HarnessSnapshot) {
        let previousControls = controls
        snapshot = fresh
        if !availableHosts.contains(where: { $0.id == hostID }) {
            let firstHost = availableHosts.first { $0.status == "online" } ?? availableHosts.first
            hostID = firstHost?.id ?? ""
            harness = firstHost?.launchableOffers.first?.name ?? ""
            selectRunPreferences()
            return
        }
        if !reconcileHarness(), controls != previousControls {
            selectRunPreferences()
        }
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
        isCompletionUnsupported = false
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
            isCompletionUnsupported = false
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
            isCompletionUnsupported = false
        } catch {
            guard generation == completionGeneration, host == hostID else { return }
            // A request the next keystroke tore down is not a failed one.
            if let droverError = error as? DroverError, droverError.isCancellation { return }
            liveCompletions = []
            let unsupported = Self.isUnsupportedCompletionError(error)
            isCompletionUnsupported = unsupported
            isCompletionHostUnreachable = !unsupported
        }
    }

    /// Whether a failed completion means "this host's release has no
    /// completion routes" rather than "the host could not be asked".
    ///
    /// The hub answers 404 with a "host does not support path completion"
    /// body (kept 404 so older app builds are not told the host is
    /// unreachable); a hub from before #232 passes the host's own 404 through.
    /// `validate()` maps both to `.unavailable` like every 404; the hub's own
    /// unknown-host 404 is told apart by its text, and is a host the hub
    /// cannot route to, so it stays "can't reach". 501 is accepted as well.
    static func isUnsupportedCompletionError(_ error: Error) -> Bool {
        switch error {
        case DroverError.httpStatus(501, _):
            return true
        case DroverError.unavailable(let text):
            return !text.hasPrefix("unknown harness host")
        default:
            return false
        }
    }

    // MARK: - Private helpers

    /// Replaces a harness the selected host can no longer launch with the
    /// host's first launchable one (structured first, then host order).
    /// Returns true when it changed `harness`, whose observer re-selects run
    /// preferences.
    @discardableResult
    private func reconcileHarness() -> Bool {
        let launchable = availableHarnesses
        guard !launchable.contains(harness) else { return false }
        let replacement = launchable.first ?? ""
        guard replacement != harness else { return false }
        harness = replacement
        return true
    }

    /// Points the model catalog at the selection, or at nothing when the
    /// host does not advertise a catalog for it: an unadvertised catalog is
    /// never fetched and its pickers never shown.
    private func selectRunPreferences() {
        runPreferences.select(
            hostID: hostID,
            harness: harness,
            catalogAvailable: showsRunPreferences
        )
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
