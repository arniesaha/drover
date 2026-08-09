import Foundation

public enum AnalyticsDistributionSection: String, CaseIterable, Sendable {
    case projects, harnesses, hosts, models

    public var title: String { rawValue.capitalized }
}

public struct ObservedAggregatePresentation: Sendable, Equatable {
    public let sourceText: String
    public let freshnessText: String
    public let coverageText: String

    public init(
        metadata: ObservedAggregateMetadata?,
        fallbackCoverage: Coverage,
        now: Date = .now
    ) {
        sourceText = metadata?.source == "drover_observed"
            ? "Drover observed" : "Observed source unavailable"
        let coverage = metadata?.coverage ?? fallbackCoverage
        coverageText = coverage.tokenPercent.map {
            "\(Self.number($0))% token coverage"
        } ?? "Token coverage unavailable"
        guard let observedAt = metadata?.observedAt else {
            freshnessText = "Observation time unavailable"
            return
        }
        let seconds = max(0, now.timeIntervalSince(observedAt))
        let age: String
        if seconds < 60 {
            age = "Updated now"
        } else if seconds < 3_600 {
            age = "Updated \(Int(seconds / 60))m ago"
        } else if seconds < 86_400 {
            age = "Updated \(Int(seconds / 3_600))h ago"
        } else {
            age = "Updated \(Int(seconds / 86_400))d ago"
        }
        let state = metadata?.freshness.rawValue.capitalized ?? "Unknown"
        freshnessText = "\(age) · \(state)"
    }

    private static func number(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(format: "%.1f", value)
    }
}

public enum ContentAnalysisMode: String, CaseIterable, Identifiable, Sendable, Equatable {
    case disabled
    case local
    case cloud

    public var id: Self { self }
    public var title: String { rawValue.capitalized }
}

public struct ContentAnalysisPropagationPresentation: Sendable, Equatable {
    public let isWarning: Bool
    public let title: String
    public let hostLines: [String]
    public let accessibilityLabel: String

    public init(
        status: ContentAnalysisStatus,
        outcome: ContentAnalysisMutationOutcome
    ) {
        isWarning = outcome != .complete
        switch outcome {
        case .complete: title = "Fleet propagation complete"
        case .partial: title = "Fleet propagation incomplete"
        case .failed: title = "Fleet propagation failed"
        }
        hostLines = status.affectedHosts.map(Self.hostLine)
        let accessibleHosts = status.affectedHosts.map {
            let state = Self.stateText($0.state)
            let error = Self.nonEmpty($0.error).map { ", \($0)" } ?? ""
            return "\($0.hostID), \(state)\(error)"
        }
        accessibilityLabel = ([title] + accessibleHosts).joined(separator: ". ")
    }

    private static func hostLine(_ host: ContentAnalysisHostResult) -> String {
        let base = "\(host.hostID) · \(stateText(host.state))"
        return nonEmpty(host.error).map { "\(base) · \($0)" } ?? base
    }

    private static func stateText(_ state: ContentAnalysisHostState) -> String {
        switch state {
        case .acknowledged: return "Acknowledged"
        case .disconnected: return "Disconnected"
        case .failed: return "Failed"
        case .unknown: return "Unknown status"
        }
    }

    private static func nonEmpty(_ value: String?) -> String? {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? nil : trimmed
    }
}

/// Keeps the backend picker aligned with confirmed server state. Choosing
/// Disabled is a revocation request, not a local presentation change: the
/// confirmed backend remains visible until the destructive operation succeeds.
public struct ContentAnalysisSelectionState: Sendable, Equatable {
    public private(set) var displayedMode: ContentAnalysisMode = .disabled
    public private(set) var isRevocationPending = false
    public var disclosureAccepted = false

    private var confirmedMode: ContentAnalysisMode = .disabled

    public init() {}

    public mutating func synchronize(
        enabled: Bool,
        backend: ContentAnalysisBackend,
        disclosureAccepted: Bool
    ) {
        confirmedMode = enabled
            ? (backend == .local ? .local : .cloud)
            : .disabled
        displayedMode = confirmedMode
        isRevocationPending = false
        self.disclosureAccepted = disclosureAccepted
    }

    /// Returns true when the caller must present the destructive revocation
    /// confirmation instead of changing the visible mode.
    @discardableResult
    public mutating func select(_ mode: ContentAnalysisMode) -> Bool {
        if mode == .disabled, confirmedMode != .disabled {
            displayedMode = confirmedMode
            isRevocationPending = true
            return true
        }
        displayedMode = mode
        isRevocationPending = false
        return false
    }

