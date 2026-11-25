import Foundation

#if DEBUG
import UIKit
/// Mutable state for the deterministic HTTP hub. Only a synthetic client turn
/// ID, receipt count, and lookup count are persisted; request text and any
/// credential are deliberately discarded.
final class FixtureReceiptState: @unchecked Sendable {
    private enum Key {
        static let clientTurnID = "fixture.client-turn-id"
        static let receiptCount = "fixture.receipt-count"
        static let submissionCount = "fixture.submission-count"
        static let historyLookupsAfterReceipt = "fixture.history-lookups-after-receipt"
        static let harnessSnapshotCount = "fixture.harness-snapshot-count"
        static let analyticsResponseCount = "fixture.analytics-response-count"
    }

    private let lock = NSLock()
    private let defaults: UserDefaults
    let kind: FixtureScenarioKind
    let delaysAnalytics: Bool
    private var backgroundObserver: NSObjectProtocol?
    private var analyticsReleased = false
    private var pendingAnalytics: [UUID: @Sendable () -> Void] = [:]

    init(
        runID: String,
        kind: FixtureScenarioKind = .coreJourney,
        delaysAnalytics: Bool = false
    ) {
        self.kind = kind
        self.delaysAnalytics = delaysAnalytics
        let suiteName = "com.arnab.drover.ui-fixture.\(runID)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            preconditionFailure("Could not create isolated fixture defaults")
        }
        self.defaults = defaults
        if let phase = ProcessInfo.processInfo.environment["DROVER_UI_TEST_RESULT_LIFECYCLE"] {
            backgroundObserver = NotificationCenter.default.addObserver(
                forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main
            ) { [weak self] _ in
                guard let self else { return }
                self.lock.withLock { self.defaults.set(true, forKey: "fixture.result-completed." + phase) }
            }
        }
    }

    deinit {
        if let backgroundObserver { NotificationCenter.default.removeObserver(backgroundObserver) }
    }

    var receiptCount: Int {
        lock.withLock { defaults.integer(forKey: Key.receiptCount) }
    }

    var submissionCount: Int {
        lock.withLock { defaults.integer(forKey: Key.submissionCount) }
    }

    var harnessSnapshotCount: Int {
        lock.withLock { defaults.integer(forKey: Key.harnessSnapshotCount) }
    }

    var analyticsResponseCount: Int {
        lock.withLock { defaults.integer(forKey: Key.analyticsResponseCount) }
    }

    /// Test-controlled barrier: gestures, not elapsed wall time, release the response.
    func holdAnalytics(id: UUID, delivery: @escaping @Sendable () -> Void) {
        let held = lock.withLock {
            guard delaysAnalytics, !analyticsReleased else { return false }
            pendingAnalytics[id] = delivery
            return true
        }
        if !held { delivery() }
    }

    func cancelAnalytics(id: UUID) {
        lock.withLock { _ = pendingAnalytics.removeValue(forKey: id) }
    }

    func releaseAnalytics() {
        let deliveries = lock.withLock {
            analyticsReleased = true
            let deliveries = Array(pendingAnalytics.values)
            pendingAnalytics.removeAll()
            return deliveries
        }
        for delivery in deliveries { delivery() }
    }

    func recordAnalyticsResponse() {
        lock.withLock {
            defaults.set(defaults.integer(forKey: Key.analyticsResponseCount) + 1,
                         forKey: Key.analyticsResponseCount)
        }
    }

    /// The live-update UI test needs a real polling transition, rather than a
    /// timer that merely assumes a refresh happened.  On the third snapshot
    /// the selected session moves to the needs-you bucket while its Chat view
    /// remains pushed above the Home list.
    func harnessSnapshotData() -> Data {
        let liveUpdateSessionID = ProcessInfo.processInfo.environment[
            "DROVER_UI_TEST_LIVE_UPDATE_SESSION_ID"
        ]
        let updatedSessionID = lock.withLock { () -> String? in
            let next = defaults.integer(forKey: Key.harnessSnapshotCount) + 1
            defaults.set(next, forKey: Key.harnessSnapshotCount)
            return next >= 3 ? liveUpdateSessionID : nil
        }
        return unreadLifecycleData(FixtureScenarioData.snapshotData(liveUpdateSessionID: updatedSessionID), snapshot: true)
    }

    /// A real background transition releases this synthetic completion.
    /// Polling, loading Chat and launch speed cannot release it.
    private func unreadLifecycleData(_ data: Data, snapshot: Bool, sessionID: String? = nil) -> Data {
        guard let phase = ProcessInfo.processInfo.environment["DROVER_UI_TEST_RESULT_LIFECYCLE"],
              var body = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return data }
        let completed = lock.withLock {
            defaults.bool(forKey: "fixture.result-completed." + phase)
        }
        let completionID = "lifecycle-completed-" + phase
        if snapshot {
            var sessions = (body["sessions"] as! [[String: Any]]).filter {
                $0["session_id"] as? String == FixtureScenarioData.primarySessionID
            }
            if let index = sessions.firstIndex(where: { $0["session_id"] as? String == FixtureScenarioData.primarySessionID }) {
                sessions[index]["status"] = completed ? (phase == "terminated" ? "terminated" : "running") : "working"
                sessions[index]["awaiting"] = completed ? "input" : nil
                sessions[index]["notification_kind"] = completed ? "finished" : nil
                sessions[index]["attention_id"] = completed ? completionID : nil
                sessions[index]["preview"] = completed ? "Result generation " + phase : "Working generation " + phase
                if phase == "terminated" {
                    sessions.append(["session_id": "old-lifecycle-only", "host_id": "old-nas",
                                     "harness": "codex", "mode": "structured", "status": "terminated",
                                     "attention_id": "old-stop", "preview": "Old lifecycle only",
                                     "last_activity": "2026-09-01T00:00:00Z"])
                }
                body["sessions"] = sessions
            }
        } else {
            let seq = phase == "second" ? 10 : 1
            var messages: [[String: Any]] = [[
                "event_id": "lifecycle-response-" + phase, "seq": seq,
                "type": "assistant_output", "role": "assistant",
                "text": completed ? "Final response generation " + phase : "Working response generation " + phase,
                "payload": [:],
            ]]
            if completed {
                messages.append(["event_id": completionID, "seq": seq + 1,
                                 "type": "status", "role": "system", "text": "turn.completed",
                                 "payload": ["turn_complete": true, "awaiting": "input"]])
            }
            if phase == "terminated", completed {
                messages.append(["event_id": "lifecycle-stop", "seq": seq + 2,
                                 "type": "status", "role": "system", "text": "Session terminated",
                                 "payload": ["session_status": "terminated", "turn_complete": true]])
            }
            if phase == "terminated", sessionID == "old-lifecycle-only" {
                messages = [["event_id": "old-stop", "seq": seq,
                             "type": "status", "role": "system", "text": "Session terminated",
                             "payload": ["session_status": "terminated", "turn_complete": true]]]
            }
            body["messages"] = messages
            body["page_min_seq"] = seq
            body["page_max_seq"] = messages.last?["seq"] ?? seq
            body["max_seq"] = messages.last?["seq"] ?? seq
        }
        return try! JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    }

    func acceptTurn(from request: URLRequest) -> FixtureHubResponse {
        guard let body = request.bodyData(),
              let payload = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              let clientTurnID = payload["client_turn_id"] as? String,
              UUID(uuidString: clientTurnID) != nil
        else {
            return .json(status: 400, ["error": "missing synthetic client turn id"])
        }

        return lock.withLock {
            defaults.set(defaults.integer(forKey: Key.submissionCount) + 1, forKey: Key.submissionCount)
            if let savedID = defaults.string(forKey: Key.clientTurnID) {
                guard savedID == clientTurnID else {
                    return .json(status: 409, ["error": "fixture accepts one logical turn"])
                }
                // The same id is an idempotent receipt lookup: it must never
                // count as another logical send.
                return .json(status: 202, ["turn_id": FixtureScenarioData.syntheticTurnID])
            }
            defaults.set(clientTurnID, forKey: Key.clientTurnID)
            defaults.set(1, forKey: Key.receiptCount)
            defaults.set(0, forKey: Key.historyLookupsAfterReceipt)
            return .json(status: 202, ["turn_id": FixtureScenarioData.syntheticTurnID])
        }
    }

    func historyData(for sessionID: String) -> Data {
        guard sessionID == FixtureScenarioData.primarySessionID else {
            return FixtureScenarioData.historyData(sessionID: sessionID, receiptTurnID: nil)
        }

        let receiptID: String? = lock.withLock {
            guard let clientTurnID = defaults.string(forKey: Key.clientTurnID) else {
                return nil
            }
            let lookups = defaults.integer(forKey: Key.historyLookupsAfterReceipt) + 1
            defaults.set(lookups, forKey: Key.historyLookupsAfterReceipt)
            // The first recreation catch-up remains unresolved; the explicit
            // manual Check delivery performs the next catch-up and gets the
            // exact original turn id.
            return lookups >= 2 ? clientTurnID : nil
        }
        return unreadLifecycleData(FixtureScenarioData.historyData(sessionID: sessionID, receiptTurnID: receiptID), snapshot: false, sessionID: sessionID)
    }
}

