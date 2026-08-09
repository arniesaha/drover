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
    public var errorDescription: String? {
        switch self {
        case .unauthorized:
            return "Token rejected — check Settings"
        case .conflict(let message), .badRequest(let message):
            return message
        case .unavailable(let message):
            return message.isEmpty ? "That session is no longer available" : message
        case .transport:
            return isCancellation ? "Request cancelled" : "Can't reach the hub"
        case .httpStatus(let code, let message):
            return message.isEmpty ? "Server error (\(code))" : message
        case .decoding:
            return "Unexpected response from the hub"
        }
    }
}

// MARK: - DroverClient

/// Talks to the central Drover server's harness REST API over plain HTTP
/// (encrypted at the WireGuard/Tailscale hop, not at this layer — see
/// `NSAllowsArbitraryLoads` in the app's Info.plist).
public actor DroverClient {
    private let config: ServerConfig
    private let token: String
    private let session: URLSession

    public init(config: ServerConfig, token: String, session: URLSession = .shared) {
        self.config = config
        self.token = token
        self.session = session
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
        let data = try await request(url: url, method: "GET", body: nil)
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
        ])
        let data = try await request(url: url, method: "GET", body: nil)
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
        let data = try await request(
            path: "/insights/content-analysis", method: "GET", body: nil
        )
        return try decode(ContentAnalysisStatus.self, from: data)
    }

    public func setContentAnalysisConsent(
        backend: ContentAnalysisBackend,
        externalDisclosureAccepted: Bool = false
    ) async throws -> ContentAnalysisStatus {
        let body = try encodeJSON(ContentAnalysisConsentBody(
            backend: backend,
            externalDisclosureAccepted: externalDisclosureAccepted
        ))
        let data = try await request(
            path: "/insights/content-analysis/consent", method: "POST", body: body
        )
        return try decode(ContentAnalysisStatus.self, from: data)
    }

    public func revokeContentAnalysis() async throws -> ContentAnalysisStatus {
        let data = try await request(
            path: "/insights/content-analysis/revoke",
            method: "POST",
            body: Data("{}".utf8)
        )
        return try decode(ContentAnalysisStatus.self, from: data)
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
                         thinkingEffort: String? = nil) async throws -> String {
        var payload: [String: Any] = ["text": text]
        if !images.isEmpty {
            payload["images"] = images.map {
                ["media_type": $0.mediaType, "data_base64": $0.data.base64EncodedString()]
            }
        }
        if let model { payload["model"] = model }
        if let thinkingEffort { payload["thinking_effort"] = thinkingEffort }
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
    private nonisolated func errorText(from data: Data) -> String {
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
