#if DEBUG
import DroverKit
import SwiftUI

/// Isolated, credential-free visual verification using the real screen views.
struct ObservabilityFixtureRoot: View {
    let client: DroverClient
    @State private var store: CockpitStore
    @State private var appearance = AppearanceStore()

    init(client: DroverClient) {
        self.client = client
        let store = CockpitStore(client: client)
        store.updateCapability(from: try! JSONDecoder().decode(HarnessSnapshot.self,
            from: FixtureScenarioData.insightCapabilitySnapshotData()))
        _store = State(initialValue: store)
    }

    var body: some View {
        TabView {
            NavigationStack { AnalyticsView(store: store) }
                .tabItem { Label("Analytics", systemImage: "chart.bar") }
            NavigationStack { InsightsView(client: client, store: store) }
                .tabItem { Label("Insights", systemImage: "lightbulb") }
            NavigationStack {
                ProviderAccountsView(accounts: ObservabilityFixtureData.accounts, status: .ok,
                    statusMessage: nil, hostTitles: ObservabilityFixtureData.hostTitles)
            }
            .tabItem { Label("Accounts", systemImage: "person.crop.circle") }
            NavigationStack {
                SessionsView(client: client, notifier: FixtureNotifier(), recoveryStore: nil,
                    recoveryWriteGate: ChatRecoveryWriteGate(), recoveryGeneration: 0)
            }
            .tabItem { Label("Home", systemImage: "rectangle.stack") }

        }
        .environment(appearance)
        .preferredColorScheme(appearance.appearance.colorScheme)
        .droverTint()
    }
}

enum ObservabilityFixtureData {
    static let hostTitles = ["studio": "Mac Studio", "mini": "Mac Mini",
        "laptop": "Work laptop with a long descriptive host name", "nas": "NAS"]

    static var homeSnapshot: Data {
        var value = try! JSONSerialization.jsonObject(with: FixtureScenarioData.snapshotData()) as! [String: Any]
        value["cockpit_api_version"] = 1
        value["cockpit_sections"] = ["provider_capacity", "activity", "insights"]
        return encode(value)
    }

    static var overview: Data {
        var value = try! JSONSerialization.jsonObject(with: analytics) as! [String: Any]
        value["insight_counts"] = ["critical": 0, "high": 1, "medium": 0, "low": 0]
        return encode(value)
    }

    private static let reported = "2026-09-30T18:00:00Z"

    static var accounts: [ProviderAccount] {
        try! JSONDecoder().decode([ProviderAccount].self, from: encode(accountValues))
    }

    private static var accountValues: [[String: Any]] {
        let shared: [[String: Any]] = [("studio", "ok", 32), ("mini", "stale", 32), ("laptop", "error", 32)].map { host, status, used in
            ["snapshot_id": host, "dedup_key": host, "provider": "anthropic",
             "account_label": "alex@example.com", "plan_label": "Max",
             // Offline readings older than 72h collapse into "Stale hosts", so
             // a fixed date would hide these chips from the journeys over time.
             "host_id": host, "status": status,
             "observed_at": status == "ok" ? reported
                 : ISO8601DateFormatter().string(from: Date().addingTimeInterval(-86400)),
             "source": "fixture-usage", "error_category": status == "ok" ? NSNull() : "host_offline",
             "windows": [["kind": "seven_day", "used_percent": used],
                         ["kind": "five_hour", "used_percent": 18]]]
        }
        let others: [[String: Any]] = (0..<10).map { index in
            let provider = index == 0 ? "openai" : index == 1 ? "google" : index == 8 ? "other" : index == 9 ? "local" : "anthropic"
            return ["snapshot_id": "other-\(index)", "dedup_key": "other-\(index)",
                "provider": provider, "account_label": index == 1 ? "Antigravity" : "account-\(index)@example.com", "plan_label": index == 1 ? NSNull() : "Personal",
                "host_id": "studio", "status": index == 2 ? "stale" : "ok", "observed_at": reported,
                "source": index == 1 ? "agy-usage" : "fixture-usage", "windows": [["kind": "seven_day", "used_percent": index == 2 ? 81 : index == 0 ? 28 : 0],
                    ["kind": "five_hour", "used_percent": 0]]]
        }
        let googleHistory: [[String: Any]] = [("mini", 2), ("nas", 5)].map { host, days in
            ["snapshot_id": "google-\(host)", "dedup_key": "google-\(host)",
             "provider": "google", "account_label": "arniesaha@gmail.com",
             "plan_label": NSNull(), "host_id": host, "status": "stale",
             "observed_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(-Double(days) * 86400)),
             "source": "agy-usage", "error_category": "host_offline",
             "windows": [["kind": "five_hour", "used_percent": 99]]]
        }
        return shared + others + googleHistory
    }

    static var analytics: Data {
        func metrics(_ sessions: Int, _ tokens: Int) -> [String: Any] {
            ["session_count": sessions, "total_tokens": tokens, "cost_usd": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "total_latency_ms": 0]
        }
        func breakdown(_ key: String, _ sessions: Int, _ tokens: Int, project: Bool = false) -> [String: Any] {
            var row = metrics(sessions, tokens)
            row[project ? "project_key" : "key"] = key
            if project { row["harnesses"] = ["codex", "claude-code"]; row["hosts"] = ["studio"] }
            return row
        }
        let coverage = ["token_percent": 93, "cost_percent": 65]
        return encode([
            "cockpit_api_version": 1, "filters": ["days": 7],
            "provider_capacity": ["status": "ok", "data": accountValues],
            "activity": ["status": "ok", "observed_at": reported, "coverage": coverage,
                "data": ["totals": metrics(128, 4_100_000),
                    "projects": [breakdown("drover", 80, 2_400_000, project: true),
                                 breakdown("relay", 30, 910_000, project: true),
                                 breakdown("mobile", 18, 790_000, project: true)],
                    "harnesses": [breakdown("codex", 70, 2_600_000), breakdown("claude-code", 58, 1_500_000)],
                    "hosts": [breakdown("studio", 100, 3_500_000), breakdown("laptop", 28, 600_000)],
                    "models": [breakdown("model-a", 128, 4_100_000)],
                    "project_metric": "tokens", "coverage": coverage]]
        ])
    }

    static var insights: Data {
        let finding = try! JSONSerialization.jsonObject(with: FixtureScenarioData.insightSummaryData())
        return encode(["findings": [finding], "next_cursor": NSNull()])
    }

    private static func encode(_ value: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }
}
#endif
