import Foundation

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
        if let counts = overview.insightCounts,
           counts.critical + counts.high + counts.medium + counts.low > 0 {
            sections.append(.insights)
        }
        sections.append(.sessions)
        return sections
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
        case "codex_app_server": return "Provider reported"
        case "harness_inventory": return "Harness detected"
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
