import Foundation

// MARK: - Shared section metadata

public enum DataStatus: String, Decodable, Sendable, Equatable {
    case ok, stale, unavailable, error, unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Self(rawValue: raw) ?? .unknown
    }
}

public struct MetricSources: Decodable, Sendable, Equatable {
    public let usagePercent: Double?
    public let spansPercent: Double?
    public let status: DataStatus

    private enum CodingKeys: String, CodingKey {
        case usagePercent = "usage_percent"
        case spansPercent = "span_percent"
        case status
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        usagePercent = try? container.decodeIfPresent(Double.self, forKey: .usagePercent)
        spansPercent = try? container.decodeIfPresent(Double.self, forKey: .spansPercent)
        status = (try? container.decode(DataStatus.self, forKey: .status)) ?? .unknown
    }
}

public struct CoverageSources: Decodable, Sendable, Equatable {
    public let tokens: MetricSources
    public let cache: MetricSources
}

public struct Coverage: Decodable, Sendable, Equatable {
    public let source: String?
    public let accountCount: Int?
    public let attributableSessionPercent: Double?
    public let tokenPercent: Double?
    public let costPercent: Double?
    public let cachePercent: Double?
    public let latencyPercent: Double?
    public let sources: CoverageSources?

    private enum CodingKeys: String, CodingKey {
        case source
        case accountCount = "account_count"
        case attributableSessionPercent = "attributable_session_percent"
        case tokenPercent = "token_percent"
        case costPercent = "cost_percent"
        case cachePercent = "cache_percent"
        case latencyPercent = "latency_percent"
        case sources
    }
}

public struct SectionEnvelope<Value: Decodable & Sendable>: Decodable, Sendable {
    public let status: DataStatus
    public let observedAt: Date?
    public let coverage: Coverage?
    public let data: Value?

    private enum CodingKeys: String, CodingKey {
        case status
        case observedAt = "observed_at"
        case coverage
        case data
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try container.decode(DataStatus.self, forKey: .status)
        observedAt = try container.decodeOptionalWireDate(forKey: .observedAt)
        coverage = try? container.decode(Coverage.self, forKey: .coverage)
        data = try container.decodeIfPresent(Value.self, forKey: .data)
    }
}

extension SectionEnvelope: Equatable where Value: Equatable {}

// MARK: - Provider capacity

public enum ProviderAccountStatus: String, Decodable, Sendable, Equatable {
    case ok
    case usageUnavailable = "usage_unavailable"
    case stale, error, unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Self(rawValue: raw) ?? .unknown
    }
}

public struct ProviderWindow: Decodable, Sendable, Equatable {
    public let kind: String
    public let usedPercent: Double?
    public let limitValue: Double?
    public let remainingValue: Double?
    public let unit: String?
    public let windowMinutes: Int?
    public let startsAt: Date?
    public let resetsAt: Date?

    private enum CodingKeys: String, CodingKey {
        case kind
        case usedPercent = "used_percent"
        case limitValue = "limit_value"
        case remainingValue = "remaining_value"
        case unit
        case windowMinutes = "window_minutes"
        case startsAt = "starts_at"
        case resetsAt = "resets_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        kind = try container.decodeRequiredIdentity(forKey: .kind)
        usedPercent = try? container.decode(Double.self, forKey: .usedPercent)
        limitValue = try? container.decode(Double.self, forKey: .limitValue)
        remainingValue = try? container.decode(Double.self, forKey: .remainingValue)
        unit = try? container.decode(String.self, forKey: .unit)
        windowMinutes = try? container.decode(Int.self, forKey: .windowMinutes)
        startsAt = try container.decodeOptionalWireDate(forKey: .startsAt)
        resetsAt = try container.decodeOptionalWireDate(forKey: .resetsAt)
    }
}

public struct ProviderAccount: Decodable, Sendable, Equatable {
    public let snapshotID: String
    public let dedupKey: String
    public let provider: String
    public let accountLabel: String
    public let planLabel: String?
    public let hostID: String
    public let status: ProviderAccountStatus
    public let observedAt: Date
    public let windows: [ProviderWindow]
    public let source: String
    public let errorCategory: String?