    public mutating func cancelRevocation() {
        displayedMode = confirmedMode
        isRevocationPending = false
    }
}

/// Stable information order for the analytics-first Home. Healthy sections
/// with no content stay out of the way, while degraded provider state remains
/// visible even when a connector cannot return an account snapshot.
public enum HomeSection: Sendable, Equatable {
    case attention
    case providerCapacity
    case activity
    case popularProjects
    case insights
    case sessions

    public static func visible(for overview: CockpitOverview) -> [Self] {
        var sections: [Self] = [.attention]

        if overview.providerCapacity.status != .ok
            || !(overview.providerCapacity.data ?? []).isEmpty {
            sections.append(.providerCapacity)
        }
        if overview.activity.data != nil {
            sections.append(.activity)
        }
        if !overview.popularProjects.isEmpty {
            sections.append(.popularProjects)
        }
        if overview.insightCounts != nil {
            sections.append(.insights)
        }
        sections.append(.sessions)
        return sections
    }
}

/// Section-level provider state always wins over a retained account's last
/// successful status. This prevents a last-known-good `.ok` account from
/// being called Live while its enclosing response is stale or unavailable.
public struct ProviderSectionPresentation: Sendable, Equatable {
    public let isDegraded: Bool
    public let warningText: String?
    private let status: DataStatus

    public init(
        status: DataStatus,
        message: String? = nil,
        hasRetainedValues: Bool = true
    ) {
        self.status = status
        isDegraded = status != .ok

        guard status != .ok else {
            warningText = nil
            return
        }

        let base = Self.nonEmpty(message) ?? Self.defaultWarning(for: status)
        warningText = base.map {
            hasRetainedValues ? "\($0) Showing last reported values." : $0
        }
    }

    public func accountStatusText(accountStatus: ProviderAccountStatus) -> String {
        switch status {
        case .stale: return "Stale"
        case .unavailable: return "Unavailable"
        case .error: return "Error"
        case .unknown: return "Unknown"
        case .ok:
            switch accountStatus {
            case .ok: return "Live"
            case .usageUnavailable: return "Unavailable"
            case .stale: return "Stale"
            case .error: return "Error"
            case .unknown: return "Unknown"
            }
        }
    }

    private static func defaultWarning(for status: DataStatus) -> String? {
        switch status {
        case .stale: return "Provider capacity is stale."
        case .unavailable: return "Provider usage is unavailable."
        case .error, .unknown: return "Provider capacity could not be refreshed."
        case .ok: return nil
        }
    }

    private static func nonEmpty(_ message: String?) -> String? {
        let value = message?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
    }
}

/// Deterministic display values for one provider-reported quota window.
/// SwiftUI receives strings and state only; it never derives quota semantics.
public struct ProviderCapacityPresentation: Sendable, Equatable {
    public let usedText: String
    public let remainingText: String
    public let resetText: String
    public let freshnessText: String?
    public let sourceText: String?
    public let isStale: Bool

    public init(window: ProviderWindow, now: Date) {
        self.init(account: nil, window: window, now: now)
    }

    public init(account: ProviderAccount, window: ProviderWindow, now: Date) {
        self.init(account: Optional(account), window: window, now: now)
    }

    private init(account: ProviderAccount?, window: ProviderWindow, now: Date) {
        if let limit = window.limitValue,
           let remaining = window.remainingValue,
           let unit = Self.nonEmpty(window.unit) {
            usedText = "\(Self.amount(max(0, limit - remaining))) \(unit) used"
            remainingText = "\(Self.amount(max(0, remaining))) \(unit) remaining"
        } else if let percent = window.usedPercent {
            let bounded = min(100, max(0, percent))
            usedText = "\(Self.amount(bounded))% used"
            remainingText = "\(Self.amount(100 - bounded))% remaining"
        } else if let remaining = window.remainingValue,
                  let unit = Self.nonEmpty(window.unit) {
            usedText = "Usage unavailable"
            remainingText = "\(Self.amount(max(0, remaining))) \(unit) remaining"
        } else {
            usedText = "Usage unavailable"
            remainingText = "Remaining unavailable"
        }

        if let reset = window.resetsAt {
            let seconds = reset.timeIntervalSince(now)
            if seconds <= 0 {
                resetText = "Stale"
                isStale = true
            } else {
                resetText = "Resets in \(Self.duration(seconds))"
                isStale = account?.status == .stale
            }
        } else {
            resetText = "Reset unavailable"
            isStale = account?.status == .stale
        }

        if let account {
            freshnessText = Self.freshness(observedAt: account.observedAt, now: now)
            sourceText = Self.sourceLabel(account.source)
        } else {
            freshnessText = nil
            sourceText = nil
        }
    }

