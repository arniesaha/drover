import Foundation

/// Receive-only fake WebSocket stream for demo mode sessions.
/// Deliberately never opens a live socket. Connects into DemoHub's broadcast
/// bus to receive live session events. For the streaming demo session,
/// it delivers incremental assistant chunks at regular intervals.
public struct DemoHubWebSocketConnector: WebSocketConnecting, @unchecked Sendable {
    public var chunkInterval: Duration

    public init(chunkInterval: Duration = .milliseconds(300)) {
        self.chunkInterval = chunkInterval
    }

    public func connect(_ request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            guard let url = request.url,
                  url.host == DemoHubData.syntheticHost,
                  url.scheme == "wss" else {
                continuation.finish(throwing: URLError(.unsupportedURL))
                return
            }

            let path = url.path
            let sessionID: String
            if path.hasPrefix("/harness/sessions/") && path.hasSuffix("/stream") {
                sessionID = path
                    .replacingOccurrences(of: "/harness/sessions/", with: "")
                    .replacingOccurrences(of: "/stream", with: "")
            } else {
                sessionID = ""
            }

            let streamID = UUID()
            DemoHub.shared.registerStreamContinuation(sessionID: sessionID, id: streamID, continuation: continuation)

            let isStreamingSession = sessionID == DemoHubData.sessionStreaming
            let chunkInterval = self.chunkInterval

            if isStreamingSession {
                let pump = Task {
                    let existing = DemoHub.shared.currentMessages(for: sessionID)
                    if existing.count <= 2 {
                        let streamChunks = [
                            "Analyzing payment form validation...\n\n",
                            "```typescript\nexport const checkoutPaymentSchema = z.object({\n  cardNumber: z.string().regex(/^\\d{16}$/, 'Must be 16 digits'),\n  expiryMonth: z.number().int().min(1).max(12),\n  expiryYear: z.number().int().min(2026).max(2035),\n  cvv: z.string().regex(/^\\d{3,4}$/, 'Invalid CVV'),\n  applePayToken: z.string().optional(),\n});\n```\n\n",
                            "Running validation test suite against mock checkout payload:\n\n- Validated card number length and Luhn checksum.\n- Verified Apple Pay token parsing.\n- Validated future expiration dates.\n\nAll 12 validation checks passed.",
                        ]

                        var seq = 3
                        for chunk in streamChunks {
                            do {
                                try await Task.sleep(for: chunkInterval)
                            } catch {
                                break
                            }
                            if Task.isCancelled { break }

                            let message: [String: Any] = [
                                "event_id": "stream-chunk-\(seq)",
                                "seq": seq,
                                "type": "assistant_output",
                                "role": "assistant",
                                "text": chunk,
                                "payload": [:] as [String: Any],
                            ]
                            DemoHub.shared.appendMessage(sessionID: sessionID, message: message)
                            seq += 1
                        }
                    }
                }

                continuation.onTermination = { _ in
                    pump.cancel()
                    DemoHub.shared.unregisterStreamContinuation(sessionID: sessionID, id: streamID)
                }
            } else {
                continuation.onTermination = { _ in
                    DemoHub.shared.unregisterStreamContinuation(sessionID: sessionID, id: streamID)
                }
            }
        }
    }
}