    private enum CodingKeys: String, CodingKey {
        case snapshotID = "snapshot_id"
        case dedupKey = "dedup_key"
        case provider
        case accountLabel = "account_label"
        case planLabel = "plan_label"
        case hostID = "host_id"
        case status
        case observedAt = "observed_at"
        case windows
        case source
        case errorCategory = "error_category"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        snapshotID = try container.decodeRequiredIdentity(forKey: .snapshotID)
        dedupKey = try container.decodeRequiredIdentity(forKey: .dedupKey)
        provider = try container.decodeRequiredIdentity(forKey: .provider)
        accountLabel = try container.decodeRequiredIdentity(forKey: .accountLabel)
        planLabel = try? container.decode(String.self, forKey: .planLabel)
        hostID = try container.decodeRequiredIdentity(forKey: .hostID)
        status = try container.decode(ProviderAccountStatus.self, forKey: .status)
        observedAt = try container.decodeRequiredWireDate(forKey: .observedAt)
        windows = try container.decodeIfPresent([ProviderWindow].self, forKey: .windows) ?? []
        source = try container.decodeRequiredIdentity(forKey: .source)
        errorCategory = try? container.decode(String.self, forKey: .errorCategory)
    }
}

// MARK: - Observed activity

public struct ActivityTotals: Decodable, Sendable, Equatable {
    public let sessionCount: Int
    public let totalTokens: Int
    public let costUSD: Double
    public let cacheReadTokens: Int
    public let cacheWriteTokens: Int
    public let totalLatencyMS: Double
    public let averageLatencyMS: Double?
    public let metadata: ObservedAggregateMetadata?

    private enum CodingKeys: String, CodingKey {
        case sessionCount = "session_count"
        case totalTokens = "total_tokens"
        case costUSD = "cost_usd"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case totalLatencyMS = "total_latency_ms"
        case averageLatencyMS = "average_latency_ms"
        case metadata
    }
}

public struct ActivityBreakdown: Decodable, Sendable, Equatable {
    public let key: String
    public let sessionCount: Int
    public let totalTokens: Int
    public let costUSD: Double
    public let cacheReadTokens: Int
    public let cacheWriteTokens: Int
    public let totalLatencyMS: Double
    public let averageLatencyMS: Double?
    public let metadata: ObservedAggregateMetadata?

    private enum CodingKeys: String, CodingKey {
        case key
        case sessionCount = "session_count"
        case totalTokens = "total_tokens"
        case costUSD = "cost_usd"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case totalLatencyMS = "total_latency_ms"
        case averageLatencyMS = "average_latency_ms"
        case metadata
    }
}

public struct ProjectActivity: Decodable, Sendable, Equatable {
    public let projectKey: String
    public let sessionCount: Int
    public let totalTokens: Int
    public let costUSD: Double
    public let cacheReadTokens: Int
    public let cacheWriteTokens: Int
    public let totalLatencyMS: Double
    public let averageLatencyMS: Double?
    public let harnesses: [String]
    public let harnessAttributedSessionCount: Int
    public let hosts: [String]
    public let hostAttributedSessionCount: Int
    public let metadata: ObservedAggregateMetadata?

