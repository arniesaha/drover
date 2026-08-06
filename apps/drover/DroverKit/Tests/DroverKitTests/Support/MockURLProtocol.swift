import Foundation

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

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        if let transportError = Self.transportError {
            client?.urlProtocol(self, didFailWithError: transportError)
            return
        }
        guard let handler = Self.handler else { return }
        let (status, body) = handler(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

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
