import Foundation

// MARK: - Widget Fleet Snapshot Models

/// Three-way host state for widget presentation.
public enum WidgetHostPresence: String, Codable, Sendable, Equatable {
    case active
    case idle
    case offline
}

/// A compact quota representation for a single provider subscription row in the widget.
public struct WidgetAccountQuota: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let provider: String
    /// A privacy-safe, compact account identity. Email labels are stored as
    /// their local part only so widgets never render a truncated full address.
    public let accountLabel: String
    /// Subscription tier reported by the accounts API, such as "Pro" or
    /// "Plus". This makes same-provider accounts distinguishable at a glance.
    public let planLabel: String?
    public let remainingFraction: Double?
    public let tightestWindowName: String?
    public let resetsAt: Date?
    public let observedAt: Date?
    public let isStale: Bool
    public let hasWarnings: Bool

    public init(
        id: String,
        provider: String,
        accountLabel: String,
        planLabel: String? = nil,
        remainingFraction: Double?,
        tightestWindowName: String?,
        resetsAt: Date? = nil,
        observedAt: Date? = nil,
        isStale: Bool,
        hasWarnings: Bool
    ) {
        self.id = id
        self.provider = provider
        self.accountLabel = accountLabel
        self.planLabel = planLabel
        self.remainingFraction = remainingFraction
        self.tightestWindowName = tightestWindowName
        self.resetsAt = resetsAt
        self.observedAt = observedAt
        self.isStale = isStale
        self.hasWarnings = hasWarnings
    }

    /// Single uppercase letter or symbol representing the provider.
    public var providerInitial: String {
        switch provider.lowercased() {
        case "anthropic": return "A"
        case "openai": return "O"
        case "google", "gemini": return "G"  // harness-name: display-only initial
        case "codex": return "C"  // harness-name: display-only initial
        default:
            return provider.prefix(1).uppercased()
        }
    }

    public var providerTitle: String {
        switch provider.lowercased() {
        case "openai": return "OpenAI"
        default: return provider.capitalized
        }
    }

    /// The harness names users recognize on the home screen, rather than an
    /// initial badge that makes multiple accounts indistinguishable.
    public var providerDisplayName: String {
        switch provider.lowercased() {
        case "anthropic", "claude": return "Claude"  // harness-name: display-only
        case "openai", "codex": return "Codex"  // harness-name: display-only
        case "google", "gemini", "antigravity": return "Gemini"  // harness-name: display-only
        default: return provider.capitalized
        }
    }

    /// Prefer the reported tier; otherwise use the short, privacy-safe label.
    public var shortLabel: String {
        let plan = planLabel?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return plan.isEmpty ? accountLabel : plan
    }

    public func resetText(now: Date = Date()) -> String? {
        guard let resetsAt else { return nil }
        let seconds = resetsAt.timeIntervalSince(now)
        guard seconds > 0 else { return nil }
        let minutes = max(1, Int(seconds / 60))
        if minutes < 60 { return "resets \(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return "resets \(hours)h" }
        return "resets \(hours / 24)d"
    }

    public func updatedText(now: Date = Date()) -> String? {
        guard let observedAt else { return nil }
        let seconds = max(0, now.timeIntervalSince(observedAt))
        if seconds < 60 { return "just now" }
        if seconds < 3_600 { return "\(Int(seconds / 60))m ago" }
        if seconds < 86_400 { return "\(Int(seconds / 3_600))h ago" }
        return "\(Int(seconds / 86_400))d ago"
    }

    public var remainingPercent: Int? {
        remainingFraction.map { max(0, min(100, Int(($0 * 100).rounded()))) }
    }

    public var remainingText: String {
        remainingPercent.map { "\($0)% left" } ?? "Unknown"
    }

    /// Widgets use an amber treatment below 20% remaining so urgency remains
    /// readable even when color is not the only cue.
    public var isLowQuota: Bool {
        (remainingFraction ?? 1) < 0.20
    }
}

/// A compact host status row in the widget.
public struct WidgetHostStatus: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let name: String
    public let status: WidgetHostPresence
    public let activeSessionCount: Int
    public let hasWaiting: Bool

    public init(
        id: String,
        name: String,
        status: WidgetHostPresence,
        activeSessionCount: Int,
        hasWaiting: Bool
    ) {
        self.id = id
        self.name = name
        self.status = status
        self.activeSessionCount = activeSessionCount
        self.hasWaiting = hasWaiting
    }

    public var isLive: Bool {
        status == .active || status == .idle
    }
}

/// Helper container representing up to 4 visible accounts with "+N" truncation metadata.
public struct WidgetQuotaDisplay: Sendable, Equatable {
    public static let maxVisibleRows = 4

