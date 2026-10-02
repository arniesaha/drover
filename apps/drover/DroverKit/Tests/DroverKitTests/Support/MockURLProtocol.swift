import Foundation
@testable import DroverKit

/// A boolean one thread raises and another reads — the mock's own cancellation
/// flag, and whatever a test needs to observe from a handler running off the
/// main actor.
final class MockFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    init() {}
    func raise() { lock.lock(); value = true; lock.unlock() }
    var isRaised: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

/// An isolated mock network environment for a test or session.
/// Routes requests by a per-session identifier header (`X-Mock-Session-ID`)
/// so that concurrent tests and suites never share handlers or leak requests.
final class MockNetwork: @unchecked Sendable {
    let id: String
    private let lock = NSLock()

    private var _handler: (@Sendable (URLRequest) -> (Int, Data))?
    private var _responseHeaders: [String: String]?
    private var _transportError: URLError?
    private var _responseDelay: (@Sendable (URLRequest) -> TimeInterval?)?
    private var _recordedClientTurnIDs: [String] = []

    init(id: String = UUID().uuidString) {
        self.id = id
        MockURLProtocol.register(self)
    }

    deinit {
        MockURLProtocol.unregister(self)
    }

    var handler: (@Sendable (URLRequest) -> (Int, Data))? {
        get { lock.withLock { _handler } }
        set { lock.withLock { _handler = newValue } }
    }

    var responseHeaders: [String: String]? {
        get { lock.withLock { _responseHeaders } }
        set { lock.withLock { _responseHeaders = newValue } }
    }

    var transportError: URLError? {
        get { lock.withLock { _transportError } }
        set { lock.withLock { _transportError = newValue } }
    }

    var responseDelay: (@Sendable (URLRequest) -> TimeInterval?)? {
        get { lock.withLock { _responseDelay } }
        set { lock.withLock { _responseDelay = newValue } }
    }

    var sentClientTurnIDs: [String] {
        lock.withLock { _recordedClientTurnIDs }
    }

    func resetRecordedRequests() {
        lock.withLock {
            _recordedClientTurnIDs.removeAll(keepingCapacity: true)
        }
    }

    func recordClientTurnID(in request: URLRequest) {
        guard request.httpMethod == "POST",
              request.url?.path.hasSuffix("/turns") == true,
              let object = try? JSONSerialization.jsonObject(with: request.bodyStreamData())
                as? [String: Any],
              let clientTurnID = object["client_turn_id"] as? String
        else { return }
        lock.withLock {
            _recordedClientTurnIDs.append(clientTurnID)
        }
    }

    func configuration() -> URLSessionConfiguration {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        cfg.httpAdditionalHeaders = [MockURLProtocol.sessionHeader: id]
        return cfg
    }

    func session() -> URLSession {
        URLSession(configuration: configuration())
    }

    func client(
        config: ServerConfig = ServerConfig(urlString: "http://test.local:7080")!,
        token: String = "test-token",
        credentialBindingID: UUID = testRecoveryBindingID,
        retryGate: HubRetryGate = HubRetryGate()
    ) -> DroverClient {
        DroverClient(
            config: config,
            token: token,
            credentialBindingID: credentialBindingID,
            session: session(),
            retryGate: retryGate
        )
    }
}

/// Test double intercepting `URLSession` traffic so `DroverClient` tests never
/// touch the network. Requests are scoped to a `MockNetwork` via the `X-Mock-Session-ID`
/// header attached to the session configuration.
final class MockURLProtocol: URLProtocol, @unchecked Sendable {
    static let sessionHeader = "X-Mock-Session-ID"

    private static let registryLock = NSLock()
    nonisolated(unsafe) private static var registry: [String: MockNetwork] = [:]

    static func register(_ mock: MockNetwork) {
        registryLock.withLock { registry[mock.id] = mock }
    }

    static func unregister(_ mock: MockNetwork) {
        registryLock.withLock { _ = registry.removeValue(forKey: mock.id) }
    }

    static func isRegistered(token: String) -> Bool {
        registryLock.withLock { registry[token] != nil }
    }

    static func mock(for token: String) -> MockNetwork? {
        registryLock.withLock { registry[token] }
    }

    static func session(for mock: MockNetwork) -> URLSession {
        mock.session()
    }

    /// Set by `stopLoading` so a delayed delivery for a cancelled request
    /// stays quiet rather than calling back into a finished task.
    private let isStopped = MockFlag()

    override class func canInit(with request: URLRequest) -> Bool {
        guard let token = request.value(forHTTPHeaderField: sessionHeader) else {
            return false
        }
        return isRegistered(token: token)
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let token = request.value(forHTTPHeaderField: Self.sessionHeader),
              let mock = Self.mock(for: token)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }

        mock.recordClientTurnID(in: request)
        if let transportError = mock.transportError {
            client?.urlProtocol(self, didFailWithError: transportError)
            return
        }
        guard let handler = mock.handler else { return }
        guard let delay = mock.responseDelay?(request), delay > 0 else {
            deliver(handler(request), headers: mock.responseHeaders)
            return
        }
        let pending = request
        let headers = mock.responseHeaders
        let isStopped = self.isStopped
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !isStopped.isRaised else { return }
            self.deliver(handler(pending), headers: headers)
        }
    }

    override func stopLoading() { isStopped.raise() }

    private func deliver(_ answer: (Int, Data), headers: [String: String]?) {
        let (status, body) = answer
        let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                       httpVersion: nil, headerFields: headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

extension URLRequest {
    /// `URLProtocol` sees request bodies as a stream (`httpBodyStream`), even
    /// when the caller set `httpBody` directly — read it fully for assertions.
    func bodyStreamData() -> Data {
        guard let stream = httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }

        var data = Data()
        let bufferSize = 4096
        var buffer = [UInt8](repeating: 0, count: bufferSize)
        while stream.hasBytesAvailable {
            let bytesRead = stream.read(&buffer, maxLength: bufferSize)
            if bytesRead > 0 {
                data.append(buffer, count: bytesRead)
            } else {
                break
            }
        }
        return data
    }
}
