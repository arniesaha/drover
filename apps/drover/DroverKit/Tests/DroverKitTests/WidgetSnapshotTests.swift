import Foundation
import Testing
@testable import DroverKit

struct WidgetSnapshotTests {
    // MARK: - Encoding and Decoding

    @Test func snapshotEncodesAndDecodesCleanly() throws {
        let original = WidgetFleetSnapshot(
            fetchedAt: Date(timeIntervalSince1970: 1760000000),
            needsYouCount: 3,
            accounts: [
                WidgetAccountQuota(
                    id: "anthropic|work",
                    provider: "anthropic",
                    accountLabel: "Work",
                    remainingFraction: 0.15,
                    tightestWindowName: "5-hour",
                    isStale: false,
                    hasWarnings: false
                ),
                WidgetAccountQuota(
                    id: "openai|team",
                    provider: "openai",
                    accountLabel: "Team",
                    remainingFraction: 0.60,
                    tightestWindowName: "Weekly",
                    isStale: true,
                    hasWarnings: true
                )
            ],
            hosts: [
                WidgetHostStatus(
                    id: "mini",
                    name: "Mac Mini",
                    status: .active,
                    activeSessionCount: 2,
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
                    id: "box",
                    name: "Offline Box",
                    status: .offline,
                    activeSessionCount: 0,
                    hasWaiting: false
                )
            ]
        )

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(original)

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let decoded = try decoder.decode(WidgetFleetSnapshot.self, from: data)

        #expect(decoded == original)
        #expect(decoded.needsYouCount == 3)
        #expect(decoded.accounts.count == 2)
        #expect(decoded.hosts.count == 3)
        #expect(decoded.liveHostsCount == 2)
        #expect(decoded.lowestQuotaAccount?.id == "anthropic|work")
        #expect(decoded.lowestQuotaAccount?.remainingPercent == 15)
        #expect(decoded.lowestQuotaAccount?.providerInitial == "A")
        #expect(decoded.accounts[1].providerInitial == "O")
        #expect(decoded.accounts[1].providerTitle == "OpenAI")
    }

    // MARK: - Truncation "+N"

    @Test func quotaDisplayTruncationLogic() {
        let makeAccount: (Int) -> WidgetAccountQuota = { index in
            WidgetAccountQuota(
                id: "account-\(index)",
                provider: "provider-\(index)",
                accountLabel: "Acc \(index)",
                remainingFraction: Double(index) * 0.1,
                tightestWindowName: "Window",
                isStale: false,
                hasWarnings: false
            )
        }

        // Case 1: 0 accounts
        let zero = WidgetQuotaDisplay(accounts: [])
        #expect(zero.visibleAccounts.isEmpty)
        #expect(zero.truncatedCount == 0)
        #expect(!zero.hasTruncatedAccounts)

        // Case 2: 3 accounts (< 4)
        let threeAccounts = (1...3).map(makeAccount)
        let three = WidgetQuotaDisplay(accounts: threeAccounts)
        #expect(three.visibleAccounts.count == 3)
        #expect(three.truncatedCount == 0)
        #expect(!three.hasTruncatedAccounts)

        // Case 3: Exactly 4 accounts
        let fourAccounts = (1...4).map(makeAccount)
        let four = WidgetQuotaDisplay(accounts: fourAccounts)
        #expect(four.visibleAccounts.count == 4)
        #expect(four.truncatedCount == 0)
        #expect(!four.hasTruncatedAccounts)

        // Case 4: 5 accounts (+1)
        let fiveAccounts = (1...5).map(makeAccount)
        let five = WidgetQuotaDisplay(accounts: fiveAccounts)
        #expect(five.visibleAccounts.count == 4)
        #expect(five.truncatedCount == 1)
        #expect(five.hasTruncatedAccounts)
        #expect(five.visibleAccounts.map(\.id) == ["account-1", "account-2", "account-3", "account-4"])

        // Case 5: 8 accounts (+4)
        let eightAccounts = (1...8).map(makeAccount)
        let eight = WidgetQuotaDisplay(accounts: eightAccounts)
        #expect(eight.visibleAccounts.count == 4)
        #expect(eight.truncatedCount == 4)
        #expect(eight.hasTruncatedAccounts)
    }

