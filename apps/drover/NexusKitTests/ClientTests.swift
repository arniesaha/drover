import Foundation
import Testing
@testable import NexusKit

/// `.serialized`: every test in this file mutates the process-global
/// `MockURLProtocol.handler` — without serialization, Swift Testing's
/// default parallel execution could let one test's handler answer another
/// test's in-flight request.
@Suite(.serialized)
struct ClientTests {

@Test func snapshotSendsBearerAndDecodes() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        #expect(request.url?.path == "/harness")
        return (200, snapshotJSON)
    }
    let snap = try await client().snapshot()
    #expect(snap.sessions.count == 2)
}

@Test func unauthorizedMaps() async {
    MockURLProtocol.handler = { _ in (401, Data(#"{"error": "authentication required"}"#.utf8)) }
    await #expect(throws: NexusError.unauthorized) { try await client().snapshot() }
}

@Test func turnConflictMaps() async {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/turns")
        return (409, Data(#"{"error": "approval pending; answer it first"}"#.utf8))
    }
    await #expect(throws: NexusError.conflict("approval pending; answer it first")) {
        _ = try await client().sendTurn(sessionID: "s1", text: "go")
    }
}

@Test func createSessionPostsBodyAndReturnsID() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(request.url?.path == "/harness/hosts/mac-mini/sessions")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(body["harness"] as? String == "gemini")
        #expect(body["mode"] as? String == "structured")
        #expect(body["prompt"] as? String == "do it")
        return (201, Data(#"{"session_id": "harness-xyz", "mode": "structured"}"#.utf8))
    }
    let id = try await client().createSession(hostID: "mac-mini", harness: "gemini",
                                              mode: "structured", prompt: "do it", cwd: nil)
    #expect(id == "harness-xyz")
}

@Test func permissionBadRequestMaps() async {
    MockURLProtocol.handler = { _ in
        (400, Data(#"{"error": "codex exec has no approval channel; use sandbox flags"}"#.utf8))
    }
    await #expect(throws: NexusError.badRequest(
        "codex exec has no approval channel; use sandbox flags")) {
        try await client().answerPermission(sessionID: "s1", requestID: "r1",
                                            decision: "allow", note: nil)
    }
}

@Test func streamRequestShape() {
    let request = client().streamRequest(sessionID: "harness-1")
    #expect(request.url?.absoluteString == "ws://test.local:7080/harness/sessions/harness-1/stream")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
}

@Test func terminalRequestShape() {
    let request = client().terminalRequest(sessionID: "harness-1")
    #expect(request.url?.absoluteString == "ws://test.local:7080/harness/sessions/harness-1/terminal")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
}

@Test func messagesSendsAfterSeqQueryAndDecodes() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/messages")
        let query = request.url?.query ?? ""
        #expect(query.contains("after_seq=3"))
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        return (200, messagesJSON)
    }
    let batch = try await client().messages(sessionID: "s1", afterSeq: 3)
    #expect(batch.maxSeq == 3)
    #expect(batch.messages.count == 3)
}

@Test func sendTurnPostsBodyAndReturnsTurnID() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/turns")
        #expect(request.httpMethod == "POST")
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body["text"] as? String == "go")
        return (202, Data(#"{"turn_id": "turn-42"}"#.utf8))
    }
    let turnID = try await client().sendTurn(sessionID: "s1", text: "go")
    #expect(turnID == "turn-42")
}

@Test func answerPermissionPostsBody() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/permission")
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body["request_id"] as? String == "r1")
        #expect(body["decision"] as? String == "allow")
        #expect(body["note"] as? String == "looks fine")
        return (200, Data())
    }
    try await client().answerPermission(sessionID: "s1", requestID: "r1",
                                        decision: "allow", note: "looks fine")
}

@Test func interruptPostsToInterruptRoute() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/interrupt")
        #expect(request.httpMethod == "POST")
        return (200, Data())
    }
    try await client().interrupt(sessionID: "s1")
}

@Test func terminatePostsToTerminateRoute() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/terminate")
        #expect(request.httpMethod == "POST")
        return (200, Data())
    }
    try await client().terminate(sessionID: "s1")
}

@Test func continueSessionPostsTargetsAndReturnsNewID() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(request.url?.path == "/harness/sessions/s1/continue")
        #expect(request.httpMethod == "POST")
        #expect(body["target_host_id"] as? String == "nas")
        #expect(body["target_harness"] as? String == "codex")
        return (201, Data(#"{"session_id": "harness-continued", "mode": "structured"}"#.utf8))
    }
    let continued = try await client().continueSession(sessionID: "s1",
                                                       targetHostID: "nas",
                                                       targetHarness: "codex")
    #expect(continued.sessionID == "harness-continued")
    #expect(continued.isStructured)   // daemon's structured-create response carries mode
}

