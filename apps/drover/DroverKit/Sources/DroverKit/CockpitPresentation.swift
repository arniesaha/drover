import Foundation

public struct AnalyticsContributorPresentation: Sendable, Equatable {
    public let text: String

    public init(
        values: [String], attributedSessionCount: Int, totalSessionCount: Int
    ) {
        let known = values.isEmpty ? nil : values.joined(separator: ", ")
        let unavailable = max(0, totalSessionCount - attributedSessionCount)
        guard unavailable > 0 else {
            text = known ?? "Unavailable"
            return
        }
        let missing = "unavailable for \(unavailable) session\(unavailable == 1 ? "" : "s")"
        text = known.map { "\($0) · \(missing)" } ?? missing.prefix(1).uppercased() + missing.dropFirst()
    }
}

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
            "\(CoveragePercent.text($0))% token coverage"
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
}

/// One rendering of a coverage percentage, so a heading and the card beneath it
/// can never print the same figure two different ways. Whole numbers lose the
/// decimal: "5%", not "5.0%".
enum CoveragePercent {
    static func text(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(format: "%.1f", value)
    }
}

/// The activity totals, worded once for every screen that shows them.
///
/// The cockpit card and the analytics screen render the same three figures off
/// the same payload, and before #150 they disagreed about the third: the card
/// said "API-billed" with a cost-coverage clause, analytics still said "API
/// cost" with no coverage at all. Both now read from here.
public struct ActivityTotalsPresentation: Sendable, Equatable {
    /// Deliberately not "API cost". Drover computes no prices — there is no
    /// pricing table anywhere in `src/`; `cost_usd` is whatever the harness
    /// reported over OTLP, and subscription-billed usage has no marginal
    /// per-token cost to report. In the 7-day window behind #150 `claude-opus-5`
    /// carried 62,951,650 tokens (99.7% of the volume) and reported $0.0181
    /// against them, while `gpt-5.6-sol` reported $0.3591 on 9,122. The total
    /// is a sum of the API-billed slice, and naming that slice is the point.
    public static let costLabel = "API-billed"

    /// A currency figure, or the unreported wording. Never both meanings.
    public let costText: String
    /// The whole spoken form, label included: VoiceOver must not read the
    /// unreported case as "zero dollars, API-billed".
    public let costAccessibilityText: String
    public let costIsUnreported: Bool
    /// Coverage for tokens *and* cost, because they are routinely different —
    /// 5.8% against 5% in the case that prompted #150 — and printing only the
    /// token figure left the less-covered number looking like a total. Says it
    /// twice only when the two actually differ.
    public let coverageText: String

    /// `locale` is injected only so the wording can be asserted: the currency
    /// style disambiguates against the reader's region, so the same $0.59
    /// renders "$0.59" in the US and "US$0.59" everywhere else, and the
    /// `swift test` host is one of the everywhere-elses. The app passes
    /// nothing and keeps the reader's own formatting.
    public init(
        totals: ActivityTotals, coverage: Coverage, locale: Locale = .autoupdatingCurrent
    ) {
        let unreported = Self.costIsUnreported(totals: totals, coverage: coverage)
        let value = Self.currency(totals.costUSD, locale: locale)
        costIsUnreported = unreported
        costText = unreported ? "Not reported" : value
        costAccessibilityText = unreported
            ? "\(Self.costLabel) cost not reported"
            : "\(value) \(Self.costLabel) cost"
        coverageText = Self.coverageText(coverage)
    }

    /// Zero cost across sessions that definitely ran is not a bill of nothing,
    /// it is the absence of one — the same distinction tokens got in #145.
    ///
    /// `cost_percent` counts the sessions whose `cost_usd` came through
    /// non-null, so zero there means no session in the window reported a cost
    /// at all. That is the normal state of a subscription-billed fleet, and
    /// rendered as `$0.00` it claims the work was free.
    ///
    /// A non-zero total is left exactly as it is however low its coverage: the
    /// $0.59 in #150 is a real measurement of a twentieth of the sessions, and
    /// the coverage clause beside it is what qualifies it.
    static func costIsUnreported(totals: ActivityTotals, coverage: Coverage) -> Bool {
        guard totals.costUSD == 0, totals.sessionCount > 0 else { return false }
        // A missing percentage counts as zero, as it does for tokens: the
        // server always emits this field, so its absence means the payload
        // came from something that did not, and asserting $0.00 against a
        // payload we cannot check is the worse of the two failures.
        return (coverage.costPercent ?? 0) == 0
    }