    public let visibleAccounts: [WidgetAccountQuota]
    public let truncatedCount: Int

    public var hasTruncatedAccounts: Bool {
        truncatedCount > 0
    }

    public init(accounts: [WidgetAccountQuota]) {
        if accounts.count > Self.maxVisibleRows {
            self.visibleAccounts = Array(accounts.prefix(Self.maxVisibleRows))
            self.truncatedCount = accounts.count - Self.maxVisibleRows
        } else {
            self.visibleAccounts = accounts
            self.truncatedCount = 0
        }
    }
}

/// Privacy-safe, self-contained snapshot stored in the App Group container
/// for WidgetKit timelines.
///
/// Contains NO session contents, prompt text, or transcripts — only quota
/// percentages, host status dots, active session counts, and needs-you counts.
public struct WidgetFleetSnapshot: Codable, Sendable, Equatable {
    public let fetchedAt: Date
    public let needsYouCount: Int
    public let accounts: [WidgetAccountQuota]
    public let hosts: [WidgetHostStatus]

    public init(
        fetchedAt: Date,
        needsYouCount: Int,
        accounts: [WidgetAccountQuota],
        hosts: [WidgetHostStatus]
    ) {
        self.fetchedAt = fetchedAt
        self.needsYouCount = needsYouCount
        self.accounts = accounts
        self.hosts = hosts
    }

    public var quotaDisplay: WidgetQuotaDisplay {
        WidgetQuotaDisplay(accounts: accounts)
    }

    public var liveHostsCount: Int {
        hosts.filter(\.isLive).count
    }

    public var lowestQuotaAccount: WidgetAccountQuota? {
        accounts.first
    }

    public func isStale(now: Date = Date(), threshold: TimeInterval = 20 * 60) -> Bool {
        now.timeIntervalSince(fetchedAt) > threshold
    }

    /// Builds a privacy-safe snapshot from live DroverKit models.
    public static func build(
        snapshot: HarnessSnapshot?,
        accounts: [ProviderAccount],
        now: Date = Date()
    ) -> WidgetFleetSnapshot {
        let sessions = snapshot?.sessions ?? []
        let rawHosts = snapshot?.hosts ?? []

        // Needs-you count: awaiting approval or input
        let needsYouCount = SessionBuckets(sessions: sessions).needsYou.count

        // Host titles map for subscription grouping
        let hostTitles = Dictionary(
            uniqueKeysWithValues: rawHosts.map { ($0.id, $0.title) }
        )

        // Accounts quota: grouped by subscription, tightest window first
        let subscriptions = ProviderSubscriptionGrouping.group(
            accounts,
            hostTitles: hostTitles,
            now: now
        )
        let preview = ProviderCapacityPreview(subscriptions: subscriptions, now: now)

        let widgetAccounts: [WidgetAccountQuota] = preview.meters.map { meter in
            let matchingSubscription = subscriptions.first { $0.id == meter.id }
            let windowTitle = matchingSubscription?.headline.windowTitle
            return WidgetAccountQuota(
                id: meter.id,
                provider: meter.provider,
                accountLabel: shortAccountLabel(meter.accountLabel),
                planLabel: matchingSubscription?.planLabel,
                remainingFraction: meter.remainingFraction,
                tightestWindowName: windowTitle,
                resetsAt: ProviderHeadline.leadingWindow(matchingSubscription?.windows ?? [])?.resetsAt,
                observedAt: matchingSubscription?.representative.observedAt,
                isStale: meter.isStale,
                hasWarnings: meter.hasWarnings
            )
        }

        // Active sessions per host: needsApproval, needsInput, or working
        let activeSessions = sessions.filter {
            $0.attention == .needsApproval || $0.attention == .needsInput || $0.attention == .working
        }
        let sessionsByHost = Dictionary(grouping: activeSessions, by: \.hostID)

        let widgetHosts: [WidgetHostStatus] = rawHosts.map { host in
            let hostSessions = sessionsByHost[host.id] ?? []
            let activeCount = hostSessions.count
            let hasWaiting = hostSessions.contains {
                $0.attention == .needsApproval || $0.attention == .needsInput
            }

            let presence: WidgetHostPresence
            switch host.presence {
            case .online:
                presence = activeCount > 0 ? .active : .idle
            case .stale, .offline:
                presence = .offline
            }

            return WidgetHostStatus(
                id: host.id,
                name: host.title,
                status: presence,
                activeSessionCount: activeCount,
                hasWaiting: hasWaiting
            )
        }

        return WidgetFleetSnapshot(
            fetchedAt: now,
            needsYouCount: needsYouCount,
            accounts: widgetAccounts,
            hosts: widgetHosts
        )
    }

