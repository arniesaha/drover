import Foundation

public struct WorkAttentionItem: Decodable, Identifiable, Sendable, Equatable {
    public let id: String
    public let workspaceID: String
    public let actorPrincipal: String
    public let goalID: String
    public let repo: String
    public let kind: String
    public let url: String
    public let refreshedAt: String
    public let stale: Bool
    public let freshnessSeconds: TimeInterval
    public let actions: [String]
    public let number: Int?
    public let sessionID: String?
    public let decisionID: String?
    public let category: String?
    public let uncertain: Bool?

    enum CodingKeys: String, CodingKey {
        case id, repo, kind, url, stale, actions, number, category, uncertain
        case workspaceID = "workspace_id", actorPrincipal = "actor_principal"
        case goalID = "goal_id", refreshedAt = "refreshed_at"
        case sessionID = "session_id", decisionID = "decision_id"
        case freshnessSeconds = "freshness_seconds"
    }

    public var title: String {
        switch kind {
        case "environment_approval": "Environment approval"
        case "draft_pr": "Draft PR awaiting review"
        case "pr_review": "PR awaiting review"
        case "agent_question": "Agent question"
        case "blocked": "Blocked work"
        case "failed_session": "Failed session"
        case "failed_check": "Failed check"
        default: "Work needs attention"
        }
    }

    public var githubURL: URL? {
        guard let value = URL(string: url), value.scheme == "https",
              value.host == "github.com", value.user == nil, value.password == nil else { return nil }
        return value
    }

    private var refreshedDate: Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: refreshedAt) ?? ISO8601DateFormatter().date(from: refreshedAt)
    }

    public func isStale(now: Date = Date()) -> Bool {
        guard let date = refreshedDate, freshnessSeconds > 0 else { return true }
        return stale || now.timeIntervalSince(date) > freshnessSeconds
    }

    public func refreshAge(now: Date = Date()) -> String {
        guard let date = refreshedDate else { return "Refresh age unknown" }
        return "Refreshed \(SnapshotFreshness.ageText(now.timeIntervalSince(date))) ago"
    }
}

public struct WorkAttentionResponse: Decodable, Sendable {
    public let items: [WorkAttentionItem]
    public let stale: Bool
    public let truncated: Bool
    public let pollIntervalSeconds: TimeInterval
    enum CodingKeys: String, CodingKey {
        case items, stale, truncated
        case pollIntervalSeconds = "poll_interval_seconds"
    }
}

public struct WorkDecisionResponse: Decodable, Sendable {
    public let resolved: Bool
    public let url: String?
}

public protocol WorkAttentionServing: Sendable {
    func workAttention() async throws -> WorkAttentionResponse
    func workDecide(item: WorkAttentionItem, action: String) async throws -> WorkDecisionResponse
}

extension DroverClient: WorkAttentionServing {}

@MainActor @Observable
public final class WorkAttentionStore {
    public private(set) var items: [WorkAttentionItem] = []
    public private(set) var isReachable = false
    public private(set) var isRefreshing = false
    public private(set) var isDeciding = false
    public private(set) var message: String?
    public private(set) var truncated = false
    public private(set) var pollIntervalSeconds: TimeInterval?
    private let service: any WorkAttentionServing

    public init(service: any WorkAttentionServing) { self.service = service }

    public func refresh() async {
        guard !isRefreshing, !isDeciding else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do { try await load() }
        catch { failedRefresh() }
    }

    private func load() async throws {
        let response = try await service.workAttention()
        items = response.items
        isReachable = !response.stale
        pollIntervalSeconds = response.pollIntervalSeconds > 0 ? response.pollIntervalSeconds : nil
        truncated = response.truncated
        message = response.stale ? "Showing last refreshed work. Refresh before deciding." : nil
    }

    private func failedRefresh() {
        isReachable = false
        message = "Can't refresh Needs you. Decisions are unavailable."
    }

    public func canDecide(_ item: WorkAttentionItem, action: String) -> Bool {
        isReachable && !isRefreshing && !isDeciding && !item.isStale() && item.uncertain != true
            && item.decisionID != nil && item.number != nil && item.actions.contains(action)
    }

    /// Refresh before sending; the serialized control service rechecks the event history.
    public func decide(_ item: WorkAttentionItem, action: String) async -> URL? {
        guard canDecide(item, action: action) else { return nil }
        isDeciding = true
        defer { isDeciding = false }
        var sent = false
        do {
            try await load()
            guard isReachable, let current = items.first(where: { $0.id == item.id }),
                  current.workspaceID == item.workspaceID, current.decisionID == item.decisionID,
                  !current.isStale(), current.uncertain != true, current.actions.contains(action) else {
                message = "This request changed or was resolved. The list is refreshed."
                return nil
            }
            sent = true
            let result = try await service.workDecide(item: current, action: action)
            // Disable the old row even if the follow-up read fails.
            items.removeAll { $0.id == current.id }
            isReachable = false
            try await load()
            if !result.resolved {
                message = "Continue on GitHub."
                if let raw = result.url, let url = URL(string: raw),
                   url.scheme == "https", url.host == "github.com",
                   url.user == nil, url.password == nil { return url }
            }
        } catch {
            isReachable = false
            message = sent ? "Decision outcome is unknown. Refresh and inspect before trying again."
                : "Can't refresh Needs you. Decisions are unavailable."
        }
        return nil
    }
}

public struct WorkAttentionRef: Sendable, Equatable {
    public let workspaceID: String
    public let id: String
    public init(workspaceID: String, id: String) {
        self.workspaceID = workspaceID
        self.id = id
    }
}

/// Foreground and background checks share source receipts with delivered push alerts.
@MainActor
public enum WorkAttentionNotifications {
    private static func key(_ workspace: String) -> String { "drover.work.seen." + workspace }

    public static func markSeen(_ ref: WorkAttentionRef, defaults: UserDefaults = .standard) {
        var seen = Set(defaults.stringArray(forKey: key(ref.workspaceID)) ?? [])
        seen.insert(ref.id)
        defaults.set(Array(seen), forKey: key(ref.workspaceID))
    }

    public static func evaluate(_ items: [WorkAttentionItem], notifier: any Notifying,
                                defaults: UserDefaults = .standard) async {
        let groups = Dictionary(grouping: items.filter { !$0.isStale() }, by: \.workspaceID)
        for (workspace, rows) in groups {
            let seen = Set(defaults.stringArray(forKey: key(workspace)) ?? [])
            defaults.set(rows.map(\.id), forKey: key(workspace))
            for row in rows where !seen.contains(row.id) {
                await notifier.notify(attention: WorkAttentionRef(workspaceID: workspace, id: row.id))
            }
        }
    }
}