    private enum CodingKeys: String, CodingKey {
        case projectKey = "project_key"
        case sessionCount = "session_count"
        case totalTokens = "total_tokens"
        case costUSD = "cost_usd"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case totalLatencyMS = "total_latency_ms"
        case averageLatencyMS = "average_latency_ms"
        case harnesses, hosts, metadata
        case harnessAttributedSessionCount = "harness_attributed_session_count"
        case hostAttributedSessionCount = "host_attributed_session_count"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        projectKey = try container.decodeRequiredIdentity(forKey: .projectKey)
        sessionCount = try container.decode(Int.self, forKey: .sessionCount)
        totalTokens = try container.decode(Int.self, forKey: .totalTokens)
        costUSD = try container.decode(Double.self, forKey: .costUSD)
        cacheReadTokens = try container.decode(Int.self, forKey: .cacheReadTokens)
        cacheWriteTokens = try container.decode(Int.self, forKey: .cacheWriteTokens)
        totalLatencyMS = try container.decode(Double.self, forKey: .totalLatencyMS)
        averageLatencyMS = try container.decodeIfPresent(Double.self, forKey: .averageLatencyMS)
        harnesses = try container.decodeIfPresent([String].self, forKey: .harnesses) ?? []
        harnessAttributedSessionCount = try container.decodeIfPresent(
            Int.self, forKey: .harnessAttributedSessionCount
        ) ?? (harnesses.isEmpty ? 0 : sessionCount)
        hosts = try container.decodeIfPresent([String].self, forKey: .hosts) ?? []
        hostAttributedSessionCount = try container.decodeIfPresent(
            Int.self, forKey: .hostAttributedSessionCount
        ) ?? (hosts.isEmpty ? 0 : sessionCount)
        metadata = try container.decodeIfPresent(ObservedAggregateMetadata.self, forKey: .metadata)
    }
}

public enum AggregateFreshness: String, Decodable, Sendable, Equatable {
    case fresh, stale, unavailable
}

public struct ObservedAggregateMetadata: Decodable, Sendable, Equatable {
    public let source: String
    public let observedAt: Date?
    public let freshness: AggregateFreshness
    public let coverage: Coverage

    private enum CodingKeys: String, CodingKey {
        case source, freshness, coverage
        case observedAt = "observed_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        source = try container.decodeRequiredIdentity(forKey: .source)
        observedAt = try container.decodeOptionalWireDate(forKey: .observedAt)
        freshness = try container.decode(AggregateFreshness.self, forKey: .freshness)
        coverage = try container.decode(Coverage.self, forKey: .coverage)
    }
}

public enum AnalyticsProjectionStatus: String, Decodable, Sendable, Equatable {
    case ready
    case catchingUp = "catching_up"
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Self(rawValue: raw) ?? .unknown
    }
}

public struct AnalyticsProjectionMetadata: Decodable, Sendable, Equatable {
    public let status: AnalyticsProjectionStatus
    public let completedPartitionCount: Int
    public let totalPartitionCount: Int

    private enum CodingKeys: String, CodingKey {
        case status
        case completedPartitionCount = "completed_partition_count"
        case totalPartitionCount = "total_partition_count"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // Every field degrades rather than throws. A decode failure here does not
        // cost the banner, it costs the whole Analytics screen: this type is
        // nested inside ActivitySummary, so one malformed field blanks the
        // aggregates the user actually came for. The same reasoning already
        // makes `status` fall back to `.unknown` on a string it has never seen.
        status =
            (try? container.decode(AnalyticsProjectionStatus.self, forKey: .status)) ?? .unknown
        completedPartitionCount =
            (try? container.decodeIfPresent(Int.self, forKey: .completedPartitionCount)) ?? 0
        totalPartitionCount =
            (try? container.decodeIfPresent(Int.self, forKey: .totalPartitionCount)) ?? 0
    }
}

public struct AnalyticsPageMetadata: Decodable, Sendable, Equatable {
    public let limit: Int
    public let nextCursor: String?

    private enum CodingKeys: String, CodingKey {
        case limit
        case nextCursor = "next_cursor"
    }

    fileprivate static let empty = AnalyticsPageMetadata(limit: 25, nextCursor: nil)

    fileprivate init(limit: Int, nextCursor: String?) {
        self.limit = limit
        self.nextCursor = nextCursor
    }
}

public struct AnalyticsPagination: Decodable, Sendable, Equatable {
    public let projects: AnalyticsPageMetadata
    public let harnesses: AnalyticsPageMetadata
    public let hosts: AnalyticsPageMetadata
    public let models: AnalyticsPageMetadata

    fileprivate static let empty = AnalyticsPagination(
        projects: .empty, harnesses: .empty, hosts: .empty, models: .empty
    )
}

public enum PopularProjectMetric: String, Decodable, Sendable, Equatable {
    case tokens, sessions
}

