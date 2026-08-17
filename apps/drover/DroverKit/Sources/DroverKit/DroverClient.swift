import Foundation

public enum MessagePageRequest: Sendable, Equatable {
    case newest(limit: Int)
    case older(beforeSeq: Int, limit: Int)
    case newer(afterSeq: Int, throughSeq: Int?, limit: Int)
}

// MARK: - DroverError

public enum DroverError: Error, Equatable {
    case unauthorized                    // 401
    case conflict(String)                // 409 body "error" text
    case badRequest(String)              // 400 body "error" text
    case unavailable(String)             // 404 body "error" text
    case transport(String)               // URLError etc.
    case httpStatus(Int, String)         // Other HTTP statuses
    case decoding(String)

    /// Canonical detail string for a client-side cancellation, set by
    /// `DroverClient` when it sees `URLError.cancelled`.
    public static let cancellationDetail = "cancelled"

    /// True for a request the app itself tore down — a superseded poll, a
    /// dismissed screen. Not a failure worth telling anyone about.
    public var isCancellation: Bool {
        guard case .transport(let detail) = self else { return false }
        return detail == Self.cancellationDetail
    }
}

// Without this, `"\(error)"` renders Swift's default reflection of the enum
// — users were shown literal `transport("cancelled")` in the sessions banner.
extension DroverError: LocalizedError {
    /// Named because the cold-open failure state reaches for these same two
    /// lines from outside this switch (see `connectionFailureReason`), and
    /// two screens quietly saying it differently is how copy drifts.
    public static let unreachableDescription = "Can't reach the hub"
    public static let unreachableTailscaleDescription = "Can't reach the hub over Tailscale"
    public static let malformedDescription = "Unexpected response from the hub"

    public var errorDescription: String? {
        switch self {
        case .unauthorized:
            return "Token rejected — check Settings"
        case .conflict(let message), .badRequest(let message):
            return message
        case .unavailable(let message):
            return message.isEmpty ? "That session is no longer available" : message
        case .transport:
            return isCancellation ? "Request cancelled" : Self.unreachableDescription
        case .httpStatus(let code, let message):
            return message.isEmpty ? "Server error (\(code))" : message
        case .decoding:
            return Self.malformedDescription
        }
    }

    /// Returns a contextual error description given whether the host is a Tailscale endpoint.
    public func localizedDescription(isTailscale: Bool) -> String {
        if case .transport = self, !isCancellation, isTailscale {
            return Self.unreachableTailscaleDescription
        }
        return errorDescription ?? Self.unreachableDescription
    }

    /// The user-facing reason for a connection attempt that never landed.
    ///
    /// Anything that is not already a `DroverError` — a raw `URLError` off a
    /// WebSocket task, say — collapses to the transport line rather than
    /// carrying `localizedDescription` through. Foundation's copy talks about
    /// the phone ("The Internet connection appears to be offline"), and these
    /// screens have always talked about the hub: the radio being fine says
    /// nothing about whether the fleet answered.
    public static func connectionFailureReason(_ error: Error, isTailscale: Bool = false) -> String {
        if let droverError = error as? DroverError {
            return droverError.localizedDescription(isTailscale: isTailscale)
        }
        return isTailscale ? unreachableTailscaleDescription : unreachableDescription
    }
}

// MARK: - Pairing

/// What the hub hands back when a pairing code is redeemed. The token belongs
/// to this device alone and is revocable on its own.
public struct PairResponse: Decodable, Sendable, Equatable {
    public let token: String
    public let credentialID: String
    public let scope: String
    public let serverID: String
    public let fleetName: String

    private enum CodingKeys: String, CodingKey {
        case token
        case credentialID = "credential_id"
        case scope
        case serverID = "server_id"
        case fleetName = "fleet_name"
    }
}

// MARK: - DroverClient