struct FixtureHubResponse: Sendable {
    let status: Int
    let body: Data

    static func json(status: Int, _ object: [String: Any]) -> FixtureHubResponse {
        FixtureHubResponse(
            status: status,
            body: try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        )
    }
}

/// URLSession-only fixture transport. It is installed into one ephemeral
/// session by `UITestScenario`; it can never receive a request from the live
/// app session and it never opens a network connection.
final class FixtureHubURLProtocol: URLProtocol, @unchecked Sendable {
    private let deliveryLock = NSRecursiveLock()
    private var stopped = false
    private let analyticsID = UUID()
    private static let stateLock = NSLock()
    // `URLProtocol` invokes instances off the main actor. Accesses are
    // serialized by `stateLock`; make that externally synchronized contract
    // explicit to Swift 6 rather than incorrectly pinning protocol loading to
    // the main actor.
    nonisolated(unsafe) private static var receiptState: FixtureReceiptState?

    static func install(receiptState: FixtureReceiptState) {
        stateLock.withLock { self.receiptState = receiptState }
    }

    private static var state: FixtureReceiptState? {
        stateLock.withLock { receiptState }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let state = Self.state, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        let response = route(request, url: url, state: state)
        if url.path == "/analytics", state.delaysAnalytics {
            state.holdAnalytics(id: analyticsID) { [weak self] in
                self?.deliver(response, url: url, state: state)
            }
        } else {
            deliver(response, url: url, state: state)
        }
    }