public struct PopularProject: Decodable, Sendable, Equatable {
    public let projectKey: String
    public let sessionCount: Int
    public let totalTokens: Int
    public let costUSD: Double
    public let cacheReadTokens: Int
    public let cacheWriteTokens: Int
    public let totalLatencyMS: Double
    public let averageLatencyMS: Double?
    public let harnesses: [String]
    public let hosts: [String]
    public let metric: PopularProjectMetric

    private enum CodingKeys: String, CodingKey {
        case projectKey = "project_key"
        case sessionCount = "session_count"
        case totalTokens = "total_tokens"
        case costUSD = "cost_usd"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case totalLatencyMS = "total_latency_ms"
        case averageLatencyMS = "average_latency_ms"
        case harnesses, hosts, metric
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        projectKey = try container.decodeRequiredIdentity(forKey: .projectKey)
        sessionCount = try container.decode(Int.self, forKey: .sessionCount)
        totalTokens = try container.decode(Int.self, forKey: .totalTokens)
        costUSD = try container.decode(Double.self, forKey: .costUSD)
        cacheReadTokens = try container.decode(Int.self, forKey: .cacheReadTokens)
        cacheWriteTokens = try container.decode(Int.self, forKey: .cacheWriteTokens)
        totalLatencyMS = try container.decode(Double.self, forKey: .totalLatencyMS)
        averageLatencyMS = try container.decodeIfPresent(Double.self, forKey: .averageLatencyMS)
        harnesses = try container.decodeIfPresent([String].self, forKey: .harnesses) ?? []
        hosts = try container.decodeIfPresent([String].self, forKey: .hosts) ?? []
        metric = try container.decode(PopularProjectMetric.self, forKey: .metric)
    }
}

public struct ActivitySummary: Decodable, Sendable, Equatable {
    public let totals: ActivityTotals
    public let projects: [ProjectActivity]
    public let harnesses: [ActivityBreakdown]
    public let hosts: [ActivityBreakdown]
    public let models: [ActivityBreakdown]
    public let projectMetric: PopularProjectMetric
    public let coverage: Coverage
    public let metadata: ObservedAggregateMetadata?
    public let projection: AnalyticsProjectionMetadata?
    public let pagination: AnalyticsPagination

    private enum CodingKeys: String, CodingKey {
        case totals, projects, harnesses, hosts, models, coverage, metadata, projection, pagination
        case projectMetric = "project_metric"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        totals = try container.decode(ActivityTotals.self, forKey: .totals)
        projects = try container.decodeIfPresent([ProjectActivity].self, forKey: .projects) ?? []
        harnesses = try container.decodeIfPresent([ActivityBreakdown].self, forKey: .harnesses) ?? []
        hosts = try container.decodeIfPresent([ActivityBreakdown].self, forKey: .hosts) ?? []
        models = try container.decodeIfPresent([ActivityBreakdown].self, forKey: .models) ?? []
        projectMetric = try container.decode(PopularProjectMetric.self, forKey: .projectMetric)
        coverage = try container.decode(Coverage.self, forKey: .coverage)
        metadata = try container.decodeIfPresent(ObservedAggregateMetadata.self, forKey: .metadata)
        projection = try container.decodeIfPresent(AnalyticsProjectionMetadata.self, forKey: .projection)
        pagination = try container.decodeIfPresent(AnalyticsPagination.self, forKey: .pagination) ?? .empty
    }
}

public struct InsightCounts: Decodable, Sendable, Equatable {
    public let critical: Int
    public let high: Int
    public let medium: Int
    public let low: Int

    private enum CodingKeys: String, CodingKey {
        case critical, high, medium, low
    }
}

public struct CockpitOverview: Decodable, Sendable, Equatable {
    public let cockpitAPIVersion: Int
    public let providerCapacity: SectionEnvelope<[ProviderAccount]>
    public let activity: SectionEnvelope<ActivitySummary>
    public let popularProjects: [PopularProject]
    public let insightCounts: InsightCounts?

