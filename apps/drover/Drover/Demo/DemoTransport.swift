import Foundation
import DroverKit

/// The demo's complete in-process HTTP and WebSocket surface. Every request
/// is intercepted by its own ephemeral URLSession or connector; unknown and
/// non-demo routes fail locally and can never fall through to a live session.
final class DemoTransport: @unchecked Sendable {
    let session: URLSession
    let client: DroverClient
    let webSocketConnector: DemoWebSocketConnector
    private let state: DemoTransportState

    init(recorder: DemoOperationRecorder) throws {
        guard let config = ServerConfig(urlString: DemoScenarioData.serverURLString) else {
            throw DemoTransportError.invalidConfiguration
        }
        let state = DemoTransportState(recorder: recorder)
        DemoURLProtocol.install(state: state)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DemoURLProtocol.self]
        let session = URLSession(configuration: configuration)
        self.state = state
        self.session = session
        self.webSocketConnector = DemoWebSocketConnector(state: state)
        self.client = DroverClient(
            config: config,
            token: DemoScenarioData.syntheticToken,
            credentialBindingID: DemoScenarioData.credentialBindingID,
            session: session
        )
    }

    func reset() {
        state.reset()
    }

    func simulateReconnect() {
        state.simulateReconnect()
    }
}

private enum DemoTransportError: LocalizedError {
    case invalidConfiguration

    var errorDescription: String? { "The local demo configuration is invalid." }
}

private struct DemoHTTPResponse {
    let status: Int
    let body: Data

    static func json(status: Int, _ object: Any) -> DemoHTTPResponse {
        guard JSONSerialization.isValidJSONObject(object) else {
            preconditionFailure("Demo transport must emit valid JSON")
        }
        return DemoHTTPResponse(
            status: status,
            body: try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        )
    }
}

/// URLProtocol is scoped to `DemoTransport.session`; it does not globally
/// register and cannot intercept, reuse, or fall back to the normal app
/// client's URLSession.
private final class DemoURLProtocol: URLProtocol {
    private static let stateLock = NSLock()
    nonisolated(unsafe) private static var installedState: DemoTransportState?

    static func install(state: DemoTransportState) {
        stateLock.withLock { installedState = state }
    }

    private static var state: DemoTransportState? {
        stateLock.withLock { installedState }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let state = Self.state, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        let response = state.route(request, url: url)
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
}

/// This connector never instantiates URLSessionWebSocketTask. A reconnect is
/// a controlled local stream termination, which lets the normal MessageStream
/// and ChatView show their real reconnect state without any socket traffic.
struct DemoWebSocketConnector: WebSocketConnecting {
    fileprivate let state: DemoTransportState

    func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        state.openWebSocket(request)
    }
}