/// Talks to the central Drover server's harness REST API over plain HTTP
/// (encrypted at the WireGuard/Tailscale hop, not at this layer — see
/// `NSAllowsArbitraryLoads` in the app's Info.plist).
public actor DroverClient {
    private static let cockpitRequestTimeout: TimeInterval = 60
    /// Filesystem lookups run while someone types, so they get a shorter
    /// budget than the 15s default: the host itself gives up after 3s, and
    /// anything longer than this is the hub or the network, not the listing.
    private static let pathRequestTimeout: TimeInterval = 10
    public nonisolated let config: ServerConfig
    private let token: String
    private let session: URLSession

    public init(config: ServerConfig, token: String, session: URLSession = .shared) {
        self.config = config
        self.token = token
        self.session = session
    }

    // MARK: Pairing

    /// Redeem a scanned pairing code for this device's own token.
    ///
    /// Static, and the only call in the app that sends no `Authorization`
    /// header: the device has no credential yet, which is the entire point of
    /// pairing. The hub burns the code on success, so this must not be retried
    /// blindly — a second attempt with the same code returns 410.
    public static func pair(
        payload: PairingPayload,
        deviceName: String,
        session: URLSession = .shared
    ) async throws -> PairResponse {
        var request = URLRequest(
            url: payload.serverURL.appendingPathComponent("auth/pair")
        )
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(
            withJSONObject: ["code": payload.code, "device_name": deviceName]
        )

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            throw DroverError.transport(
                error.code == .cancelled
                    ? DroverError.cancellationDetail
                    : error.localizedDescription
            )
        }
        guard let http = response as? HTTPURLResponse else {
            throw DroverError.transport("non-HTTP response")
        }
        let validated = try validate(data, response: http)
        do {
            return try JSONDecoder().decode(PairResponse.self, from: validated)
        } catch {
            throw DroverError.decoding("\(error)")
        }
    }

    // MARK: Public API

    public func snapshot() async throws -> HarnessSnapshot {
        let data = try await request(path: "/harness", method: "GET", body: nil)
        return try decode(HarnessSnapshot.self, from: data)
    }

    public func cockpitOverview(days: Int = 7) async throws -> CockpitOverview {
        let url = try queryURL(path: "/cockpit/overview", items: [
            ("days", String(days)),
        ])
        let data = try await request(
            url: url, method: "GET", body: nil,
            timeout: Self.cockpitRequestTimeout
        )
        return try decode(CockpitOverview.self, from: data)
    }

    public func analytics(filters: AnalyticsFilters = AnalyticsFilters()) async throws
        -> AnalyticsSnapshot {
        let url = try queryURL(path: "/analytics", items: [
            ("days", String(filters.days)),
            ("host_id", filters.hostID),
            ("harness", filters.harness),
            ("provider", filters.provider),
            ("model", filters.model),
            ("project_key", filters.projectKey),
            ("limit", filters.limit == 25 ? nil : String(filters.limit)),
            ("project_cursor", filters.projectCursor),
            ("harness_cursor", filters.harnessCursor),
            ("host_cursor", filters.hostCursor),
            ("model_cursor", filters.modelCursor),
        ])
        let data = try await request(
            url: url, method: "GET", body: nil,
            timeout: Self.cockpitRequestTimeout
        )
        return try decode(AnalyticsSnapshot.self, from: data)
    }

    public func insights(filters: InsightFilters = InsightFilters()) async throws
        -> InsightPage {
        let url = try queryURL(path: "/insights", items: [
            ("state", filters.state?.rawValue),
            ("severity", filters.severity?.rawValue),
            ("confidence", filters.confidence?.rawValue),
            ("analyzer_class", filters.analyzerClass?.rawValue),
            ("host", filters.host),
            ("harness", filters.harness),
            ("target_type", filters.targetType),
            ("target_id", filters.targetID),
            ("cursor", filters.cursor),
            ("limit", String(filters.limit)),
        ])
        let data = try await request(url: url, method: "GET", body: nil)
        return try decode(InsightPage.self, from: data)
    }

    public func insightDetail(findingID: String) async throws -> InsightDetail {
        let path = "/insights/\(encodePathComponent(findingID))"
        let data = try await request(path: path, method: "GET", body: nil)
        return try decode(InsightDetail.self, from: data)
    }

    public func acknowledgeInsight(findingID: String) async throws -> InsightFinding {
        let path = "/insights/\(encodePathComponent(findingID))/acknowledge"
        let data = try await request(path: path, method: "POST", body: Data("{}".utf8))
        return try decode(InsightMutationResponse.self, from: data).finding
    }

    public func dismissInsight(findingID: String, reason: String) async throws
        -> InsightFinding {
        let path = "/insights/\(encodePathComponent(findingID))/dismiss"
        let body = try encodeJSON(DismissInsightBody(reason: reason))
        let data = try await request(path: path, method: "POST", body: body)
        return try decode(InsightMutationResponse.self, from: data).finding
    }

    public func checkInsight(findingID: String) async throws -> InsightCheckResponse {
        let path = "/insights/\(encodePathComponent(findingID))/check"
        let data = try await request(path: path, method: "POST", body: Data("{}".utf8))
        return try decode(InsightCheckResponse.self, from: data)
    }

    public func contentAnalysisStatus() async throws -> ContentAnalysisStatus {
        let result = try await contentAnalysisStateRequest(
            path: "/insights/content-analysis", method: "GET", body: nil
        )
        return result.status
    }

    public func setContentAnalysisConsent(
        backend: ContentAnalysisBackend,
        externalDisclosureAccepted: Bool = false
    ) async throws -> ContentAnalysisConsentResult {
        let body = try encodeJSON(ContentAnalysisConsentBody(
            backend: backend,
            externalDisclosureAccepted: externalDisclosureAccepted
        ))
        return try await contentAnalysisStateRequest(
            path: "/insights/content-analysis/consent", method: "POST", body: body
        )
    }

    public func revokeContentAnalysis() async throws -> ContentAnalysisConsentResult {
        try await contentAnalysisStateRequest(
            path: "/insights/content-analysis/revoke",
            method: "POST",
            body: Data("{}".utf8)
        )
    }

    public func purgeContentExcerpts() async throws -> PurgeContentExcerptsResponse {
        let data = try await request(
            path: "/insights/content-excerpts", method: "DELETE", body: nil
        )
        return try decode(PurgeContentExcerptsResponse.self, from: data)
    }

    public func authStatus(hostID: String, harness: String) async throws -> HarnessAuthStatus {
        let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/status"
        let data = try await request(path: path, method: "GET", body: nil)
        return try decode(HarnessAuthStatus.self, from: data)
    }

    public func modelCatalog(
        hostID: String,
        harness: String,
        force: Bool = false
    ) async throws -> HarnessModelCatalog {
        let path = "/harness/hosts/\(encodePathComponent(hostID))/model-catalog"
        let url = try queryURL(path: path, items: [
            ("harness", harness),
            ("refresh", force ? "1" : "0"),
        ])
        let data = try await request(url: url, method: "GET", body: nil)
        return try decode(HarnessModelCatalog.self, from: data)
    }

    /// Asks a host which directories the half-typed `path` could become.
    ///
    /// A parent that does not exist is not an error — see `PathCompletion`.
    /// A host that cannot be reached is, and surfaces as `DroverError`.
    public func completePath(hostID: String, path: String) async throws -> PathCompletion {
        let basePath = "/harness/hosts/\(encodePathComponent(hostID))/fs/complete"
        let url = try queryURL(path: basePath, items: [("path", path)])
        let data = try await request(
            url: url, method: "GET", body: nil, timeout: Self.pathRequestTimeout
        )
        return try decode(PathCompletion.self, from: data)
    }

    /// Which of `paths` exist on the host *and* are directories.
    ///
    /// One round trip for the whole batch: the launch sheet checks every
    /// untagged favorite at once when its host changes, and a request per
    /// favorite would be a burst on every switch.
    public func pathsExist(hostID: String, paths: [String]) async throws -> [String: Bool] {
        let requestPath = "/harness/hosts/\(encodePathComponent(hostID))/fs/exists"
        let body = try JSONSerialization.data(withJSONObject: ["paths": paths])
        let data = try await request(
            path: requestPath, method: "POST", body: body,
            timeout: Self.pathRequestTimeout
        )
        return try decode(PathExistsResponse.self, from: data).exists
    }

    public func startAuthFlow(hostID: String, harness: String) async throws -> HarnessAuthFlow {
        let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/start"
        let data = try await request(path: path, method: "POST", body: Data("{}".utf8))
        return try decode(HarnessAuthFlow.self, from: data)
    }

    public func authFlow(hostID: String, harness: String, flowID: String) async throws -> HarnessAuthFlow {
        let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/flows/\(encodePathComponent(flowID))"
        let data = try await request(path: path, method: "GET", body: nil)
        return try decode(HarnessAuthFlow.self, from: data)
    }

    /// Type a line into a running login CLI -- the code the browser hands
    /// back at the end of an OAuth round trip. Only flows reporting
    /// `supportsInput` have a terminal on the other end to receive it.
    public func submitAuthInput(hostID: String, harness: String, flowID: String,
                                text: String) async throws -> HarnessAuthFlow {
        let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/flows/\(encodePathComponent(flowID))/input"
        let body = try JSONSerialization.data(withJSONObject: ["text": text])
        let data = try await request(path: path, method: "POST", body: body)
        return try decode(HarnessAuthFlow.self, from: data)
    }

    public func cancelAuthFlow(hostID: String, harness: String, flowID: String) async throws -> HarnessAuthFlow {
        let path = "/harness/hosts/\(encodePathComponent(hostID))/auth/\(encodePathComponent(harness))/flows/\(encodePathComponent(flowID))/cancel"
        let data = try await request(path: path, method: "POST", body: Data("{}".utf8))
        return try decode(HarnessAuthFlow.self, from: data)
    }

    public func messages(sessionID: String, afterSeq: Int) async throws -> MessageBatch {
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/messages?after_seq=\(afterSeq)"
        let data = try await request(path: path, method: "GET", body: nil)
        return try decodeMessageBatch(from: data)
    }

    public func messagePage(
        sessionID: String,
        request pageRequest: MessagePageRequest
    ) async throws -> MessagePage {
        var components = URLComponents(
            url: config.baseURL, resolvingAgainstBaseURL: false
        )
        components?.percentEncodedPath =
            "/harness/sessions/\(encodePathComponent(sessionID))/messages"
        switch pageRequest {
        case .newest(let limit):
            components?.queryItems = [URLQueryItem(name: "limit", value: "\(limit)")]
        case .older(let beforeSeq, let limit):
            components?.queryItems = [
                URLQueryItem(name: "before_seq", value: "\(beforeSeq)"),
                URLQueryItem(name: "limit", value: "\(limit)"),
            ]
        case .newer(let afterSeq, let throughSeq, let limit):
            var queryItems = [
                URLQueryItem(name: "after_seq", value: "\(afterSeq)"),
            ]
            if let throughSeq {
                queryItems.append(
                    URLQueryItem(name: "through_seq", value: "\(throughSeq)")
                )
            }
            queryItems.append(URLQueryItem(name: "limit", value: "\(limit)"))
            components?.queryItems = queryItems
        }
        guard let url = components?.url else {
            throw DroverError.transport("invalid session message page URL")
        }
        let data = try await request(url: url, method: "GET", body: nil)
        do {
            return try MessagePage.decode(from: data)
        } catch {
            throw DroverError.decoding("\(error)")
        }
    }

    public func createSession(hostID: String, harness: String, mode: String,
                              prompt: String?, cwd: String?,
                              images: [TurnAttachment] = [],
                              model: String? = nil,
                              thinkingEffort: String? = nil) async throws -> String {
        var payload: [String: Any] = ["harness": harness, "mode": mode]
        if let prompt { payload["prompt"] = prompt }
        if let cwd { payload["cwd"] = cwd }
        if !images.isEmpty {
            payload["images"] = images.map {
                ["media_type": $0.mediaType, "data_base64": $0.data.base64EncodedString()]
            }
        }
        if let model { payload["model"] = model }
        if let thinkingEffort { payload["thinking_effort"] = thinkingEffort }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let path = "/harness/hosts/\(encodePathComponent(hostID))/sessions"
        let data = try await request(path: path, method: "POST", body: body,
                                     timeout: images.isEmpty ? nil : 60)
        let decoded = try decode(CreateSessionResponse.self, from: data)
        return decoded.sessionID
    }

    public func sendTurn(sessionID: String, text: String,
                         images: [TurnAttachment] = [],
                         model: String? = nil,
                         thinkingEffort: String? = nil,
                         clientTurnID: String? = nil) async throws -> String {
        var payload: [String: Any] = ["text": text]
        if !images.isEmpty {
            payload["images"] = images.map {
                ["media_type": $0.mediaType, "data_base64": $0.data.base64EncodedString()]
            }
        }
        if let model { payload["model"] = model }
        if let thinkingEffort { payload["thinking_effort"] = thinkingEffort }
        if let clientTurnID { payload["client_turn_id"] = clientTurnID }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/turns"
        // Image bodies are orders of magnitude larger than any other request
        // this client makes — give them a cellular-realistic budget.
        let data = try await request(path: path, method: "POST", body: body,
                                     timeout: images.isEmpty ? nil : 60)
        let decoded = try decode(TurnResponse.self, from: data)
        return decoded.turnID
    }

    public func answerPermission(sessionID: String, requestID: String,
                                 decision: String, note: String?) async throws {
        var payload: [String: Any] = ["request_id": requestID, "decision": decision]
        if let note { payload["note"] = note }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/permission"
        _ = try await request(path: path, method: "POST", body: body)
    }

    /// Continues a session as a fresh one seeded with a server-built handoff
    /// prompt: for structured-capable targets the server creates a structured
    /// session with the handoff context as its first turn; for shell targets
    /// (and native resume) it creates a PTY session with a typed-in seed.
    /// Optionally retargets a different host and/or harness; both default to
    /// the source session's own. The returned `isStructured` tells the caller
    /// which screen to open (chat vs terminal).
    public func continueSession(sessionID: String, targetHostID: String? = nil,
                                targetHarness: String? = nil) async throws -> ContinuedSession {
        var payload: [String: Any] = [:]
        if let targetHostID { payload["target_host_id"] = targetHostID }
        if let targetHarness { payload["target_harness"] = targetHarness }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/continue"
        let data = try await request(path: path, method: "POST", body: body)
        let decoded = try decode(CreateSessionResponse.self, from: data)
        return ContinuedSession(sessionID: decoded.sessionID,
                                isStructured: decoded.mode == "structured")
    }

    public func interrupt(sessionID: String) async throws {
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/interrupt"
        _ = try await request(path: path, method: "POST", body: nil)
    }

    public func terminate(sessionID: String) async throws {
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/terminate"
        _ = try await request(path: path, method: "POST", body: nil)
    }

    /// Hand the hub this device's APNs token so it can push "needs you"
    /// alerts when the app is not running.
    ///
    /// Registration is per *credential*, not per install: the hub stores the
    /// token against the device credential this client's bearer token belongs
    /// to, so re-pairing a phone replaces its registration rather than
    /// accumulating dead ones.
    public func registerAPNsToken(
        _ token: Data, environment: APNsEnvironment = .current()
    ) async throws {
        let payload: [String: String] = [
            "token": token.apnsHexString,
            "environment": environment.rawValue,
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)
        _ = try await request(path: "/auth/device/apns", method: "PUT", body: body)
    }

    /// Drop this device's registration, so a signed-out phone stops lighting
    /// up for a fleet it no longer belongs to.
    public func unregisterAPNsToken() async throws {
        _ = try await request(path: "/auth/device/apns", method: "DELETE", body: nil)
    }

    /// Deliberately bypasses the shared `request()` helper: its contract is
    /// different from the authed endpoints — it answers "is the server
    /// reachable?" as a Bool, sends no Authorization header, and collapses
    /// network-level `URLError`s to `false` instead of throwing `.transport`.
    public func healthz() async throws -> Bool {
        guard let url = URL(string: "/healthz", relativeTo: config.baseURL) else {
            throw DroverError.transport("invalid healthz URL")
        }
        var urlRequest = URLRequest(url: url.absoluteURL)
        urlRequest.httpMethod = "GET"
        do {
            let (_, response) = try await session.data(for: urlRequest)
            guard let http = response as? HTTPURLResponse else {
                throw DroverError.transport("non-HTTP response")
            }
            return (200..<300).contains(http.statusCode)
        } catch let error as DroverError {
            throw error
        } catch {
            return false
        }
    }

    public nonisolated func streamRequest(sessionID: String, afterSeq: Int? = nil) -> URLRequest {
        wsRequest(sessionID: sessionID, suffix: "stream", query: afterSeq.map { "after_seq=\($0)" })
    }

    public nonisolated func terminalRequest(sessionID: String) -> URLRequest {
        wsRequest(sessionID: sessionID, suffix: "terminal")
    }

    // MARK: Private helpers

    private nonisolated func wsRequest(sessionID: String, suffix: String,
                                       query: String? = nil) -> URLRequest {
        var components = URLComponents(url: config.baseURL, resolvingAgainstBaseURL: false)
        let isSecure = components?.scheme == "https"
        components?.scheme = isSecure ? "wss" : "ws"
        components?.path = "/harness/sessions/\(encodePathComponent(sessionID))/\(suffix)"
        components?.percentEncodedQuery = query
        let url = components?.url ?? config.baseURL
        var urlRequest = URLRequest(url: url)
        urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return urlRequest
    }

    private nonisolated func encodePathComponent(_ raw: String) -> String {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return raw.addingPercentEncoding(withAllowedCharacters: allowed) ?? raw
    }

    private nonisolated func queryURL(
        path: String,
        items: [(String, String?)]
    ) throws -> URL {
        var components = URLComponents(
            url: config.baseURL, resolvingAgainstBaseURL: false
        )
        components?.percentEncodedPath = path
        let query = items.compactMap { name, value -> String? in
            guard let value else { return nil }
            return "\(encodeQueryComponent(name))=\(encodeQueryComponent(value))"
        }.joined(separator: "&")
        components?.percentEncodedQuery = query.isEmpty ? nil : query
        guard let url = components?.url else {
            throw DroverError.transport("invalid URL for path \(path)")
        }
        return url
    }

    private nonisolated func encodeQueryComponent(_ raw: String) -> String {
        encodePathComponent(raw)
    }

    private nonisolated func encodeJSON<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        do {
            return try encoder.encode(value)
        } catch {
            throw DroverError.decoding("failed to encode request: \(error)")
        }
    }

    /// Builds the request, sends it, and maps non-2xx responses to
    /// `DroverError`. Returns the raw response body on success.
    private func request(path: String, method: String, body: Data?,
                         timeout: TimeInterval? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: config.baseURL) else {
            throw DroverError.transport("invalid URL for path \(path)")
        }
        return try await request(
            url: url.absoluteURL, method: method, body: body, timeout: timeout
        )
    }

    private func request(url: URL, method: String, body: Data?,
                         timeout: TimeInterval? = nil) async throws -> Data {
        let (data, http) = try await send(
            url: url, method: method, body: body, timeout: timeout
        )
        return try validatedData(data, response: http)
    }

    private func contentAnalysisStateRequest(
        path: String, method: String, body: Data?
    ) async throws -> ContentAnalysisConsentResult {
        guard let url = URL(string: path, relativeTo: config.baseURL) else {
            throw DroverError.transport("invalid URL for path \(path)")
        }
        let (data, http) = try await send(
            url: url.absoluteURL, method: method, body: body, timeout: nil
        )
        let decoded = try? JSONDecoder().decode(ContentAnalysisStatus.self, from: data)
        if !(200..<300).contains(http.statusCode) {
            guard http.statusCode == 503,
                  let decoded,
                  decoded.propagation == .failed else {
                _ = try validatedData(data, response: http)
                throw DroverError.httpStatus(http.statusCode, "unexpected status")
            }
        }
        guard let status = decoded else {
            throw DroverError.decoding("invalid content-analysis mutation response")
        }
        let outcome: ContentAnalysisMutationOutcome
        switch status.propagation {
        case .failed?, .unknown?: outcome = .failed
        case .partial?: outcome = .partial
        case .complete?: outcome = http.statusCode == 207 ? .partial : .complete
        case nil: outcome = http.statusCode == 207 ? .failed : .complete
        }
        return ContentAnalysisConsentResult(status: status, outcome: outcome)
    }

    private func send(
        url: URL, method: String, body: Data?, timeout: TimeInterval?
    ) async throws -> (Data, HTTPURLResponse) {
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = method
        urlRequest.timeoutInterval = timeout ?? 15
        urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            urlRequest.httpBody = body
            urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            // Normalize here rather than downstream: URLError's
            // localizedDescription for a cancel is the useless
            // "The operation couldn't be completed. (NSURLErrorDomain error
            // -999.)" on some platforms and a bare "cancelled" on others, so
            // no substring test on it is reliable. The code always is.
            if (error as? URLError)?.code == .cancelled {
                throw DroverError.transport(DroverError.cancellationDetail)
            }
            throw DroverError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw DroverError.transport("non-HTTP response")
        }

        return (data, http)
    }

    private func validatedData(_ data: Data, response http: HTTPURLResponse) throws -> Data {
        try Self.validate(data, response: http)
    }

    /// Static so the unauthenticated pairing call below can share exactly this
    /// mapping rather than growing a second copy that drifts from it.
    nonisolated static func validate(
        _ data: Data, response http: HTTPURLResponse
    ) throws -> Data {
        switch http.statusCode {
        case 200..<300:
            return data
        case 401:
            throw DroverError.unauthorized
        case 409:
            throw DroverError.conflict(errorText(from: data))
        case 400:
            throw DroverError.badRequest(errorText(from: data))
        case 404:
            throw DroverError.unavailable(errorText(from: data))
        default:
            let text = errorText(from: data)
            throw DroverError.httpStatus(
                http.statusCode,
                text.isEmpty ? "unexpected status \(http.statusCode)" : text
            )
        }
    }

    /// Extracts the `"error"` field from a JSON body, falling back to the
    /// raw body text when it isn't present or isn't valid JSON.
    nonisolated static func errorText(from data: Data) -> String {
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let text = object["error"] as? String {
            return text
        } else if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let text = object["detail"] as? String {
            return text
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    private nonisolated func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw DroverError.decoding("\(error)")
        }
    }

    private nonisolated func decodeMessageBatch(from data: Data) throws -> MessageBatch {
        do {
            return try MessageBatch.decode(from: data)
        } catch {
            throw DroverError.decoding("\(error)")
        }
    }
}

// MARK: - ContinuedSession

/// Result of a `/continue` handoff: the new session's id plus whether it is
/// structured (chat UI) or a PTY (terminal). `mode` is absent on the wire
/// for PTY creates and older daemons — both mean terminal.
public struct ContinuedSession: Sendable, Equatable {
    public let sessionID: String
    public let isStructured: Bool

    public init(sessionID: String, isStructured: Bool) {
        self.sessionID = sessionID
        self.isStructured = isStructured
    }
}

// MARK: - Wire response shapes

private struct CreateSessionResponse: Decodable {
    let sessionID: String
    let mode: String?

    private enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case mode
    }
}

private struct TurnResponse: Decodable {
    let turnID: String

    private enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
    }
}

private struct DismissInsightBody: Encodable {
    let reason: String
}

private struct ContentAnalysisConsentBody: Encodable {
    let backend: ContentAnalysisBackend
    let externalDisclosureAccepted: Bool

    private enum CodingKeys: String, CodingKey {
        case backend
        case externalDisclosureAccepted = "external_disclosure_accepted"
    }
}