    private enum CodingKeys: String, CodingKey {
        case cockpitAPIVersion = "cockpit_api_version"
        case providerCapacity = "provider_capacity"
        case activity
        case popularProjects = "popular_projects"
        case insightCounts = "insight_counts"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        cockpitAPIVersion = try container.decode(Int.self, forKey: .cockpitAPIVersion)
        providerCapacity = try container.decode(SectionEnvelope<[ProviderAccount]>.self, forKey: .providerCapacity)
        activity = try container.decode(SectionEnvelope<ActivitySummary>.self, forKey: .activity)
        popularProjects = try container.decodeIfPresent([PopularProject].self, forKey: .popularProjects) ?? []
        insightCounts = try? container.decode(InsightCounts.self, forKey: .insightCounts)
    }
}

// MARK: - Analytics

public struct AnalyticsFilters: Sendable, Equatable {
    public var days: Int
    public var hostID: String?
    public var harness: String?
    public var provider: String?
    public var model: String?
    public var projectKey: String?
    public var limit: Int
    public var projectCursor: String?
    public var harnessCursor: String?
    public var hostCursor: String?
    public var modelCursor: String?

    public init(days: Int = 7, hostID: String? = nil, harness: String? = nil,
                provider: String? = nil, model: String? = nil,
                projectKey: String? = nil, limit: Int = 25,
                projectCursor: String? = nil, harnessCursor: String? = nil,
                hostCursor: String? = nil, modelCursor: String? = nil) {
        self.days = days
        self.hostID = hostID
        self.harness = harness
        self.provider = provider
        self.model = model
        self.projectKey = projectKey
        self.limit = limit
        self.projectCursor = projectCursor
        self.harnessCursor = harnessCursor
        self.hostCursor = hostCursor
        self.modelCursor = modelCursor
    }
}

public struct AppliedAnalyticsFilters: Decodable, Sendable, Equatable {
    public let days: Int
    public let hostID: String?
    public let harness: String?
    public let provider: String?
    public let model: String?
    public let projectKey: String?
    public let limit: Int?

    private enum CodingKeys: String, CodingKey {
        case days
        case hostID = "host_id"
        case harness, provider, model
        case projectKey = "project_key"
        case limit
    }
}

public struct AnalyticsSnapshot: Decodable, Sendable, Equatable {
    public let cockpitAPIVersion: Int
    public let filters: AppliedAnalyticsFilters
    public let providerCapacity: SectionEnvelope<[ProviderAccount]>
    public let activity: SectionEnvelope<ActivitySummary>

    private enum CodingKeys: String, CodingKey {
        case cockpitAPIVersion = "cockpit_api_version"
        case filters
        case providerCapacity = "provider_capacity"
        case activity
    }
}

// MARK: - Advisory insights

public enum InsightAnalyzerClass: String, Decodable, Sendable, Equatable {
    case deterministic, model
}

public enum InsightSeverity: String, Decodable, Sendable, Equatable {
    case critical, high, medium, low
}

public enum InsightConfidence: String, Decodable, Sendable, Equatable {
    case confirmed, likely, speculative
}

public enum InsightState: String, Decodable, Sendable, Equatable {
    case open, acknowledged, dismissed, resolved, regressed
}

public struct InsightFilters: Sendable, Equatable {
    public var state: InsightState?
    public var severity: InsightSeverity?
    public var confidence: InsightConfidence?
    public var analyzerClass: InsightAnalyzerClass?
    public var host: String?
    public var harness: String?
    public var targetType: String?
    public var targetID: String?
    public var cursor: String?
    public var limit: Int

    public init(state: InsightState? = nil, severity: InsightSeverity? = nil,
                confidence: InsightConfidence? = nil,
                analyzerClass: InsightAnalyzerClass? = nil,
                host: String? = nil, harness: String? = nil,
                targetType: String? = nil, targetID: String? = nil,
                cursor: String? = nil, limit: Int = 50) {
        self.state = state
        self.severity = severity
        self.confidence = confidence
        self.analyzerClass = analyzerClass
        self.host = host
        self.harness = harness
        self.targetType = targetType
        self.targetID = targetID
        self.cursor = cursor
        self.limit = limit
    }
}

