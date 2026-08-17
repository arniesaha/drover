import Foundation

/// A boolean one thread raises and another reads — the mock's own cancellation
/// flag, and whatever a test needs to observe from a handler running off the
/// main actor.
final class MockFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    func raise() { lock.lock(); value = true; lock.unlock() }
    var isRaised: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

/// Test double intercepting `URLSession` traffic so `DroverClient` tests never
/// touch the network. Install a `handler` before each request and it decides
/// the (status, body) pair returned for that request.
final class MockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) -> (Int, Data))?

    /// Fail the request at the transport layer instead of answering it, so
    /// tests can exercise the `DroverError.transport` path (offline hub,
    /// cancelled poll) that no (status, body) pair can represent. Takes
    /// precedence over `handler`; clear it when the test is done.
    nonisolated(unsafe) static var transportError: URLError?

    /// Seconds to hold a given request's answer before delivering it, or nil
    /// (the default) to answer inline on the loader thread.
    ///
    /// Sleeping inside `handler` instead does not work: `startLoading` runs on
    /// a thread the session reuses, so a blocked handler blocks every *other*
    /// request too, and two requests can never be in flight at once. A delayed
    /// delivery returns the thread immediately and lets them overlap, which is
    /// the only way to test a response arriving after a newer one superseded
    /// it. Clear it when the test is done.
    nonisolated(unsafe) static var responseDelay: (@Sendable (URLRequest) -> TimeInterval?)?

    /// Set by `stopLoading` so a delayed delivery for a cancelled request
    /// stays quiet rather than calling back into a finished task.
    private let isStopped = MockFlag()

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        if let transportError = Self.transportError {
            client?.urlProtocol(self, didFailWithError: transportError)
            return
        }
        guard let handler = Self.handler else { return }
        guard let delay = Self.responseDelay?(request), delay > 0 else {
            deliver(handler(request))
            return
        }
        let pending = request
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.isStopped.isRaised else { return }
            self.deliver(handler(pending))
        }
    }

    override func stopLoading() { isStopped.raise() }

    private func deliver(_ answer: (Int, Data)) {
        let (status, body) = answer
        let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    static func session() -> URLSession {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: cfg)
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