    private static func amount(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = true
        formatter.groupingSize = 3
        formatter.minimumFractionDigits = 0
        formatter.maximumFractionDigits = value.rounded() == value ? 0 : 1
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }

    private static func duration(_ seconds: TimeInterval) -> String {
        let minutes = max(1, Int(seconds / 60))
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h" }
        return "\(hours / 24)d"
    }

    private static func freshness(observedAt: Date, now: Date) -> String {
        let seconds = max(0, now.timeIntervalSince(observedAt))
        if seconds < 60 { return "Updated just now" }
        if seconds < 3_600 { return "Updated \(Int(seconds / 60))m ago" }
        if seconds < 86_400 { return "Updated \(Int(seconds / 3_600))h ago" }
        return "Updated \(Int(seconds / 86_400))d ago"
    }

    private static func sourceLabel(_ source: String) -> String {
        switch source {
        case "codex-app-server", "codex_app_server": return "Provider reported"
        case "harness-inventory", "harness_inventory": return "Harness detected"
        default: return source.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private static func nonEmpty(_ value: String?) -> String? {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? nil : trimmed
    }
}

public struct ProjectActivityPresentation: Sendable, Equatable {
    public let projectName: String
    public let valueText: String
    public let metricText: String
    public let coverageText: String
    public let contributorsText: String

    public init(project: PopularProject, tokenCoveragePercent: Double?) {
        projectName = project.projectKey
        switch project.metric {
        case .tokens:
            valueText = "\(Self.integer(project.totalTokens)) tokens"
            metricText = "Ranked by tokens"
        case .sessions:
            valueText = "\(project.sessionCount) \(project.sessionCount == 1 ? "session" : "sessions")"
            metricText = "Ranked by sessions"
        }
        coverageText = tokenCoveragePercent.map {
            "\(Self.percent($0))% token coverage"
        } ?? "Token coverage unavailable"

        let harnesses = project.harnesses.joined(separator: ", ")
        let hosts = project.hosts.joined(separator: ", ")
        contributorsText = [harnesses, hosts]
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
    }

    private static func integer(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = true
        formatter.groupingSize = 3
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }

    private static func percent(_ value: Double) -> String {
        let bounded = min(100, max(0, value))
        return ProviderNumberFormatting.amount(bounded)
    }
}

public struct InsightPresentation: Sendable, Equatable {
    public let severityText: String
    public let confidenceText: String
    public let sourceText: String
    public let uncertaintyText: String?

    public init(insight: InsightSummary) {
        self.init(
            analyzerClass: insight.analyzerClass,
            severity: insight.severity,
            confidence: insight.confidence
        )
    }

    public init(insight: InsightFinding) {
        self.init(
            analyzerClass: insight.analyzerClass,
            severity: insight.severity,
            confidence: insight.confidence
        )
    }

    private init(
        analyzerClass: InsightAnalyzerClass,
        severity: InsightSeverity,
        confidence: InsightConfidence
    ) {
        severityText = Self.title(severity.rawValue)
        confidenceText = Self.title(confidence.rawValue)
        switch analyzerClass {
        case .deterministic:
            sourceText = "Deterministic check"
            uncertaintyText = nil
        case .model:
            sourceText = "Model judgment"
            uncertaintyText = "Review the evidence before making changes."
        }
    }

    private static func title(_ value: String) -> String {
        value.prefix(1).uppercased() + value.dropFirst()
    }
}

private enum ProviderNumberFormatting {
    static func amount(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.minimumFractionDigits = 0
        formatter.maximumFractionDigits = value.rounded() == value ? 0 : 1
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }
}

/// One subscription, however many hosts report it.
///
/// The fleet reports provider capacity *per host*, because that is where the
/// probe runs. Presented raw, one Codex account signed in on three machines is
/// three identical cards with no way to tell them apart — the host is not even
/// on the card. So identity here is the subscription (provider + account +
/// plan), and the hosts become an attribute of it.
///
/// Merging is by label, which is a true identity for providers that report an
/// account (`arnabsahacanada@gmail.com`) and a generic name for those that do
/// not (`Claude Code`). Collapsing the generic ones assumes a single operator's
/// account signed in on each host — the deployment Drover is built for. It is
/// the same assumption the fleet already makes everywhere else.
public struct ProviderSubscriptionPresentation: Sendable, Equatable, Identifiable {
    public let id: String
    public let title: String
    public let provider: String
    public let accountLabel: String
    public let planLabel: String?
    /// Host ids that reported this subscription, sorted.
    public let hostIDs: [String]
    /// "Mac Mini, NAS" — display titles when known, ids otherwise.
    public let hostsText: String
    /// The freshest reading across hosts; the card's numbers come from it.
    public let representative: ProviderAccount
    public let windows: [ProviderWindow]
    public let status: ProviderAccountStatus
    /// Why this subscription is degraded, naming the host it failed on, so a
    /// single broken probe reads as one card's problem rather than a banner
    /// over the whole section.
    public let reasonText: String?

    public var isDegraded: Bool { status != .ok }
}

public enum ProviderSubscriptionGrouping {
    /// Groups per-host accounts into one entry per subscription.
    ///
    /// The representative is the most recently observed *healthy* member when
    /// there is one — a host whose probe just failed should not blank out
    /// numbers another host reported successfully a minute ago — falling back
    /// to the freshest member overall.
    public static func group(
        _ accounts: [ProviderAccount],
        hostTitles: [String: String] = [:]
    ) -> [ProviderSubscriptionPresentation] {
        var order: [String] = []
        var buckets: [String: [ProviderAccount]] = [:]

        for account in accounts {
            let key = identity(for: account)
            if buckets[key] == nil {
                buckets[key] = []
                order.append(key)
            }
            buckets[key]?.append(account)
        }

        return order.compactMap { key in
            guard let members = buckets[key], let newest = members.max(by: { $0.observedAt < $1.observedAt })
            else { return nil }

            let healthy = members.filter { $0.status == .ok }
            let representative = healthy.max(by: { $0.observedAt < $1.observedAt }) ?? newest
            let hostIDs = Array(Set(members.map(\.hostID))).sorted()
            let titles = hostIDs.map { hostTitles[$0] ?? $0 }

            return ProviderSubscriptionPresentation(
                id: key,
                title: "\(representative.provider.capitalized) · \(representative.accountLabel)",
                provider: representative.provider,
                accountLabel: representative.accountLabel,
                // Any host that could read the plan speaks for the whole
                // subscription; a host that could not should not blank it.
                planLabel: representative.planLabel
                    ?? members.compactMap(\.planLabel).first,
                hostIDs: hostIDs,
                hostsText: ListFormatter.localizedString(byJoining: titles),
                representative: representative,
                windows: representative.windows,
                status: representative.status,
                reasonText: reason(members: members, hostTitles: hostTitles)
            )
        }
    }

    /// Provider and account only — the plan is an attribute of the
    /// subscription, not part of its identity. Hosts disagree about it: the
    /// same Anthropic account reports `max` from one machine and nothing at
    /// all from another, depending on what that host's CLI could see. Keying
    /// on it split one subscription back into the duplicate cards this exists
    /// to remove.
    private static func identity(for account: ProviderAccount) -> String {
        [account.provider, account.accountLabel]
            .map { $0.lowercased() }
            .joined(separator: "|")
    }

    /// Names the failing hosts and what went wrong, rather than asserting a
    /// cause the probe never reported. `unavailable` means the provider CLI
    /// would not launch on that host — which is what a machine missing the
    /// tool, or one that was never signed in, actually looks like from here.
    private static func reason(
        members: [ProviderAccount],
        hostTitles: [String: String]
    ) -> String? {
        let failing = members.filter { $0.status == .error || $0.status == .stale }
        guard !failing.isEmpty else { return nil }

        let hosts = ListFormatter.localizedString(
            byJoining: failing.map { hostTitles[$0.hostID] ?? $0.hostID }.sorted()
        )
        let categories = Set(failing.compactMap { $0.errorCategory })
        let detail = categories.count == 1 ? categories.first.map(explain) ?? nil : nil
        return detail.map { "\($0) on \(hosts)" } ?? "Not reporting on \(hosts)"
    }

    private static func explain(_ category: String) -> String? {
        switch category {
        case "unavailable": return "Provider CLI unavailable"
        case "timeout": return "Timed out"
        case "process_error": return "Probe failed"
        case "empty_inventory": return "No accounts detected"
        case "freshness_expired", "provider_window_expired": return "Reading expired"
        default: return nil
        }
    }
}