public struct InsightSummary: Decodable, Sendable, Equatable {
    public let findingID: String
    public let analyzerID: String
    public let ruleID: String
    public let targetType: String
    public let targetID: String
    public let analyzerClass: InsightAnalyzerClass
    public let severity: InsightSeverity
    public let confidence: InsightConfidence
    public let title: String
    public let state: InsightState
    public let firstSeenAt: Date
    public let lastSeenAt: Date

    enum CodingKeys: String, CodingKey {
        case findingID = "finding_id"
        case analyzerID = "analyzer_id"
        case ruleID = "rule_id"
        case targetType = "target_type"
        case targetID = "target_id"
        case analyzerClass = "analyzer_class"
        case severity, confidence, title, state
        case firstSeenAt = "first_seen_at"
        case lastSeenAt = "last_seen_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        findingID = try container.decodeRequiredIdentity(forKey: .findingID)
        analyzerID = try container.decodeRequiredIdentity(forKey: .analyzerID)
        ruleID = try container.decodeRequiredIdentity(forKey: .ruleID)
        targetType = try container.decodeRequiredIdentity(forKey: .targetType)
        targetID = try container.decodeRequiredIdentity(forKey: .targetID)
        analyzerClass = try container.decode(InsightAnalyzerClass.self, forKey: .analyzerClass)
        severity = try container.decode(InsightSeverity.self, forKey: .severity)
        confidence = try container.decode(InsightConfidence.self, forKey: .confidence)
        title = try container.decodeRequiredIdentity(forKey: .title)
        state = try container.decode(InsightState.self, forKey: .state)
        firstSeenAt = try container.decodeRequiredWireDate(forKey: .firstSeenAt)
        lastSeenAt = try container.decodeRequiredWireDate(forKey: .lastSeenAt)
    }
}

public struct InsightFinding: Decodable, Sendable, Equatable {
    public let findingID: String
    public let analyzerID: String
    public let ruleID: String
    public let targetType: String
    public let targetID: String
    public let analyzerClass: InsightAnalyzerClass
    public let severity: InsightSeverity
    public let confidence: InsightConfidence
    public let title: String
    public let impact: String
    public let remediation: [String]
    public let state: InsightState
    public let dismissalReason: String?
    public let firstSeenAt: Date
    public let lastSeenAt: Date
    public let resolvedAt: Date?
    public let dismissedAt: Date?
    public let regressedAt: Date?

    private enum CodingKeys: String, CodingKey {
        case findingID = "finding_id"
        case analyzerID = "analyzer_id"
        case ruleID = "rule_id"
        case targetType = "target_type"
        case targetID = "target_id"
        case analyzerClass = "analyzer_class"
        case severity, confidence, title, impact, remediation, state
        case dismissalReason = "dismissal_reason"
        case firstSeenAt = "first_seen_at"
        case lastSeenAt = "last_seen_at"
        case resolvedAt = "resolved_at"
        case dismissedAt = "dismissed_at"
        case regressedAt = "regressed_at"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        findingID = try container.decodeRequiredIdentity(forKey: .findingID)
        analyzerID = try container.decodeRequiredIdentity(forKey: .analyzerID)
        ruleID = try container.decodeRequiredIdentity(forKey: .ruleID)
        targetType = try container.decodeRequiredIdentity(forKey: .targetType)
        targetID = try container.decodeRequiredIdentity(forKey: .targetID)
        analyzerClass = try container.decode(InsightAnalyzerClass.self, forKey: .analyzerClass)
        severity = try container.decode(InsightSeverity.self, forKey: .severity)
        confidence = try container.decode(InsightConfidence.self, forKey: .confidence)
        title = try container.decodeRequiredIdentity(forKey: .title)
        impact = try container.decode(String.self, forKey: .impact)
        remediation = try container.decodeIfPresent([String].self, forKey: .remediation) ?? []
        state = try container.decode(InsightState.self, forKey: .state)
        dismissalReason = try? container.decode(String.self, forKey: .dismissalReason)
        firstSeenAt = try container.decodeRequiredWireDate(forKey: .firstSeenAt)
        lastSeenAt = try container.decodeRequiredWireDate(forKey: .lastSeenAt)
        resolvedAt = try container.decodeOptionalWireDate(forKey: .resolvedAt)
        dismissedAt = try container.decodeOptionalWireDate(forKey: .dismissedAt)
        regressedAt = try container.decodeOptionalWireDate(forKey: .regressedAt)
    }
}