fileprivate final class DemoTransportState: @unchecked Sendable {
    private let lock = NSLock()
    private let recorder: DemoOperationRecorder
    private var approvalAnswered = false
    private var turnCount = 0
    private var sockets: [UUID: AsyncThrowingStream<String, Error>.Continuation] = [:]

    init(recorder: DemoOperationRecorder) {
        self.recorder = recorder
    }

    func reset() {
        let openSockets = lock.withLock { () -> [AsyncThrowingStream<String, Error>.Continuation] in
            approvalAnswered = false
            turnCount = 0
            let values = Array(sockets.values)
            sockets.removeAll()
            return values
        }
        openSockets.forEach { $0.finish() }
    }

    func simulateReconnect() {
        let openSockets = lock.withLock { () -> [AsyncThrowingStream<String, Error>.Continuation] in
            let values = Array(sockets.values)
            sockets.removeAll()
            return values
        }
        openSockets.forEach { $0.finish(throwing: URLError(.networkConnectionLost)) }
    }

    func openWebSocket(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        guard isValidWebSocketRequest(request) else {
            return AsyncThrowingStream { $0.finish(throwing: URLError(.unsupportedURL)) }
        }
        recorder.recordLocalWebSocket()
        let id = UUID()
        return AsyncThrowingStream { continuation in
            lock.withLock { sockets[id] = continuation }
            continuation.onTermination = { [weak self] _ in
                self?.lock.withLock { self?.sockets.removeValue(forKey: id) }
            }
        }
    }

    func route(_ request: URLRequest, url: URL) -> DemoHTTPResponse {
        guard url.scheme == "https", url.host == "demo.drover.invalid" else {
            return .json(status: 400, ["error": "demo transport refuses non-local origin"])
        }
        recorder.recordLocalHTTP()
        let path = url.path
        switch (request.httpMethod, path) {
        case ("GET", "/harness"):
            return .json(status: 200, snapshotObject())
        case ("GET", "/harness/hosts/\(DemoScenarioData.hostID)/model-catalog"):
            return .json(status: 200, modelCatalogObject())
        case ("POST", "/harness/hosts/\(DemoScenarioData.hostID)/sessions"):
            return .json(status: 201, [
                "session_id": DemoScenarioData.launchedSessionID,
                "mode": "structured",
            ])
        case ("GET", let path) where isMessagesPath(path):
            return .json(status: 200, historyObject(for: sessionID(fromMessagesPath: path)))
        case ("POST", let path) where path == "/harness/sessions/\(DemoScenarioData.chatSessionID)/turns"
            || path == "/harness/sessions/\(DemoScenarioData.launchedSessionID)/turns":
            return acceptTurn(request)
        case ("POST", let path) where path == "/harness/sessions/\(DemoScenarioData.approvalSessionID)/permission":
            return answerApproval(request)
        default:
            return .json(status: 404, ["error": "unknown local demo route"])
        }
    }

    private func isValidWebSocketRequest(_ request: URLRequest) -> Bool {
        guard let url = request.url,
              url.scheme == "wss",
              url.host == "demo.drover.invalid"
        else { return false }
        return url.path == "/harness/sessions/\(DemoScenarioData.approvalSessionID)/stream"
            || url.path == "/harness/sessions/\(DemoScenarioData.chatSessionID)/stream"
            || url.path == "/harness/sessions/\(DemoScenarioData.launchedSessionID)/stream"
    }

    private func isMessagesPath(_ path: String) -> Bool {
        [
            DemoScenarioData.approvalSessionID,
            DemoScenarioData.chatSessionID,
            DemoScenarioData.launchedSessionID,
        ].contains(sessionID(fromMessagesPath: path))
    }

    private func sessionID(fromMessagesPath path: String) -> String {
        path
            .replacingOccurrences(of: "/harness/sessions/", with: "")
            .replacingOccurrences(of: "/messages", with: "")
    }

    private func acceptTurn(_ request: URLRequest) -> DemoHTTPResponse {
        guard let payload = request.jsonObject,
              let clientTurnID = payload["client_turn_id"] as? String,
              UUID(uuidString: clientTurnID) != nil
        else {
            return .json(status: 400, ["error": "a local demo turn needs an id"])
        }
        let sequence = lock.withLock { () -> Int in
            turnCount += 1
            return 10 + turnCount * 2
        }
        emit([
            "event_id": "demo-local-input-\(clientTurnID)",
            "seq": sequence,
            "type": "user_input",
            "role": "user",
            "text": "Demo message sent locally.",
            "turn_id": clientTurnID,
            "payload": [:],
        ])
        emit([
            "event_id": "demo-local-reply-\(sequence)",
            "seq": sequence + 1,
            "type": "assistant_output",
            "role": "assistant",
            "text": "This sample response was generated locally for the evaluation demo.",
            "payload": [:],
        ])
        return .json(status: 202, ["turn_id": "demo-local-turn-\(sequence)"])
    }

    private func answerApproval(_ request: URLRequest) -> DemoHTTPResponse {
        guard let payload = request.jsonObject,
              payload["request_id"] as? String == DemoScenarioData.approvalRequestID,
              let decision = payload["decision"] as? String,
              decision == "allow" || decision == "deny"
        else {
            return .json(status: 400, ["error": "invalid local demo approval"])
        }
        lock.withLock { approvalAnswered = true }
        emit([
            "event_id": "demo-approval-response",
            "seq": 3,
            "type": "approval_response",
            "role": "user",
            "text": decision == "allow" ? "Allowed once in this local demo." : "Denied in this local demo.",
            "payload": ["request_id": DemoScenarioData.approvalRequestID],
        ])
        return .json(status: 200, [:])
    }

    private func emit(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8)
        else { return }
        let continuations = lock.withLock { Array(sockets.values) }
        continuations.forEach { $0.yield(text) }
    }

    private func snapshotObject() -> [String: Any] {
        let hasAnswered = lock.withLock { approvalAnswered }
        var sessions: [[String: Any]] = [
            sessionObject(
                id: DemoScenarioData.approvalSessionID,
                preview: "Review a sample deployment change",
                awaiting: hasAnswered ? nil : "approval"
            ),
            sessionObject(
                id: DemoScenarioData.chatSessionID,
                preview: "Sample product brief discussion",
                awaiting: nil
            ),
        ]
        if lock.withLock({ turnCount > 0 }) {
            sessions.append(sessionObject(
                id: DemoScenarioData.launchedSessionID,
                preview: "New local demo session",
                awaiting: nil
            ))
        }
        return [
            "hosts": [[
                "host_id": DemoScenarioData.hostID,
                "status": "online",
                "connection_kind": "direct",
                "capabilities": [
                    "display_name": DemoScenarioData.hostName,
                    "harnesses": [["name": "codex", "enabled": true]],
                ],
            ]],
            "sessions": sessions,
            "cwd_suggestions": [[
                "path": "/demo/workspace",
                "source": "sample",
                "host_id": DemoScenarioData.hostID,
            ]],
        ]
    }

    private func sessionObject(id: String, preview: String, awaiting: String?) -> [String: Any] {
        var result: [String: Any] = [
            "session_id": id,
            "host_id": DemoScenarioData.hostID,
            "harness": "codex",
            "mode": "structured",
            "status": "working",
            "cwd": "/demo/workspace",
            "preview": preview,
            "last_activity": "2026-09-04T00:00:00Z",
        ]
        if let awaiting { result["awaiting"] = awaiting }
        return result
    }

    private func historyObject(for sessionID: String) -> [String: Any] {
        var messages: [[String: Any]] = [[
            "event_id": "demo-intro-\(sessionID)",
            "seq": 1,
            "type": "assistant_output",
            "role": "assistant",
            "text": "This is a local sample conversation for App Review.",
            "payload": [:],
        ]]
        if sessionID == DemoScenarioData.approvalSessionID {
            messages.append([
                "event_id": "demo-approval-request",
                "seq": 2,
                "type": "approval_prompt",
                "role": "assistant",
                "text": "Approve the sample deployment check?",
                "payload": [
                    "request_id": DemoScenarioData.approvalRequestID,
                    "tool": "sample deployment check",
                    "input": ["environment": "evaluation"],
                ],
            ])
            if lock.withLock({ approvalAnswered }) {
                messages.append([
                    "event_id": "demo-approval-response",
                    "seq": 3,
                    "type": "approval_response",
                    "role": "user",
                    "text": "Allowed once in this local demo.",
                    "payload": ["request_id": DemoScenarioData.approvalRequestID],
                ])
            }
        }
        return [
            "messages": messages,
            "page_min_seq": 1,
            "page_max_seq": messages.count,
            "max_seq": messages.count,
            "has_older": false,
            "has_newer": false,
        ]
    }

    private func modelCatalogObject() -> [String: Any] {
        [
            "schema_version": 1,
            "host_id": DemoScenarioData.hostID,
            "harness": "codex",
            "account_scope_id": NSNull(),
            "harness_version": NSNull(),
            "discovered_at": "2026-09-04T00:00:00Z",
            "stale": false,
            "stale_reason": NSNull(),
            "models": [],
        ]
    }
}

private extension URLRequest {
    var jsonObject: [String: Any]? {
        guard let body = bodyData,
              let value = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
        else { return nil }
        return value
    }

    /// `httpBody` is nil for every request that reaches a `URLProtocol`:
    /// URLSession hands the body over as `httpBodyStream` instead. Reading
    /// only `httpBody` made every POST body here invisible, so the approval
    /// route rejected a perfectly good payload as "invalid local demo
    /// approval".
    private var bodyData: Data? {
        if let httpBody { return httpBody }
        guard let stream = httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufferSize = 4096
        var buffer = [UInt8](repeating: 0, count: bufferSize)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: bufferSize)
            guard read > 0 else { break }
            data.append(buffer, count: read)
        }
        return data.isEmpty ? nil : data
    }
}
