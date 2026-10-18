import Foundation
import Testing
@testable import DroverKit

@Suite("DemoHub Tests", .serialized)
struct DemoHubTests {

    @Test("Demo hub is strictly offline and blocks non-synthetic hosts")
    func testDemoHubOfflineAssertion() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let session = hub.urlSession

        // Attempting to hit an external URL must fail immediately with URLError.notConnectedToInternet
        let externalURL = URL(string: "https://api.external-host.com/secret")!
        do {
            _ = try await session.data(from: externalURL)
            Issue.record("Expected request to external host to be blocked")
        } catch let urlError as URLError {
            #expect(urlError.code == .notConnectedToInternet)
        } catch {
            Issue.record("Expected URLError.notConnectedToInternet, got \(error)")
        }

        // Reaching the synthetic demo hub succeeds
        let demoURL = URL(string: "\(DemoHubData.syntheticURLString)/readyz")!
        let (data, response) = try await session.data(from: demoURL)
        let httpResponse = try #require(response as? HTTPURLResponse)
        #expect(httpResponse.statusCode == 200)

        let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        #expect(payload?["status"] as? String == "ok")
    }

    @Test("Demo fleet snapshot includes 3 hosts, 4 projects, and 10 sessions across states")
    func testDemoFleetSnapshot() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        let snapshot = try await client.snapshot()

        // 3 hosts: studio, workstation, build-box
        #expect(snapshot.hosts.count == 3)
        let hostIDs = Set(snapshot.hosts.map(\.id))
        #expect(hostIDs == ["studio", "workstation", "build-box"])
        for host in snapshot.hosts {
            #expect(host.status == "online")
            #expect(host.connectionKind == "direct")
            #expect(host.harnesses.contains("claude-code"))
            #expect(host.harnesses.contains("codex"))
        }

        // Exactly 10 sessions across multiple states
        #expect(snapshot.sessions.count == 10)

        let attentionStates = Set(snapshot.sessions.map(\.attention))
        #expect(attentionStates.contains(.working))
        #expect(attentionStates.contains(.needsApproval))
        #expect(attentionStates.contains(.needsInput))
        #expect(attentionStates.contains(.done))
        #expect(attentionStates.contains(.errored))

        // 4 projects covered
        let repos = Set(snapshot.sessions.compactMap(\.repoName))
        #expect(repos.contains("acme/web-app"))
        #expect(repos.contains("acme/api"))
        #expect(repos.contains("demo/cli-tool"))
        #expect(repos.contains("acme/mobile"))

        // CWD Suggestions
        #expect(snapshot.cwdSuggestions.count == 4)
        #expect(snapshot.cockpitAPIVersion == 1)
        #expect(snapshot.cockpitSections.contains("provider_capacity"))
        #expect(snapshot.cockpitSections.contains("activity"))
    }

    @Test("Streaming demo session yields incremental assistant chunks via WebSocket")
    func testStreamingSessionIncrementalChunks() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let connector = DemoHubWebSocketConnector(chunkInterval: .milliseconds(5))
        let request = URLRequest(url: URL(string: "wss://\(DemoHubData.syntheticHost)/harness/sessions/\(DemoHubData.sessionStreaming)/stream")!)

        let stream = connector.connect(request)
        var receivedFrames: [String] = []

        for try await frame in stream {
            receivedFrames.append(frame)
            if receivedFrames.count >= 3 {
                break
            }
        }

        #expect(receivedFrames.count >= 3)
        let firstFrame = receivedFrames[0]
        #expect(firstFrame.contains("assistant_output"))
        #expect(firstFrame.contains("stream-chunk-"))
    }

    @Test("Approval workflow allows and denies requests cleanly")
    func testApprovalWorkflow() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        // 1. Initial state: awaiting approval
        var batch = try await client.messages(sessionID: DemoHubData.sessionApproval, afterSeq: 0)
        let prompt = batch.messages.first { $0.type == .approvalPrompt }
        #expect(prompt != nil)
        #expect(prompt?.payload["request_id"]?.stringValue == DemoHubData.approvalRequestID)

        // 2. Allow
        try await client.answerPermission(
            sessionID: DemoHubData.sessionApproval,
            requestID: DemoHubData.approvalRequestID,
            decision: "allow",
            note: nil
        )

        batch = try await client.messages(sessionID: DemoHubData.sessionApproval, afterSeq: 0)
        let resp = batch.messages.first { $0.type == .approvalResponse }
        #expect(resp != nil)
        #expect(resp?.payload["decision"]?.stringValue == "allow")

        let toolResult = batch.messages.first { $0.type == .toolResult }
        #expect(toolResult?.text.contains("user_sessions") == true)

        let snapshot = try await client.snapshot()
        let session = snapshot.sessions.first { $0.id == DemoHubData.sessionApproval }
        #expect(session?.awaiting == nil)

        // 3. Deny workflow on fresh hub
        hub.reset()
        try await client.answerPermission(
            sessionID: DemoHubData.sessionApproval,
            requestID: DemoHubData.approvalRequestID,
            decision: "deny",
            note: nil
        )

        let denyBatch = try await client.messages(sessionID: DemoHubData.sessionApproval, afterSeq: 0)
        let denyResp = denyBatch.messages.first { $0.type == .approvalResponse }
        #expect(denyResp?.payload["decision"]?.stringValue == "deny")
    }

    @Test("Diff block and artifacts are rendered and extracted correctly")
    func testDiffAndArtifacts() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        let batch = try await client.messages(sessionID: DemoHubData.sessionDiffArtifacts, afterSeq: 0)
        let messages = batch.messages

        // Edit tool action with unified diff lines
        let editAction = messages.first { $0.payload["tool_name"]?.stringValue == "Edit" }
        #expect(editAction != nil)
        #expect(editAction?.payload["file_path"]?.stringValue == "src/components/Navigation.tsx")
        #expect(editAction?.payload["new_string"]?.stringValue?.contains("tabs-responsive") == true)

        // Extract artifacts
        let artifacts = SessionArtifactExtractor.artifacts(in: messages)
        #expect(artifacts.contains { $0.kind == .branch && $0.value == "feat/responsive-nav" })
        #expect(artifacts.contains { $0.kind == .pullRequest && $0.value.contains("#142") })
    }

    @Test("Follow-up turn receives a scripted assistant response")
    func testFollowUpTurnScriptedReply() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        let turnID = try await client.sendTurn(
            sessionID: DemoHubData.sessionReply,
            text: "Yes, please add the benchmark tests.",
            clientTurnID: UUID().uuidString
        )
        #expect(!turnID.isEmpty)

        let replyBatch = try await client.messages(sessionID: DemoHubData.sessionReply, afterSeq: 0)
        let userTurn = replyBatch.messages.first { $0.role == "user" && $0.text == "Yes, please add the benchmark tests." }
        #expect(userTurn != nil)

        let assistantReply = replyBatch.messages.last { $0.role == "assistant" }
        #expect(assistantReply?.text.contains("benchmarks/json_bench.rs") == true)

        let snapshot = try await client.snapshot()
        let session = snapshot.sessions.first { $0.id == DemoHubData.sessionReply }
        #expect(session?.awaiting == nil)
    }

    @Test("Interrupt and terminate operations succeed on demo session")
    func testInterruptAndTerminate() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        // Interrupt
        try await client.interrupt(sessionID: DemoHubData.sessionInterrupt)
        let interruptBatch = try await client.messages(sessionID: DemoHubData.sessionInterrupt, afterSeq: 0)
        #expect(interruptBatch.messages.contains { $0.text.contains("interrupted by user") })

        // Terminate
        let terminated = try await client.terminate(sessionID: DemoHubData.sessionInterrupt)
        #expect(terminated == true)

        let snapshot = try await client.snapshot()
        let session = snapshot.sessions.first { $0.id == DemoHubData.sessionInterrupt }
        #expect(session?.status == "terminated")
    }

    @Test("Session history search, filtering, and facets")
    func testHistoryAndFacets() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        // Facets
        let facets = try await client.historyFacets()
        #expect(facets.hosts.count == 3)
        #expect(facets.harnesses.contains("claude-code"))
        #expect(facets.repos.contains("acme/web-app"))

        // Search query
        let queryResult = try await client.historyPage(filter: HistoryFilter(query: "migration"), cursor: nil)
        #expect(queryResult.items.count >= 1)
        #expect(queryResult.items.allSatisfy { $0.title.lowercased().contains("migration") || ($0.summary?.lowercased().contains("migration") ?? false) })

        // State filter
        let finishedResult = try await client.historyPage(filter: HistoryFilter(states: [.finished]), cursor: nil)
        #expect(finishedResult.items.count >= 2)
        #expect(finishedResult.items.allSatisfy { $0.state == .finished })
    }

    @Test("Analytics supports 7d and 30d breakdowns")
    func testAnalytics() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        let analytics7 = try await client.analytics(filters: AnalyticsFilters(days: 7))
        let activity7 = try #require(analytics7.activity.data)
        #expect(activity7.totals.totalTokens > 0)
        #expect(activity7.projects.count >= 4)
        #expect(activity7.hosts.count == 3)
        #expect(activity7.harnesses.count == 2)

        let analytics30 = try await client.analytics(filters: AnalyticsFilters(days: 30))
        let activity30 = try #require(analytics30.activity.data)
        #expect(activity30.totals.totalTokens > activity7.totals.totalTokens)
    }

    @Test("Cockpit overview contains quota card provider accounts")
    func testCockpitOverviewAndQuota() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        let overview = try await client.cockpitOverview()
        #expect(overview.providerCapacity.status == .ok)
        let capacityData = try #require(overview.providerCapacity.data)
        #expect(capacityData.count == 3)

        let providers = Set(capacityData.map(\.provider))
        #expect(providers == ["anthropic", "openai", "google"])

        let anthropic = capacityData.first { $0.provider == "anthropic" }
        #expect(anthropic?.planLabel == "Team")
        #expect(anthropic?.windows.first?.usedPercent == 42)
    }

    @Test("Virtual folder browser and new session creation")
    func testFolderBrowserAndLaunch() async throws {
        let hub = DemoHub.shared
        hub.reset()
        let client = hub.makeClient()

        // Listing virtual folders
        let root = try await client.listFolders(hostID: "studio", path: "/projects")
        #expect(root.entries.contains { $0.name == "acme-web-app" && $0.isGitRepo == true })
        #expect(root.entries.contains { $0.name == "acme-api" && $0.isGitRepo == true })

        // Creating a new session
        let createdSessionID = try await client.createSession(
            hostID: "studio",
            harness: "claude-code",
            mode: "structured",
            prompt: "Refactor shopping cart component",
            cwd: "/projects/acme-web-app"
        )
        #expect(createdSessionID == DemoHubData.sessionLaunched)

        // Verifying it appears in the snapshot
        let snapshot = try await client.snapshot()
        let session = snapshot.sessions.first { $0.id == DemoHubData.sessionLaunched }
        #expect(session != nil)
        #expect(session?.preview == "Refactor shopping cart component")

        // Verifying greeting messages
        let launchBatch = try await client.messages(sessionID: DemoHubData.sessionLaunched, afterSeq: 0)
        #expect(launchBatch.messages.count >= 2)
        #expect(launchBatch.messages[0].text == "Refactor shopping cart component")
    }

    @Test("No private IPs, real hostnames, or real emails in demo data")
    func testNoPrivateData() {
        let serialized = String(describing: DemoHubData.defaultHosts)
            + String(describing: DemoHubData.defaultSessions)
            + String(describing: DemoHubData.providerAccounts)
            + String(describing: DemoHubData.defaultHistoryItems)

        // No real IP ranges
        #expect(!serialized.contains("192.168."))
        #expect(!serialized.contains("10.0."))
        #expect(!serialized.contains("100.64."))
        #expect(!serialized.contains("172.16."))

        // No personal email addresses (must be @example.com or @example.org)
        #expect(!serialized.contains("@gmail.com"))
        #expect(!serialized.contains("@google.com"))
        #expect(!serialized.contains("@anthropic.com"))

        // Only fictional host names
        for host in DemoHubData.defaultHosts {
            let id = host["host_id"] as? String ?? ""
            #expect(["studio", "workstation", "build-box"].contains(id))
        }
    }
}