public struct InsightEvidence: Decodable, Sendable, Equatable {
    public let observedAt: Date
    public let sourceReference: String
    public let fields: [String: JSONValue]
    public let excerpt: String?

    private enum CodingKeys: String, CodingKey {
        case observedAt = "observed_at"
        case sourceReference = "source_ref"
        case fields, excerpt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        observedAt = try container.decodeRequiredWireDate(forKey: .observedAt)
        sourceReference = try container.decodeRequiredIdentity(forKey: .sourceReference)
        fields = try container.decodeIfPresent([String: JSONValue].self, forKey: .fields) ?? [:]
        excerpt = try? container.decode(String.self, forKey: .excerpt)
    }
}

public struct InsightActionAvailability: Decodable, Sendable, Equatable {
    public let available: Bool
    public let reason: String?

    public init(available: Bool, reason: String? = nil) {
        self.available = available
        self.reason = reason
    }
}

public struct InsightActions: Decodable, Sendable, Equatable {
    public let checkAgain: InsightActionAvailability

    private enum CodingKeys: String, CodingKey {
        case checkAgain = "check_again"
    }

    public init(checkAgain: InsightActionAvailability) {
        self.checkAgain = checkAgain
    }

    static let unavailable = InsightActions(
        checkAgain: InsightActionAvailability(available: false)
    )
}

public struct InsightDetail: Decodable, Sendable, Equatable {
    public let finding: InsightFinding
    public let evidence: [InsightEvidence]
    public let actions: InsightActions

    private enum CodingKeys: String, CodingKey { case finding, evidence, actions }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        finding = try container.decode(InsightFinding.self, forKey: .finding)
        evidence = try container.decodeIfPresent([InsightEvidence].self, forKey: .evidence) ?? []
        actions = try container.decodeIfPresent(InsightActions.self, forKey: .actions)
            ?? .unavailable
    }
}

public struct InsightPage: Decodable, Sendable, Equatable {
    public let findings: [InsightSummary]
    public let nextCursor: String?

    private enum CodingKeys: String, CodingKey {
        case findings
        case nextCursor = "next_cursor"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        findings = try container.decodeIfPresent([InsightSummary].self, forKey: .findings) ?? []
        nextCursor = try? container.decode(String.self, forKey: .nextCursor)
    }
}

public struct InsightMutationResponse: Decodable, Sendable, Equatable {
    public let finding: InsightFinding

    private enum CodingKeys: String, CodingKey { case finding }
}

public struct InsightCheckResponse: Decodable, Sendable, Equatable {
    public let status: String
    public let jobID: String

    private enum CodingKeys: String, CodingKey {
        case status
        case jobID = "job_id"
    }
}

// MARK: - Content-analysis privacy

public enum ContentAnalysisBackend: String, Codable, Sendable, Equatable {
    case local, cloud
}

public enum ContentAnalysisPropagation: Sendable, Equatable, Decodable {
    case complete, partial, failed, unknown

    public init(from decoder: Decoder) throws {
        switch try decoder.singleValueContainer().decode(String.self) {
        case "complete": self = .complete
        case "partial": self = .partial
        case "failed": self = .failed
        default: self = .unknown
        }
    }
}

public enum ContentAnalysisHostState: Sendable, Equatable, Decodable {
    case acknowledged, disconnected, failed, unknown

    public init(from decoder: Decoder) throws {
        switch try decoder.singleValueContainer().decode(String.self) {
        case "acknowledged": self = .acknowledged
        case "disconnected": self = .disconnected
        case "failed": self = .failed
        default: self = .unknown
        }
    }
}

public struct ContentAnalysisHostResult: Decodable, Sendable, Equatable {
    public let hostID: String
    public let state: ContentAnalysisHostState
    public let error: String?