    private func deliver(_ response: FixtureHubResponse, url: URL, state: FixtureReceiptState) {
        deliveryLock.lock()
        defer { deliveryLock.unlock() }
        guard !stopped else { return }
        if url.path == "/analytics" { state.recordAnalyticsResponse() }
        let http = HTTPURLResponse(
            url: url,
            statusCode: response.status,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {
        deliveryLock.withLock { stopped = true }
        Self.state?.cancelAnalytics(id: analyticsID)
    }

    private func route(
        _ request: URLRequest,
        url: URL,
        state: FixtureReceiptState
    ) -> FixtureHubResponse {
        let path = url.path
        guard url.host == "fixture.drover.invalid", url.scheme == "https" else {
            return .json(status: 400, ["error": "fixture refuses non-synthetic origin"])
        }
        switch (request.httpMethod, path) {
        case ("GET", "/work/list"):
            return .json(status: 503, ["error": "work control unavailable"])
        case ("GET", "/cockpit/overview") where state.kind == .observability:
            return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.overview)
        case ("GET", "/analytics"):
            let metric = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "metric" })?.value
            return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.analytics(metric: metric))
        case ("GET", "/insights/content-analysis"):
            return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.contentAnalysisStatus)
        case ("GET", "/insights"):
            return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.insights)
        case ("GET", "/sessions/history"):
            return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.history)
        case ("GET", "/sessions/history/facets"):
            return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.historyFacets)
        case ("GET", let path) where path == "/insights/\(FixtureScenarioData.insightFindingID)":
            return FixtureHubResponse(status: 200, body: FixtureScenarioData.insightDetailData())
        case ("GET", "/harness"):
            switch state.kind {
            case .longStreaming:
                return FixtureHubResponse(status: 200, body: FixtureScenarioData.longStreamingSnapshotData())
            case .observability:
                return FixtureHubResponse(status: 200, body: ObservabilityFixtureData.homeSnapshot)
            case .capabilityJourney:
                return FixtureHubResponse(status: 200, body: FixtureScenarioData.capabilitySnapshotData())
            case .coreJourney, .insightDetail:
                return FixtureHubResponse(status: 200, body: state.harnessSnapshotData())
            }
        case ("GET", let path) where path == "/harness/hosts/\(FixtureScenarioData.coreJourney.hostID)/fs/list":
            return FolderBrowserFixture.response(url: url)
        case ("GET", let path) where path == "/harness/hosts/\(FixtureScenarioData.coreJourney.hostID)/model-catalog":
            return FixtureHubResponse(status: 200, body: FixtureScenarioData.modelCatalogData())
        case ("POST", let path) where path == "/harness/hosts/\(FixtureScenarioData.coreJourney.hostID)/sessions":
            return .json(status: 201, [
                "session_id": FixtureScenarioData.launchedSessionID,
                "mode": "structured",
            ])
        case ("POST", let path) where state.kind == .longStreaming && path.hasSuffix("/permission"):
            return .json(status: 200, ["status": "ok"])
        case ("POST", let path) where path == "/harness/sessions/\(FixtureScenarioData.primarySessionID)/turns":
            return state.acceptTurn(from: request)
        case ("GET", let path) where path.hasPrefix("/harness/sessions/") && path.hasSuffix("/messages"):
            let sessionID = path
                .replacingOccurrences(of: "/harness/sessions/", with: "")
                .replacingOccurrences(of: "/messages", with: "")
            if state.kind == .longStreaming, sessionID == FixtureScenarioData.primarySessionID {
                return FixtureHubResponse(status: 200, body: LongStreamingTranscriptFixture.historyData(url: url))
            }
            return FixtureHubResponse(status: 200, body: state.historyData(for: sessionID))
        default:
            return .json(status: 404, ["error": "unknown fixture route"])
        }
    }
}

private extension URLRequest {
    func bodyData() -> Data? {
        if let httpBody { return httpBody }
        guard let stream = httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
#endif