    static func coverageText(_ coverage: Coverage) -> String {
        guard let tokens = coverage.tokenPercent else { return "Token coverage unavailable" }
        let tokenText = "\(CoveragePercent.text(tokens))% token coverage"
        guard let cost = coverage.costPercent,
              CoveragePercent.text(cost) != CoveragePercent.text(tokens)
        else { return tokenText }
        return "\(tokenText) · \(CoveragePercent.text(cost))% cost coverage"
    }

    static func currency(_ value: Double, locale: Locale) -> String {
        value.formatted(.currency(code: "USD").precision(.fractionLength(2)).locale(locale))
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

/// Keeps a failed activity query visible instead of silently presenting an
/// empty analytics page. In particular, a missing cost value must never look
/// like a measured zero-dollar result.
public struct ObservedUsageSectionPresentation: Sendable, Equatable {
    public static let identifier = "analytics-observed-unavailable"
    public let warningText: String?

    public var accessibilityLabel: String? { warningText }

    public init(section: SectionEnvelope<ActivitySummary>) {
        if section.status == .ok, section.data != nil {
            warningText = nil
        } else {
            warningText = "Observed usage, including API cost, is temporarily unavailable. Pull to refresh and try again."
        }
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

    static func freshness(observedAt: Date, now: Date) -> String {
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

/// A quota window's kind, in prose. `seven_day` reads as "Seven day" — one
/// capital, because `.capitalized` turns it into a title ("Seven Day") and
/// leaves an untouched underscore behind when the caller forgets to strip it.
public enum ProviderWindowTitle {
    public static func display(_ kind: String) -> String {
        let spaced = kind.replacingOccurrences(of: "_", with: " ")
        guard let first = spaced.first else { return spaced }
        return first.uppercased() + spaced.dropFirst()
    }
}

/// The one quota window a capacity card shows, plus the fraction its bar fills.
///
/// Providers report wildly different window counts — Anthropic four, OpenAI
/// one, an unsupported or failed probe none — and rendering all of them made
/// the card strip a row of four different heights. So the card shows the window
/// that will stop you first and the rest move to the analytics screen.
///
/// Always present, even when there is no window at all, so the view has exactly
/// one rendering path and the unavailable card is the same shape as every
/// other card rather than a second layout.
public struct ProviderHeadline: Sendable, Equatable {
    /// "Seven day", or "Usage" when the subscription reported no window.
    public let windowTitle: String
    /// "26% used", "750 credits used", or "Usage unavailable".
    public let usedText: String
    /// "74% remaining · Resets in 14h". Nil when there is no window.
    public let detailText: String?
    /// Used, 0...1. Nil when no fraction can be derived, which the bar draws
    /// as an empty track rather than as zero — "unknown" and "none used" are
    /// opposite readings and must not share a rendering.
    public let fraction: Double?
    /// At or past the point where an account is about to stop being useful.
    public let isCritical: Bool

    /// Tide has one accent and no per-state palette, so severity is carried by
    /// weight rather than hue (see `DroverColor`). This is the threshold that
    /// switches that weight on.
    public static let criticalFraction = 0.85

    public init(account: ProviderAccount, window: ProviderWindow?, now: Date) {
        guard let window else {
            windowTitle = "Usage"
            usedText = "Usage unavailable"
            detailText = nil
            fraction = nil
            isCritical = false
            return
        }

        // Wording comes from the presentation that already ships, so a card's
        // bar can never disagree with the text printed beside it.
        let value = ProviderCapacityPresentation(account: account, window: window, now: now)
        windowTitle = ProviderWindowTitle.display(window.kind)
        usedText = value.usedText
        detailText = "\(value.remainingText) · \(value.resetText)"

        let used = Self.usedFraction(window)
        fraction = used
        isCritical = (used ?? 0) >= Self.criticalFraction
    }

    /// Walks the same ladder `ProviderCapacityPresentation` uses to choose its
    /// wording, in the same order, for the same reason.
    static func usedFraction(_ window: ProviderWindow) -> Double? {
        // Which rung applies is decided by the same field test the wording
        // uses — presence, not usability. A window carrying both a zero limit
        // and a percentage must not let the bar drop through to the percentage
        // while the text stays on the limit: that drew a nearly-full critical
        // bar beside the words "0 credits used".
        if let limit = window.limitValue,
           let remaining = window.remainingValue,
           let unit = window.unit,
           !unit.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            // A provider reporting a zero limit would otherwise divide into an
            // infinite bar. No reading is the honest answer here.
            guard limit > 0 else { return nil }
            return min(1, max(0, (limit - remaining) / limit))
        }
        if let percent = window.usedPercent {
            return min(1, max(0, percent / 100))
        }
        return nil
    }

    /// The window closest to exhaustion. A window nobody can put a number on
    /// says nothing about how full the account is, so it ranks behind every
    /// window that can; ties break on `kind` so the card does not reshuffle
    /// between refreshes.
    static func leadingWindow(_ windows: [ProviderWindow]) -> ProviderWindow? {
        windows.sorted { lhs, rhs in
            let left = usedFraction(lhs) ?? -1
            let right = usedFraction(rhs) ?? -1
            if left != right { return left > right }
            return lhs.kind < rhs.kind
        }.first
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
/// Merging is by label, so the label has to be a real identity. This once
/// assumed that a generic name like `Claude Code` meant one operator's account
/// signed in everywhere, and merged on that basis. It was wrong on the fleet it
/// was written for: a personal subscription on two hosts and a work
/// subscription on a third collapsed into one card that showed personal
/// consumption against a work machine. Probes now report the account they are
/// actually signed into, so this code can take the label at its word.
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
    /// The single window the card renders, chosen for tightness.
    public let headline: ProviderHeadline
    /// "Updated 1m ago". Lives here rather than on a window's presentation
    /// because a subscription with no windows must still say when it was read.
    public let freshnessText: String
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
        hostTitles: [String: String] = [:],
        now: Date = Date()
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
                headline: ProviderHeadline(
                    account: representative,
                    window: ProviderHeadline.leadingWindow(representative.windows),
                    now: now
                ),
                freshnessText: ProviderCapacityPresentation.freshness(
                    observedAt: representative.observedAt, now: now
                ),
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

    /// Names the failing hosts and what went wrong.
    ///
    /// These categories describe the *central server's* attempt to collect
    /// usage from a host, not the state of that host's CLI: `unavailable` is
    /// set when the fetch of `/providers/usage` failed, which is what a
    /// restarting daemon looks like from here. Saying "provider CLI
    /// unavailable" read as "the tool is not installed" and sent a reader to
    /// reinstall CLIs that were on PATH the whole time.
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
        return detail.map { "\($0) \(hosts)" } ?? "Not reporting on \(hosts)"
    }

    private static func explain(_ category: String) -> String? {
        switch category {
        case "unavailable", "host_offline": return "Couldn't reach"
        // Not a reachability or sign-in problem: the daemon resolved no path to
        // the CLI, so the probe never ran.
        case "cli_not_found": return "CLI not found on"
        case "timeout": return "Timed out reaching"
        case "process_error": return "Usage probe failed on"
        case "empty_inventory": return "No accounts detected on"
        case "freshness_expired", "provider_window_expired": return "Reading expired on"
        default: return nil
        }
    }
}

/// The one line the capacity strip shows while collapsed.
///
/// The strip is pinned above the inbox, so every point it occupies is a point
/// the session list does not get (#80). Collapsed it still has to answer the
/// question the strip exists for — "what have I got left to spend" — which is
/// the *tightest* budget across subscriptions, not an average and not the
/// first account: the one about to run out is the one that changes what you do
/// next.
public struct ProviderCapacitySummary: Sendable, Equatable {
    /// "2 accounts", "1 account".
    public let accountsText: String
    /// "lowest 45% left", or nil when no subscription reported a window.
    /// Absent rather than zero on purpose: a probe that reported nothing is
    /// not a budget that is spent, and the two must not read alike.
    public let tightestText: String?
    /// The tightest reading is at or past `ProviderHeadline.criticalFraction`.
    public let isCritical: Bool

    public var text: String {
        [accountsText, tightestText].compactMap { $0 }.joined(separator: " · ")
    }

    public init(subscriptions: [ProviderSubscriptionPresentation]) {
        let count = subscriptions.count
        accountsText = count == 1 ? "1 account" : "\(count) accounts"

        let fractions = subscriptions.compactMap { $0.headline.fraction }
        guard let tightest = fractions.max() else {
            tightestText = nil
            isCritical = false
            return
        }
        let remaining = max(0, min(100, Int((((1 - tightest) * 100)).rounded())))
        tightestText = "lowest \(remaining)% left"
        isCritical = tightest >= ProviderHeadline.criticalFraction
    }
}
