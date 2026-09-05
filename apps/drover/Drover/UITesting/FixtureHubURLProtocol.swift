import Foundation

#if DEBUG
/// Mutable state for the deterministic HTTP hub. Only a synthetic client turn
/// ID, receipt count, and lookup count are persisted; request text and any
/// credential are deliberately discarded.
final class FixtureReceiptState: @unchecked Sendable {
    private enum Key {
        static let clientTurnID = "fixture.client-turn-id"
        static let receiptCount = "fixture.receipt-count"
        static let submissionCount = "fixture.submission-count"
        static let historyLookupsAfterReceipt = "fixture.history-lookups-after-receipt"
    }

    private let lock = NSLock()
    private let defaults: UserDefaults

    init(runID: String) {
        let suiteName = "com.arnab.drover.ui-fixture.\(runID)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            preconditionFailure("Could not create isolated fixture defaults")
        }
        self.defaults = defaults
    }

    var receiptCount: Int {
        lock.withLock { defaults.integer(forKey: Key.receiptCount) }
    }

    var submissionCount: Int {
        lock.withLock { defaults.integer(forKey: Key.submissionCount) }
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
        return FixtureScenarioData.historyData(sessionID: sessionID, receiptTurnID: receiptID)
    }
}

struct FixtureHubResponse {
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
final class FixtureHubURLProtocol: URLProtocol {
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

    override func stopLoading() {}

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
        case ("GET", "/harness"):
            return FixtureHubResponse(status: 200, body: FixtureScenarioData.snapshotData())
        case ("GET", let path) where path == "/harness/hosts/\(FixtureScenarioData.coreJourney.hostID)/model-catalog":
            return FixtureHubResponse(status: 200, body: FixtureScenarioData.modelCatalogData())
        case ("POST", let path) where path == "/harness/hosts/\(FixtureScenarioData.coreJourney.hostID)/sessions":
            return .json(status: 201, [
                "session_id": FixtureScenarioData.launchedSessionID,
                "mode": "structured",
            ])
        case ("POST", let path) where path == "/harness/sessions/\(FixtureScenarioData.primarySessionID)/turns":
            return state.acceptTurn(from: request)
        case ("GET", let path) where path.hasPrefix("/harness/sessions/") && path.hasSuffix("/messages"):
            let sessionID = path
                .replacingOccurrences(of: "/harness/sessions/", with: "")
                .replacingOccurrences(of: "/messages", with: "")
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
