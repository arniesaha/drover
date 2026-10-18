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
    public let accountLabel: String
    public let remainingFraction: Double?
    public let tightestWindowName: String?
    public let isStale: Bool
    public let hasWarnings: Bool

    public init(
        id: String,
        provider: String,
        accountLabel: String,
        remainingFraction: Double?,
        tightestWindowName: String?,
        isStale: Bool,
        hasWarnings: Bool
    ) {
        self.id = id
        self.provider = provider
        self.accountLabel = accountLabel
        self.remainingFraction = remainingFraction
        self.tightestWindowName = tightestWindowName
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

    public var remainingPercent: Int? {
        remainingFraction.map { max(0, min(100, Int(($0 * 100).rounded()))) }
    }

    public var remainingText: String {
        remainingPercent.map { "\($0)% left" } ?? "Unknown"
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
                accountLabel: meter.accountLabel,
                remainingFraction: meter.remainingFraction,
                tightestWindowName: windowTitle,
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

    /// Fictional preview snapshot for WidgetKit gallery and Xcode previews.
    public static func preview() -> WidgetFleetSnapshot {
        WidgetFleetSnapshot(
            fetchedAt: Date().addingTimeInterval(-4 * 60),
            needsYouCount: 2,
            accounts: [
                WidgetAccountQuota(
                    id: "anthropic|work",
                    provider: "anthropic",
                    accountLabel: "Work",
                    remainingFraction: 0.18,
                    tightestWindowName: "5-hour",
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "openai|personal",
                    provider: "openai",
                    accountLabel: "Personal",
                    remainingFraction: 0.42,
                    tightestWindowName: "Weekly",
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "google|lab",
                    provider: "google",
                    accountLabel: "Lab",
                    remainingFraction: 0.75,
                    tightestWindowName: "Daily",
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "anthropic|backup",
                    provider: "anthropic",
                    accountLabel: "Backup",
                    remainingFraction: 0.90,
                    tightestWindowName: "5-hour",
                    isStale: true,
                    hasWarnings: true
                ),
                WidgetAccountQuota(
                    id: "codex|eval",  // harness-name: display-only fixture ID
                    provider: "codex",  // harness-name: display-only fixture provider
                    accountLabel: "Eval",
                    remainingFraction: 0.95,
                    tightestWindowName: "Weekly",
                    isStale: false,
                    hasWarnings: false
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