    // MARK: - Selection and Ordering (Tightest Window First)

    @Test func buildOrdersAccountsByLowestRemainingFraction() throws {
        let now = Date(timeIntervalSince1970: 1760000000)

        // Helper to encode and decode ProviderAccount fixture
        let makeRawAccount: (String, String, String, Double, Int) throws -> ProviderAccount = { id, provider, label, usedPct, observedOffset in
            let json = """
            {
                "snapshot_id": "\(id)-snap",
                "dedup_key": "\(id)-key",
                "provider": "\(provider)",
                "account_label": "\(label)",
                "account_identity": "\(id)@example.com",
                "host_id": "host-1",
                "status": "ok",
                "observed_at": "\(WireDate.withFractionalSeconds.string(from: now.addingTimeInterval(TimeInterval(observedOffset))))",
                "source": "cli",
                "windows": [
                    {
                        "kind": "five_hour",
                        "used_percent": \(usedPct)
                    }
                ]
            }
            """
            return try JSONDecoder().decode(ProviderAccount.self, from: json.data(using: .utf8)!)
        }

        // Account A: 90% used (10% remaining) - tightest!
        let accA = try makeRawAccount("accA", "anthropic", "Account A", 90.0, -100)
        // Account B: 30% used (70% remaining)
        let accB = try makeRawAccount("accB", "openai", "Account B", 30.0, -100)
        // Account C: 60% used (40% remaining)
        let accC = try makeRawAccount("accC", "google", "Account C", 60.0, -100)

        let snapshot = WidgetFleetSnapshot.build(
            snapshot: nil,
            accounts: [accB, accA, accC],
            now: now
        )

        // Should be sorted lowest remaining first: A (10%), C (40%), B (70%)
        #expect(snapshot.accounts.count == 3)
        #expect(snapshot.accounts[0].accountLabel == "Account A")
        #expect(snapshot.accounts[0].remainingPercent == 10)
        #expect(snapshot.accounts[1].accountLabel == "Account C")
        #expect(snapshot.accounts[1].remainingPercent == 40)
        #expect(snapshot.accounts[2].accountLabel == "Account B")
        #expect(snapshot.accounts[2].remainingPercent == 70)
        #expect(snapshot.lowestQuotaAccount?.accountLabel == "Account A")
    }

    // MARK: - Stale Detection

    @Test func staleDetectionForSnapshotAndAccounts() throws {
        let now = Date(timeIntervalSince1970: 1760000000)

        // 1. Fresh snapshot
        let freshSnapshot = WidgetFleetSnapshot(
            fetchedAt: now.addingTimeInterval(-5 * 60),  // 5m ago
            needsYouCount: 0,
            accounts: [],
            hosts: []
        )
        #expect(!freshSnapshot.isStale(now: now, threshold: 15 * 60))

        // 2. Old snapshot beyond threshold
        let staleSnapshot = WidgetFleetSnapshot(
            fetchedAt: now.addingTimeInterval(-30 * 60),  // 30m ago
            needsYouCount: 0,
            accounts: [],
            hosts: []
        )
        #expect(staleSnapshot.isStale(now: now, threshold: 15 * 60))

        // 3. Degraded account status marked isStale
        let json = """
        {
            "snapshot_id": "snap-degraded",
            "dedup_key": "key-degraded",
            "provider": "anthropic",
            "account_label": "Degraded Acc",
            "account_identity": "deg@example.com",
            "host_id": "host-1",
            "status": "stale",
            "observed_at": "\(WireDate.withFractionalSeconds.string(from: now.addingTimeInterval(-60)))",
            "source": "cli",
            "windows": []
        }
        """
        let rawAccount = try JSONDecoder().decode(ProviderAccount.self, from: json.data(using: .utf8)!)
        let built = WidgetFleetSnapshot.build(snapshot: nil, accounts: [rawAccount], now: now)
        #expect(built.accounts.first?.isStale == true)
    }

