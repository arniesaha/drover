#if DEBUG
import DroverKit
import SwiftUI

/// Isolated, credential-free visual verification using the real screen views.
struct ObservabilityFixtureRoot: View {
    let client: DroverClient
    @State private var store: CockpitStore

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
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(ProviderSubscriptionGrouping.group(ObservabilityFixtureData.accounts,
                            hostTitles: ["studio": "Mac Studio", "mini": "Mac Mini", "laptop": "Work laptop with a long descriptive host name"])) { subscription in
                            ProviderAccountCard(subscription: subscription,
                                                section: ProviderSectionPresentation(status: .ok))
                        }
                    }
                    .padding(14)
                }
                .background(DroverColor.bg)
                .navigationTitle("Accounts")
                .navigationBarTitleDisplayMode(.inline)
            }
            .tabItem { Label("Accounts", systemImage: "person.crop.circle") }
        }
        .droverTint()
    }
}

enum ObservabilityFixtureData {
    private static let reported = "2026-09-30T18:00:00Z"

    static var accounts: [ProviderAccount] {
        try! JSONDecoder().decode([ProviderAccount].self, from: encode(accountValues))
    }

    private static var accountValues: [[String: Any]] {
        [("studio", "ok", 32), ("mini", "stale", 32), ("laptop", "error", 32)].map { host, status, used in
            ["snapshot_id": host, "dedup_key": host, "provider": "anthropic",
             "account_label": "alex@example.com", "plan_label": "Max",
             "host_id": host, "status": status, "observed_at": reported,
             "source": "fixture-usage", "error_category": status == "ok" ? NSNull() : "host_offline",
             "windows": [["kind": "seven_day", "used_percent": used],
                         ["kind": "five_hour", "used_percent": 18]]]
        }
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