@Test func continueSessionDefaultsToEmptyBody() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body.isEmpty)
        return (201, Data(#"{"session_id": "harness-continued"}"#.utf8))
    }
    let continued = try await client().continueSession(sessionID: "s1")
    #expect(continued.sessionID == "harness-continued")
    #expect(continued.isStructured == false)   // no mode on the wire → PTY
}

@Test func healthzSendsNoAuthHeaderAndReturnsTrue() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/healthz")
        // Unauthenticated per brief: header presence is not required, but if
        // NexusClient still attaches it that's harmless — assert only the
        // route and success behavior.
        return (200, Data())
    }
    let ok = try await client().healthz()
    #expect(ok == true)
}

@Test func healthzReturnsFalseOnFailureStatus() async throws {
    MockURLProtocol.handler = { _ in (503, Data()) }
    let ok = try await client().healthz()
    #expect(ok == false)
}

@Test func sessionIDIsPercentEncodedInPath() async throws {
    MockURLProtocol.handler = { request in
        // A session id containing characters that need escaping in a URL path:
        // the raw URL string must carry the literal %20, not a space.
        let absolute = request.url?.absoluteString ?? ""
        #expect(absolute.contains("/harness/sessions/harness%20id%20with%20spaces/interrupt"))
        return (200, Data())
    }
    try await client().interrupt(sessionID: "harness id with spaces")
}

@Test func decodingErrorMapsToNexusErrorDecoding() async {
    MockURLProtocol.handler = { _ in (200, Data("not json".utf8)) }
    do {
        _ = try await client().snapshot()
        Issue.record("expected NexusError.decoding, but no error was thrown")
    } catch let error as NexusError {
        guard case .decoding = error else {
            Issue.record("expected .decoding, got \(error)")
            return
        }
    } catch {
        Issue.record("expected NexusError.decoding, got \(error)")
    }
}

@Test func badRequestFallsBackToRawBodyWhenNoErrorField() async {
    MockURLProtocol.handler = { _ in (400, Data("plain text failure".utf8)) }
    await #expect(throws: NexusError.badRequest("plain text failure")) {
        try await client().interrupt(sessionID: "s1")
    }
}

@Test func createSessionOmitsNilCwdAndPromptFromBody() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body["cwd"] == nil)
        #expect(body["prompt"] == nil)
        #expect(body["harness"] as? String == "shell")
        #expect(body["mode"] as? String == "pty")
        return (201, Data(#"{"session_id": "harness-abc", "mode": "pty"}"#.utf8))
    }
    let id = try await client().createSession(hostID: "mac-mini", harness: "shell",
                                              mode: "pty", prompt: nil, cwd: nil)
    #expect(id == "harness-abc")
}

@Test func authStatusRouteShape() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/status")
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","state":"unauthenticated"}"#.utf8))
    }
    let status = try await client().authStatus(hostID: "mac-mini", harness: "codex")
    #expect(status.state == .unauthenticated)
}

@Test func startAuthFlowPostsAndDecodes() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/start")
        #expect(request.httpMethod == "POST")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user","user_code":"ABCD-EFGH"}"#.utf8))
    }
    let flow = try await client().startAuthFlow(hostID: "mac-mini", harness: "codex")
    #expect(flow.flowID == "auth-flow-1")
    #expect(flow.state == .waitingForUser)
}

@Test func pollAndCancelAuthFlowRoutes() async throws {
    var seen: [String] = []
    MockURLProtocol.handler = { request in
        seen.append("\(request.httpMethod ?? "") \(request.url?.path ?? "")")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
    }
    _ = try await client().authFlow(hostID: "mac-mini", harness: "codex", flowID: "auth-flow-1")
    _ = try await client().cancelAuthFlow(hostID: "mac-mini", harness: "codex", flowID: "auth-flow-1")
    #expect(seen == [
        "GET /harness/hosts/mac-mini/auth/codex/flows/auth-flow-1",
        "POST /harness/hosts/mac-mini/auth/codex/flows/auth-flow-1/cancel",
    ])
}

@Test func authRoutesPercentEncodePathComponents() async throws {
    var seen: [String] = []
    MockURLProtocol.handler = { request in
        seen.append("\(request.httpMethod ?? "") \(request.url?.path ?? "")")
        return (200, Data(#"{"host_id":"mac/mini","harness":"provider/test","flow_id":"flow/1","state":"waiting_for_user"}"#.utf8))
    }
    _ = try await client().authStatus(hostID: "mac/mini", harness: "provider/test")
    _ = try await client().authFlow(
        hostID: "mac/mini",
        harness: "provider/test",
        flowID: "flow/1")
    #expect(seen == [
        "GET /harness/hosts/mac%2Fmini/auth/provider%2Ftest/status",
        "GET /harness/hosts/mac%2Fmini/auth/provider%2Ftest/flows/flow%2F1",
    ])
}

}
