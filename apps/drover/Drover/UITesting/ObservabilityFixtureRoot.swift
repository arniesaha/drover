#if DEBUG
import DroverKit
import SwiftUI

/// Isolated, credential-free visual verification using the real screen views.
struct ObservabilityFixtureRoot: View {
    let client: DroverClient
    @State private var store: CockpitStore
    @State private var sessionStore: SessionStore
    @State private var workStore: WorkAttentionStore
    @State private var attentionStore: InboxAttentionStore
    @State private var appearance = AppearanceStore()

    init(client: DroverClient) {
        self.client = client
        let store = CockpitStore(client: client)
        store.updateCapability(from: try! JSONDecoder().decode(HarnessSnapshot.self,
            from: FixtureScenarioData.insightCapabilitySnapshotData()))
        _store = State(initialValue: store)
        _sessionStore = State(initialValue: SessionStore(client: client))
        _workStore = State(initialValue: WorkAttentionStore(service: client))
        _attentionStore = State(initialValue: InboxAttentionStore(client: client))
    }

    var body: some View {
        TabView {
            NavigationStack {
                SessionsView(client: client, notifier: FixtureNotifier(), recoveryStore: nil,
                    recoveryWriteGate: ChatRecoveryWriteGate(), recoveryGeneration: 0,
                    sessionStore: sessionStore, cockpitStore: store)
            }
            .tabItem { Label(AppTab.home.title, systemImage: AppTab.home.systemImage) }
            .tag(AppTab.home)
            .accessibilityIdentifier(AppTab.home.accessibilityIdentifier)
            NavigationStack {
                InboxView(
                    client: client, sessionStore: sessionStore, cockpitStore: store,
                    workStore: workStore, attentionStore: attentionStore,
                    onOpenSession: { NotificationRoute.shared.open(sessionID: $0) }
                )
            }
            .tabItem { Label(AppTab.inbox.title, systemImage: AppTab.inbox.systemImage) }
            .badge(attentionStore.insights.isEmpty ? nil : String(attentionStore.insights.count))
            .tag(AppTab.inbox)
            .accessibilityIdentifier(AppTab.inbox.accessibilityIdentifier)
            NavigationStack { AnalyticsView(store: store) }
                .tabItem { Label(AppTab.analytics.title, systemImage: AppTab.analytics.systemImage) }
                .tag(AppTab.analytics)
                .accessibilityIdentifier(AppTab.analytics.accessibilityIdentifier)
            NavigationStack {
                ProviderAccountsView(accounts: ObservabilityFixtureData.accounts, status: .ok,
                    statusMessage: nil, hostTitles: ObservabilityFixtureData.hostTitles)
            }
            .tabItem { Label(AppTab.accounts.title, systemImage: AppTab.accounts.systemImage) }
            .tag(AppTab.accounts)
            .accessibilityIdentifier(AppTab.accounts.accessibilityIdentifier)
        }
        .tabViewStyle(.sidebarAdaptable)
        .environment(appearance)
        .preferredColorScheme(appearance.appearance.colorScheme)
        .droverTint()
    }
}

enum ObservabilityFixtureData {
    static let hostTitles = ["studio": "Mac Studio", "mini": "Mac Mini",
        "laptop": "Work laptop with a long descriptive host name", "nas": "NAS"]

    static var homeSnapshot: Data {
        if ProcessInfo.processInfo.environment["DROVER_UI_TEST_FLEET_FILTERS"] == "1" {
            return fleetSnapshot
        }
        var value = try! JSONSerialization.jsonObject(with: FixtureScenarioData.snapshotData()) as! [String: Any]
        value["cockpit_api_version"] = 1
        value["cockpit_sections"] = ["provider_capacity", "activity", "insights"]
        return encode(value)
    }