    // MARK: - Host Presence and Needs-You Derivation

    @Test func hostPresenceAndNeedsYouCountsDeriveAccurately() throws {
        let host1Json = """
        {
            "host_id": "h-online-active",
            "display_name": "Online Active",
            "status": "online",
            "connection_kind": "direct"
        }
        """
        let host2Json = """
        {
            "host_id": "h-online-idle",
            "display_name": "Online Idle",
            "status": "online",
            "connection_kind": "direct"
        }
        """
        let host3Json = """
        {
            "host_id": "h-offline",
            "display_name": "Offline Box",
            "status": "offline",
            "connection_kind": "direct"
        }
        """

        let agyHarness = "agy"  // harness-name: display-only test fixture
        let snapshotJSON = """
        {
            "hosts": [\(host1Json), \(host2Json), \(host3Json)],
            "sessions": [
                {
                    "session_id": "s1",
                    "host_id": "h-online-active",
                    "harness": "\(agyHarness)",
                    "mode": "structured",
                    "status": "running",
                    "awaiting": "approval",
                    "cwd": "/tmp"
                },
                {
                    "session_id": "s2",
                    "host_id": "h-online-active",
                    "harness": "\(agyHarness)",
                    "mode": "structured",
                    "status": "running",
                    "cwd": "/tmp"
                },
                {
                    "session_id": "s3",
                    "host_id": "h-online-idle",
                    "harness": "\(agyHarness)",
                    "mode": "structured",
                    "status": "completed",
                    "cwd": "/tmp"
                }
            ],
            "cwd_suggestions": []
        }
        """

        let harnessSnapshot = try HarnessSnapshot.decode(from: Data(snapshotJSON.utf8))

        let widgetSnapshot = WidgetFleetSnapshot.build(
            snapshot: harnessSnapshot,
            accounts: [],
            now: Date()
        )

        // Needs you count: s1 is awaiting approval = 1
        #expect(widgetSnapshot.needsYouCount == 1)

        // Hosts check
        let h1Status = widgetSnapshot.hosts.first { $0.id == "h-online-active" }
        #expect(h1Status?.status == WidgetHostPresence.active)
        #expect(h1Status?.activeSessionCount == 2)
        #expect(h1Status?.hasWaiting == true)
        #expect(h1Status?.isLive == true)

        let h2Status = widgetSnapshot.hosts.first { $0.id == "h-online-idle" }
        #expect(h2Status?.status == WidgetHostPresence.idle)
        #expect(h2Status?.activeSessionCount == 0)
        #expect(h2Status?.hasWaiting == false)
        #expect(h2Status?.isLive == true)

        let h3Status = widgetSnapshot.hosts.first { $0.id == "h-offline" }
        #expect(h3Status?.status == WidgetHostPresence.offline)
        #expect(h3Status?.activeSessionCount == 0)
        #expect(h3Status?.isLive == false)

        #expect(widgetSnapshot.liveHostsCount == 2)
    }

    // MARK: - Snapshot Storage Round-Trip

    @Test func snapshotStoreWritesAndReadsFromCustomDirectory() throws {
        let tempDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let sample = WidgetFleetSnapshot.preview()
        try WidgetSnapshotStore.writeSnapshot(sample, customDirectory: tempDir)

        let loaded = WidgetSnapshotStore.readSnapshot(customDirectory: tempDir)
        #expect(loaded != nil)
        #expect(loaded?.needsYouCount == sample.needsYouCount)
        #expect(loaded?.accounts.count == sample.accounts.count)
        #expect(loaded?.hosts.count == sample.hosts.count)
        #expect(loaded?.lowestQuotaAccount?.remainingPercent == sample.lowestQuotaAccount?.remainingPercent)
    }
}