    private enum CodingKeys: String, CodingKey {
        case hostID = "host_id"
        case state, status, error
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let decodedID = try container.decodeIfPresent(String.self, forKey: .hostID)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if let decodedID, !decodedID.isEmpty {
            hostID = decodedID
        } else {
            hostID = "Unknown host"
        }
        let wireState = try container.decodeIfPresent(String.self, forKey: .state)
            ?? container.decodeIfPresent(String.self, forKey: .status)
        switch wireState {
        case "acknowledged": state = .acknowledged
        case "disconnected": state = .disconnected
        case "failed": state = .failed
        default: state = .unknown
        }
        error = try container.decodeIfPresent(String.self, forKey: .error)
    }
}

public struct ContentAnalysisStatus: Decodable, Sendable, Equatable {
    public let enabled: Bool
    public let backend: ContentAnalysisBackend
    public let externalDisclosureAccepted: Bool
    public let pendingModelJobs: Int
    public let cancelledModelJobs: Int?
    public let consentEpoch: Int?
    public let propagation: ContentAnalysisPropagation?
    public let hosts: [ContentAnalysisHostResult]

    public var affectedHosts: [ContentAnalysisHostResult] {
        hosts.filter { $0.state != .acknowledged }
    }

    public var propagationOutcome: ContentAnalysisMutationOutcome? {
        switch propagation {
        case .complete?: return .complete
        case .partial?: return .partial
        case .failed?, .unknown?: return .failed
        case nil: return nil
        }
    }

    private enum CodingKeys: String, CodingKey {
        case enabled, backend
        case externalDisclosureAccepted = "external_disclosure_accepted"
        case pendingModelJobs = "pending_model_jobs"
        case cancelledModelJobs = "cancelled_model_jobs"
        case consentEpoch = "consent_epoch"
        case propagation, hosts
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try container.decode(Bool.self, forKey: .enabled)
        backend = try container.decode(ContentAnalysisBackend.self, forKey: .backend)
        externalDisclosureAccepted = try container.decode(
            Bool.self, forKey: .externalDisclosureAccepted
        )
        pendingModelJobs = try container.decode(Int.self, forKey: .pendingModelJobs)
        cancelledModelJobs = try container.decodeIfPresent(Int.self, forKey: .cancelledModelJobs)
        consentEpoch = try container.decodeIfPresent(Int.self, forKey: .consentEpoch)
        propagation = try container.decodeIfPresent(
            ContentAnalysisPropagation.self, forKey: .propagation
        )
        hosts = try container.decodeIfPresent(
            [ContentAnalysisHostResult].self, forKey: .hosts
        ) ?? []
    }
}

public enum ContentAnalysisMutationOutcome: Sendable, Equatable {
    case complete, partial, failed
}

public struct ContentAnalysisConsentResult: Sendable, Equatable {
    public let status: ContentAnalysisStatus
    public let outcome: ContentAnalysisMutationOutcome

    public init(
        status: ContentAnalysisStatus,
        outcome: ContentAnalysisMutationOutcome
    ) {
        self.status = status
        self.outcome = outcome
    }
}

public struct PurgeContentExcerptsResponse: Decodable, Sendable, Equatable {
    public let purgedExcerptCount: Int

    private enum CodingKeys: String, CodingKey {
        case purgedExcerptCount = "purged_excerpt_count"
    }
}

// MARK: - Wire date decoding

private extension KeyedDecodingContainer {
    func decodeRequiredWireDate(forKey key: Key) throws -> Date {
        let raw = try decode(String.self, forKey: key)
        guard let date = WireDate.parse(raw) else {
            throw DecodingError.dataCorruptedError(
                forKey: key, in: self, debugDescription: "invalid wire timestamp"
            )
        }
        return date
    }

    func decodeOptionalWireDate(forKey key: Key) throws -> Date? {
        guard let raw = try? decode(String.self, forKey: key) else { return nil }
        return WireDate.parse(raw)
    }

    func decodeRequiredIdentity(forKey key: Key) throws -> String {
        let value = try decode(String.self, forKey: key)
        guard !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw DecodingError.dataCorruptedError(
                forKey: key, in: self, debugDescription: "required identity is empty"
            )
        }
        return value
    }
}