    private static func shortAccountLabel(_ label: String) -> String {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let at = trimmed.firstIndex(of: "@") else { return trimmed }
        let localPart = String(trimmed[..<at])
        return localPart.isEmpty ? "Account" : localPart
    }

    /// Fictional preview snapshot for WidgetKit gallery and Xcode previews.
    public static func preview() -> WidgetFleetSnapshot {
        WidgetFleetSnapshot(
            fetchedAt: Date().addingTimeInterval(-4 * 60),
            needsYouCount: 2,
            accounts: [
                WidgetAccountQuota(
                    id: "anthropic|pro",
                    provider: "anthropic",
                    accountLabel: "arnab",
                    planLabel: "Pro",
                    remainingFraction: 0.18,
                    tightestWindowName: "5-hour",
                    resetsAt: Date().addingTimeInterval(3 * 60 * 60),
                    observedAt: Date().addingTimeInterval(-4 * 60),
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "openai|plus",
                    provider: "openai",
                    accountLabel: "arniesaha",
                    planLabel: "Plus",
                    remainingFraction: 0.42,
                    tightestWindowName: "Weekly",
                    resetsAt: Date().addingTimeInterval(16 * 60 * 60),
                    observedAt: Date().addingTimeInterval(-4 * 60),
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "google|lab",
                    provider: "google",
                    accountLabel: "Lab",
                    planLabel: nil,
                    remainingFraction: 0.75,
                    tightestWindowName: "Daily",
                    resetsAt: Date().addingTimeInterval(8 * 60 * 60),
                    observedAt: Date().addingTimeInterval(-4 * 60),
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "openai|pro",
                    provider: "openai",
                    accountLabel: "work",
                    planLabel: "Pro",
                    remainingFraction: 0.90,
                    tightestWindowName: "Weekly",
                    resetsAt: Date().addingTimeInterval(2 * 60 * 60),
                    observedAt: Date().addingTimeInterval(-31 * 60),
                    isStale: true,
                    hasWarnings: true
                )
            ],
            hosts: [
                WidgetHostStatus(
                    id: "mac-mini",
                    name: "Mac Mini",
                    status: .active,
                    activeSessionCount: 3,
                    hasWaiting: true
                ),
                WidgetHostStatus(
                    id: "nas",
                    name: "NAS",
                    status: .idle,
                    activeSessionCount: 0,
                    hasWaiting: false
                ),
                WidgetHostStatus(
                    id: "gpu-box",
                    name: "GPU Box",
                    status: .active,
                    activeSessionCount: 1,
                    hasWaiting: false
                ),
                WidgetHostStatus(
                    id: "m4-laptop",
                    name: "M4 Laptop",
                    status: .offline,
                    activeSessionCount: 0,
                    hasWaiting: false
                )
            ]
        )
    }
}

// MARK: - Widget Snapshot Storage

public enum WidgetSnapshotStore {
    public static let appGroupID = "group.com.arnab.drover"
    public static let snapshotFileName = "widget-fleet-snapshot.json"

    public static func containerDirectory(appGroupID: String = appGroupID) -> URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroupID)
    }

    public static func snapshotFileURL(
        appGroupID: String = appGroupID,
        customDirectory: URL? = nil
    ) -> URL? {
        let directory = customDirectory ?? containerDirectory(appGroupID: appGroupID)
        return directory?.appendingPathComponent(snapshotFileName)
    }

    public static func writeSnapshot(
        _ snapshot: WidgetFleetSnapshot,
        appGroupID: String = appGroupID,
        customDirectory: URL? = nil
    ) throws {
        guard let url = snapshotFileURL(appGroupID: appGroupID, customDirectory: customDirectory) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(snapshot)
        try data.write(to: url, options: .atomic)
    }

    public static func readSnapshot(
        appGroupID: String = appGroupID,
        customDirectory: URL? = nil
    ) -> WidgetFleetSnapshot? {
        guard let url = snapshotFileURL(appGroupID: appGroupID, customDirectory: customDirectory),
              let data = try? Data(contentsOf: url)
        else {
            return nil
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(WidgetFleetSnapshot.self, from: data)
    }

    public static func deleteSnapshot(
        appGroupID: String = appGroupID,
        customDirectory: URL? = nil
    ) {
        guard let url = snapshotFileURL(appGroupID: appGroupID, customDirectory: customDirectory) else {
            return
        }
        try? FileManager.default.removeItem(at: url)
    }
}
