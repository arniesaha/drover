import Foundation

// MARK: - NexusError

public enum NexusError: Error, Equatable {
    case unauthorized                    // 401
    case conflict(String)                // 409 body "error" text
    case badRequest(String)              // 400 body "error" text
    case unavailable(String)             // 404 body "error" text
    case transport(String)               // URLError etc.
    case decoding(String)
}

// MARK: - NexusClient

/// Talks to the central Nexus server's harness REST API over plain HTTP
/// (encrypted at the WireGuard/Tailscale hop, not at this layer — see
/// `NSAllowsArbitraryLoads` in the app's Info.plist).
public actor NexusClient {
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

    public func createSession(hostID: String, harness: String, mode: String,
                              prompt: String?, cwd: String?) async throws -> String {
        var payload: [String: Any] = ["harness": harness, "mode": mode]
        if let prompt { payload["prompt"] = prompt }
        if let cwd { payload["cwd"] = cwd }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let path = "/harness/hosts/\(encodePathComponent(hostID))/sessions"
        let data = try await request(path: path, method: "POST", body: body)
        let decoded = try decode(CreateSessionResponse.self, from: data)
        return decoded.sessionID
    }

    public func sendTurn(sessionID: String, text: String) async throws -> String {
        let body = try JSONSerialization.data(withJSONObject: ["text": text])
        let path = "/harness/sessions/\(encodePathComponent(sessionID))/turns"
        let data = try await request(path: path, method: "POST", body: body)
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
            throw NexusError.transport("invalid healthz URL")
        }
        var urlRequest = URLRequest(url: url.absoluteURL)
        urlRequest.httpMethod = "GET"
        do {
            let (_, response) = try await session.data(for: urlRequest)
            guard let http = response as? HTTPURLResponse else {
                throw NexusError.transport("non-HTTP response")
            }
            return (200..<300).contains(http.statusCode)
        } catch let error as NexusError {
            throw error
        } catch {
            return false
        }
    }

    public nonisolated func streamRequest(sessionID: String) -> URLRequest {
        wsRequest(sessionID: sessionID, suffix: "stream")
    }

    public nonisolated func terminalRequest(sessionID: String) -> URLRequest {
        wsRequest(sessionID: sessionID, suffix: "terminal")
    }

    // MARK: Private helpers

    private nonisolated func wsRequest(sessionID: String, suffix: String) -> URLRequest {
        var components = URLComponents(url: config.baseURL, resolvingAgainstBaseURL: false)
        let isSecure = components?.scheme == "https"
        components?.scheme = isSecure ? "wss" : "ws"
        components?.path = "/harness/sessions/\(encodePathComponent(sessionID))/\(suffix)"
        components?.query = nil
        let url = components?.url ?? config.baseURL
        var urlRequest = URLRequest(url: url)
        urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return urlRequest
    }

    private nonisolated func encodePathComponent(_ raw: String) -> String {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return raw.addingPercentEncoding(withAllowedCharacters: allowed) ?? raw
    }

    /// Builds the request, sends it, and maps non-2xx responses to
    /// `NexusError`. Returns the raw response body on success.
    private func request(path: String, method: String, body: Data?) async throws -> Data {
        guard let url = URL(string: path, relativeTo: config.baseURL) else {
            throw NexusError.transport("invalid URL for path \(path)")
        }
        var urlRequest = URLRequest(url: url.absoluteURL)
        urlRequest.httpMethod = method
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
            throw NexusError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw NexusError.transport("non-HTTP response")
        }

        switch http.statusCode {
        case 200..<300:
            return data
        case 401:
            throw NexusError.unauthorized
        case 409:
            throw NexusError.conflict(errorText(from: data))
        case 400:
            throw NexusError.badRequest(errorText(from: data))
        case 404:
            throw NexusError.unavailable(errorText(from: data))
        default:
            throw NexusError.transport("unexpected status \(http.statusCode)")
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
            throw NexusError.decoding("\(error)")
        }
    }

    private nonisolated func decodeMessageBatch(from data: Data) throws -> MessageBatch {
        do {
            return try MessageBatch.decode(from: data)
        } catch {
            throw NexusError.decoding("\(error)")
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
