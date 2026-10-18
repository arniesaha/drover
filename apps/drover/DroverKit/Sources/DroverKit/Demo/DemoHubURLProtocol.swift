import Foundation

/// URLProtocol implementation for demo mode.
///
/// Routes requests directed to `demo.drover.invalid` to `DemoHub.shared`.
/// Fails and blocks any attempt to connect to any other host, guaranteeing
/// 100% offline operation with zero network traffic.
public final class DemoHubURLProtocol: URLProtocol, @unchecked Sendable {
    public override class func canInit(with request: URLRequest) -> Bool {
        // Intercept all requests passing through the demo URLSession.
        true
    }

    public override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    public override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }

        // Hard offline isolation guarantee: only synthetic demo origin is answered.
        guard url.host == DemoHubData.syntheticHost else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }

        let response = DemoHub.shared.handleRequest(request)

        guard let httpResponse = HTTPURLResponse(
            url: url,
            statusCode: response.status,
            httpVersion: "HTTP/1.1",
            headerFields: response.headers
        ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotParseResponse))
            return
        }

        client?.urlProtocol(self, didReceive: httpResponse, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    public override func stopLoading() {}
}