    static var fleetSnapshot: Data {
        let codexRow: [String: Any] = [
            "name": "codex",
            "enabled": true,
            "default_model": "gemini-2.5-pro",
            "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        ]
        let hosts: [[String: Any]] = [
            [
                "host_id": "studio",
                "status": "online",
                "connection_kind": "direct",
                "capabilities": [
                    "display_name": "Arnabs-Mac-Studio",
                    "harnesses": [codexRow],
                ],
            ],
            [
                "host_id": "gpu-pc",
                "status": "online",
                "connection_kind": "direct",
                "capabilities": [
                    "display_name": "GPU PC",
                    "harnesses": [codexRow],
                ],
            ],
            [
                "host_id": "nas",
                "status": "offline",
                "connection_kind": "direct",
                "capabilities": [
                    "display_name": "NAS",
                    "harnesses": [codexRow],
                ],
            ],
        ]

        let sessions: [[String: Any]] = [
            [
                "session_id": "studio-drover-approval",
                "host_id": "studio",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "awaiting": "approval",
                "repo_name": "arniesaha/drover",
                "cwd": "/Users/arnab/code/drover",
                "preview": "Fix reorder glitch and fleet filters",
                "last_activity": "2026-10-17T21:00:00Z",
            ],
            [
                "session_id": "studio-openclaw-running",
                "host_id": "studio",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "repo_name": "arniesaha/openclaw",
                "cwd": "/Users/arnab/code/openclaw",
                "preview": "Autonomous claw controller agent",
                "last_activity": "2026-10-17T20:50:00Z",
            ],
            [
                "session_id": "gpu-drover-running",
                "host_id": "gpu-pc",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "repo_name": "arniesaha/drover",
                "cwd": "/workspace/drover",
                "preview": "Run integration tests across matrix",
                "last_activity": "2026-10-17T20:45:00Z",
            ],
            [
                "session_id": "gpu-models-input",
                "host_id": "gpu-pc",
                "harness": "codex",
                "mode": "structured",
                "status": "working",
                "awaiting": "input",
                "repo_name": "arniesaha/models",
                "cwd": "/workspace/models",
                "preview": "Evaluate llama-3-checkpoint",
                "last_activity": "2026-10-17T20:30:00Z",
            ],
            [
                "session_id": "studio-drover-finished",
                "host_id": "studio",
                "harness": "codex",
                "mode": "structured",
                "status": "completed",
                "repo_name": "arniesaha/drover",
                "cwd": "/Users/arnab/code/drover",
                "preview": "Clean up unused dependencies",
                "last_activity": "2026-10-17T19:00:00Z",
            ],
        ]

        let value: [String: Any] = [
            "cockpit_api_version": 1,
            "cockpit_sections": ["provider_capacity", "activity", "insights"],
            "hosts": hosts,
            "sessions": sessions,
            "cwd_suggestions": [
                ["path": "/Users/arnab/code/drover", "source": "fixture", "host_id": "studio"],
                ["path": "/workspace/drover", "source": "fixture", "host_id": "gpu-pc"],
            ],
        ]
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
             "account_label": "sample.user@example.com", "plan_label": "Max",
             // Non-ok readings older than 72h become stale hosts (and leave
             // Home), so a fixed date would change these journeys over time.
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
                // Any non-ok reading collapses after 72h, so the stale one is
                // relative to launch like the offline hosts above.
                "host_id": "studio", "status": index == 2 ? "stale" : "ok",
                "observed_at": index == 2 ? ISO8601DateFormatter().string(from: Date().addingTimeInterval(-86400)) : reported,
                "source": index == 1 ? "agy-usage" : "fixture-usage", "windows": [["kind": "seven_day", "used_percent": index == 2 ? 81 : index == 0 ? 28 : 0],
                    ["kind": "five_hour", "used_percent": 0]]]
        }
        let googleHistory: [[String: Any]] = [("mini", 2), ("nas", 5)].map { host, days in
            ["snapshot_id": "google-\(host)", "dedup_key": "google-\(host)",
             "provider": "google", "account_label": "sample.google@example.com",
             "plan_label": NSNull(), "host_id": host, "status": "stale",
             "observed_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(-Double(days) * 86400)),
             "source": "agy-usage", "error_category": "host_offline",
             "windows": [["kind": "five_hour", "used_percent": 99]]]
        }
        // Reference hub: work-laptop's probe failed before it went dark, so its
        // 6-day-old reading is `unavailable`, not `host_offline`. It must stay
        // off Home, where 3% left would otherwise be the lowest meter.
        let darkHost: [String: Any] = ["snapshot_id": "work-laptop", "dedup_key": "work-laptop",
            "provider": "anthropic", "account_label": "work@example.com", "plan_label": "Team",
            "host_id": "work-laptop", "status": "stale",
            "observed_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(-6 * 86400)),
            "source": "fixture-usage", "error_category": "unavailable",
            "windows": [["kind": "seven_day", "used_percent": 97]]]
        return shared + others + googleHistory + [darkHost]
    }

    static var analytics: Data { analytics(metric: nil) }

    static func analytics(metric: String? = nil) -> Data {
        let activeMetric = metric ?? "tokens"
        func metrics(_ sessions: Int, _ tokens: Int) -> [String: Any] {
            ["session_count": sessions, "total_tokens": tokens, "cost_usd": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "total_latency_ms": 0]
        }
        func breakdown(_ key: String, _ sessions: Int, _ tokens: Int, project: Bool = false) -> [String: Any] {
            var row = metrics(sessions, tokens)
            row[project ? "project_key" : "key"] = key
            if project {
                row["harnesses"] = ["codex", "claude-code"]
                row["hosts"] = ["studio"]
                row["harness_attributed_session_count"] = sessions
                row["host_attributed_session_count"] = sessions
                row["token_attributed_session_count"] = sessions
            }
            return row
        }
        let coverage = ["token_percent": 93, "cost_percent": 65]
        return encode([
            "cockpit_api_version": 1, "filters": ["days": 7, "metric": activeMetric],
            "provider_capacity": ["status": "ok", "data": accountValues],
            "activity": ["status": "ok", "observed_at": reported, "coverage": coverage,
                "data": ["totals": metrics(128, 4_100_000),
                    "projects": [breakdown("drover", 80, 2_400_000, project: true),
                                 breakdown("relay", 30, 910_000, project: true),
                                 breakdown("mobile", 18, 790_000, project: true)],
                    "harnesses": [breakdown("codex", 70, 2_600_000), breakdown("claude-code", 58, 1_500_000)],
                    "hosts": [breakdown("studio", 100, 3_500_000), breakdown("laptop", 28, 600_000)],
                    "models": [breakdown("model-a", 128, 4_100_000)],
                    "project_metric": activeMetric, "coverage": coverage]]
        ])
    }

    static var insights: Data {
        let finding = try! JSONSerialization.jsonObject(with: FixtureScenarioData.insightSummaryData())
        return encode(["findings": [finding], "next_cursor": NSNull()])
    }

    static var history: Data {
        encode([
            "items": [
                [
                    "id": "fixture-history-1", "title": "Review Inbox navigation",
                    "harness": "codex", "model": "gpt-5", "state": "finished",
                    "host": ["id": "studio", "name": "Mac Studio", "retired": false],
                    "repo": "arniesaha/drover", "last_activity": "2026-10-18T20:35:00Z",
                    "summary": "Moved decisions and findings into Inbox.", "has_transcript": true,
                ],
                [
                    "id": "fixture-history-2", "title": "Earlier fleet check",
                    "harness": "claude-code", "state": "finished",
                    "host": ["id": "mini", "name": "Mac Mini", "retired": false],
                    "repo": "arniesaha/drover", "last_activity": "2026-10-17T19:12:00Z",
                    "summary": "Checked host capacity.", "has_transcript": false,
                ],
            ],
            "next_cursor": NSNull(), "has_more": false, "page_size": 30, "truncated": false,
        ])
    }

    static var historyFacets: Data {
        encode(["hosts": [["id": "studio", "name": "Mac Studio", "retired": false]],
                "harnesses": ["codex", "claude-code"], "repos": ["arniesaha/drover"]])
    }

    private static func encode(_ value: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }
}
#endif
