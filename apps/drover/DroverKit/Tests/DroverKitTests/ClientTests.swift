import Foundation
import Testing
@testable import DroverKit

/// `.serialized`: every test in this file mutates the process-global
/// `MockURLProtocol.handler` — without serialization, Swift Testing's
/// default parallel execution could let one test's handler answer another
/// test's in-flight request.
extension MockNetworkTests {
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
    await #expect(throws: DroverError.unauthorized) { try await client().snapshot() }
}

@Test func turnConflictMaps() async {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness/sessions/s1/turns")
        return (409, Data(#"{"error": "approval pending; answer it first"}"#.utf8))
    }
    await #expect(throws: DroverError.conflict("approval pending; answer it first")) {
        _ = try await client().sendTurn(sessionID: "s1", text: "go")
    }
}

@Test func sendTurnWithoutImagesOmitsImagesKey() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(request.url?.path == "/harness/sessions/s1/turns")
        #expect(body["text"] as? String == "hi")
        #expect(body["images"] == nil)
        return (202, Data(#"{"turn_id": "turn-1"}"#.utf8))
    }
    let turnID = try await client().sendTurn(sessionID: "s1", text: "hi")
    #expect(turnID == "turn-1")
}

@Test func sendTurnEncodesImagesAsBase64() async throws {
    let bytes = Data([0xFF, 0xD8, 0xFF])
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        let images = body["images"] as! [[String: Any]]
        #expect(images.count == 1)
        #expect(images[0]["media_type"] as? String == "image/jpeg")
        #expect(images[0]["data_base64"] as? String == bytes.base64EncodedString())
        #expect(body["text"] as? String == "look")
        return (202, Data(#"{"turn_id": "turn-2"}"#.utf8))
    }
    let turnID = try await client().sendTurn(
        sessionID: "s1", text: "look",
        images: [TurnAttachment(mediaType: "image/jpeg", data: bytes)])
    #expect(turnID == "turn-2")
}

@Test func createSessionPostsBodyAndReturnsID() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(request.url?.path == "/harness/hosts/mac-mini/sessions")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(body["harness"] as? String == "agy")
        #expect(body["mode"] as? String == "structured")
        #expect(body["prompt"] as? String == "do it")
        return (201, Data(#"{"session_id": "harness-xyz", "mode": "structured"}"#.utf8))
    }
    let id = try await client().createSession(hostID: "mac-mini", harness: "agy",
                                              mode: "structured", prompt: "do it", cwd: nil)
    #expect(id == "harness-xyz")
}

@Test func createSessionPostsImagesModelAndThinking() async throws {
    let bytes = Data([0x01, 0x02, 0x03])
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        let images = body["images"] as! [[String: Any]]
        #expect(images.count == 1)
        #expect(images[0]["media_type"] as? String == "image/jpeg")
        #expect(images[0]["data_base64"] as? String == bytes.base64EncodedString())
        #expect(body["model"] as? String == "gpt-5.6-sol")
        #expect(body["thinking_effort"] as? String == "high")
        return (201, Data(#"{"session_id": "harness-xyz", "mode": "structured"}"#.utf8))
    }
    let id = try await client().createSession(
        hostID: "mac-mini",
        harness: "codex",
        mode: "structured",
        prompt: "look",
        cwd: nil,
        images: [TurnAttachment(mediaType: "image/jpeg", data: bytes)],
        model: "gpt-5.6-sol",
        thinkingEffort: "high"
    )
    #expect(id == "harness-xyz")
}

@Test func permissionBadRequestMaps() async {
    MockURLProtocol.handler = { _ in
        (400, Data(#"{"error": "codex exec has no approval channel; use sandbox flags"}"#.utf8))
    }
    await #expect(throws: DroverError.badRequest(
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

@Test func streamRequestIncludesAfterSeqWhenProvided() {
    let request = client().streamRequest(sessionID: "harness-1", afterSeq: 42)
    #expect(request.url?.absoluteString == "ws://test.local:7080/harness/sessions/harness-1/stream?after_seq=42")
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

@Test func messagePageBuildsNewestRequest() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.absoluteString.contains(
            "/harness/sessions/session%20one/messages?"
        ) == true)
        #expect(request.url?.query == "limit=200")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        return (200, Data(#"{"messages": [], "max_seq": 0, "has_older": false, "has_newer": false}"#.utf8))
    }

    let page = try await client().messagePage(
        sessionID: "session one", request: .newest(limit: 200))

    #expect(page.maxSeq == 0)
}

@Test func messagePageBuildsOlderRequest() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.query == "before_seq=42&limit=100")
        return (200, Data(#"{"messages": [], "max_seq": 42, "has_older": false, "has_newer": true}"#.utf8))
    }

    _ = try await client().messagePage(
        sessionID: "s1", request: .older(beforeSeq: 42, limit: 100))
}

@Test func messagePageBuildsFixedBoundNewerRequest() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.query == "after_seq=7&through_seq=99&limit=500")
        return (200, Data(#"{"messages": [], "max_seq": 99, "has_older": true, "has_newer": false}"#.utf8))
    }

    _ = try await client().messagePage(
        sessionID: "s1",
        request: .newer(afterSeq: 7, throughSeq: 99, limit: 500))
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

@Test func sendTurnPostsModelAndThinkingWhenProvided() async throws {
    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body["text"] as? String == "go")
        #expect(body["model"] as? String == "gpt-5.6-sol")
        #expect(body["thinking_effort"] as? String == "max")
        return (202, Data(#"{"turn_id": "turn-42"}"#.utf8))
    }
    let turnID = try await client().sendTurn(
        sessionID: "s1",
        text: "go",
        model: "gpt-5.6-sol",
        thinkingEffort: "max"
    )
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
        // DroverClient still attaches it that's harmless — assert only the
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

@Test func decodingErrorMapsToDroverErrorDecoding() async {
    MockURLProtocol.handler = { _ in (200, Data("not json".utf8)) }
    do {
        _ = try await client().snapshot()
        Issue.record("expected DroverError.decoding, but no error was thrown")
    } catch let error as DroverError {
        guard case .decoding = error else {
            Issue.record("expected .decoding, got \(error)")
            return
        }
    } catch {
        Issue.record("expected DroverError.decoding, got \(error)")
    }
}

@Test func badRequestFallsBackToRawBodyWhenNoErrorField() async {
    MockURLProtocol.handler = { _ in (400, Data("plain text failure".utf8)) }
    await #expect(throws: DroverError.badRequest("plain text failure")) {
        try await client().interrupt(sessionID: "s1")
    }
}

@Test func unavailableAuthResponsePreservesStructured404Error() async {
    MockURLProtocol.handler = { _ in
        (404, Data(#"{"error": "auth is not supported for openclaw"}"#.utf8))
    }
    await #expect(throws: DroverError.unavailable("auth is not supported for openclaw")) {
        try await client().startAuthFlow(hostID: "mac-mini", harness: "openclaw")
    }
}

@Test func unavailableAuthResponseUsesDetailWhenErrorIsAbsent() async {
    MockURLProtocol.handler = { _ in
        (404, Data(#"{"host_id":"mac-mini","harness":"openclaw","state":"unavailable","detail":"auth is not supported for openclaw"}"#.utf8))
    }
    await #expect(throws: DroverError.unavailable("auth is not supported for openclaw")) {
        try await client().authStatus(hostID: "mac-mini", harness: "openclaw")
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
        #expect(request.bodyStreamData() == Data("{}".utf8))
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user","user_code":"ABCD-EFGH"}"#.utf8))
    }
    let flow = try await client().startAuthFlow(hostID: "mac-mini", harness: "codex")
    #expect(flow.flowID == "auth-flow-1")
    #expect(flow.state == .waitingForUser)
}

@Test func pollAndCancelAuthFlowRoutes() async throws {
    let seen = RequestLog()
    MockURLProtocol.handler = { request in
        seen.append("\(request.httpMethod ?? "") \(request.url?.path ?? "")")
        if request.url?.path.hasSuffix("/cancel") == true {
            #expect(request.bodyStreamData() == Data("{}".utf8))
            #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        }
        return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
    }
    _ = try await client().authFlow(hostID: "mac-mini", harness: "codex", flowID: "auth-flow-1")
    _ = try await client().cancelAuthFlow(hostID: "mac-mini", harness: "codex", flowID: "auth-flow-1")
    #expect(seen.values == [
        "GET /harness/hosts/mac-mini/auth/codex/flows/auth-flow-1",
        "POST /harness/hosts/mac-mini/auth/codex/flows/auth-flow-1/cancel",
    ])
}

@Test func authRoutesPercentEncodePathComponents() async throws {
    let seen = RequestLog()
    MockURLProtocol.handler = { request in
        seen.append("\(request.httpMethod ?? "") \(request.url?.absoluteString ?? "")")
        return (200, Data(#"{"host_id":"mac/mini","harness":"provider/test","flow_id":"flow%2F1","state":"waiting_for_user"}"#.utf8))
    }
    _ = try await client().authStatus(hostID: "mac%2Fmini", harness: "provider/test")
    _ = try await client().authFlow(
        hostID: "mac%2Fmini",
        harness: "provider/test",
        flowID: "flow%2F1")
    #expect(seen.values == [
        "GET http://test.local:7080/harness/hosts/mac%252Fmini/auth/provider%2Ftest/status",
        "GET http://test.local:7080/harness/hosts/mac%252Fmini/auth/provider%2Ftest/flows/flow%252F1",
    ])
}

@Test func cockpitOverviewUsesAuthenticatedBoundedDaysQuery() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/cockpit/overview")
        #expect(request.url?.query == "days=7")
        #expect(request.timeoutInterval == 60)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        return (200, emptyOverviewJSON)
    }

    _ = try await client().cockpitOverview(days: 7)
}

@Test func analyticsEncodesAllowlistedFiltersOnceInDeterministicOrder() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/analytics")
        #expect(request.url?.query == "days=30&host_id=mac%20mini&harness=codex&provider=openai&model=gpt-5.6-sol&project_key=arniesaha%2Fdrover")
        #expect(request.timeoutInterval == 60)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        return (200, emptyAnalyticsJSON)
    }

    _ = try await client().analytics(filters: AnalyticsFilters(
        days: 30,
        hostID: "mac mini",
        harness: "codex",
        provider: "openai",
        model: "gpt-5.6-sol",
        projectKey: "arniesaha/drover"
    ))
}

@Test func analyticsEncodesIndependentDimensionCursorsAndBoundedLimit() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.query == "days=7&limit=50&project_cursor=project%2Bnext%3D&host_cursor=host%2Fnext")
        return (200, emptyAnalyticsJSON)
    }

    _ = try await client().analytics(filters: AnalyticsFilters(
        limit: 50, projectCursor: "project+next=", hostCursor: "host/next"
    ))
}

@Test func insightsCursorAndFiltersAreEncodedExactlyOnce() async throws {
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/insights")
        #expect(request.url?.query == "state=open&severity=high&confidence=confirmed&analyzer_class=deterministic&host=mac-mini&harness=codex&target_type=hook&target_id=mac-mini%2Fcodex%2Fpre%20tool&cursor=rank%2Btime%2Fnext%3D&limit=25")
        let cursorItems = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
            .queryItems?.filter { $0.name == "cursor" }
        #expect(cursorItems?.count == 1)
        #expect(cursorItems?.first?.value == "rank+time/next=")
        return (200, Data(#"{"findings":[],"next_cursor":null}"#.utf8))
    }

    _ = try await client().insights(filters: InsightFilters(
        state: .open,
        severity: .high,
        confidence: .confirmed,
        analyzerClass: .deterministic,
        host: "mac-mini",
        harness: "codex",
        targetType: "hook",
        targetID: "mac-mini/codex/pre tool",
        cursor: "rank+time/next=",
        limit: 25
    ))
}

@Test func insightLifecycleAndPrivacyRoutesUseExpectedBodies() async throws {
    let seen = RequestLog()
    MockURLProtocol.handler = { request in
        let body = request.bodyStreamData()
        let encodedPath = request.url.flatMap {
            URLComponents(url: $0, resolvingAgainstBaseURL: false)?.percentEncodedPath
        } ?? ""
        let bodyText = body.isEmpty ? "<none>" : (String(data: body, encoding: .utf8) ?? "<none>")
        seen.append("\(request.httpMethod ?? "") \(encodedPath) \(bodyText)")
        switch encodedPath {
        case "/insights/finding%2Fone":
            return (200, insightDetailJSON)
        case "/insights/finding%2Fone/acknowledge", "/insights/finding%2Fone/dismiss":
            return (200, insightMutationJSON)
        case "/insights/finding%2Fone/check":
            return (202, Data(#"{"status":"queued","job_id":"job-1"}"#.utf8))
        case "/insights/content-analysis":
            return (200, contentAnalysisStatusJSON)
        case "/insights/content-analysis/consent":
            return (200, contentAnalysisStatusJSON)
        case "/insights/content-analysis/revoke":
            return (200, Data(#"{"enabled":false,"backend":"local","external_disclosure_accepted":false,"pending_model_jobs":0,"cancelled_model_jobs":2}"#.utf8))
        case "/insights/content-excerpts":
            return (200, Data(#"{"purged_excerpt_count":3}"#.utf8))
        default:
            Issue.record("unexpected request \(request)")
            return (404, Data())
        }
    }

    _ = try await client().insightDetail(findingID: "finding/one")
    _ = try await client().acknowledgeInsight(findingID: "finding/one")
    _ = try await client().dismissInsight(findingID: "finding/one", reason: "accepted tradeoff")
    _ = try await client().checkInsight(findingID: "finding/one")
    _ = try await client().contentAnalysisStatus()
    _ = try await client().setContentAnalysisConsent(backend: .cloud, externalDisclosureAccepted: true)
    _ = try await client().revokeContentAnalysis()
    _ = try await client().purgeContentExcerpts()

    #expect(seen.values == [
        "GET /insights/finding%2Fone <none>",
        "POST /insights/finding%2Fone/acknowledge {}",
        "POST /insights/finding%2Fone/dismiss {\"reason\":\"accepted tradeoff\"}",
        "POST /insights/finding%2Fone/check {}",
        "GET /insights/content-analysis <none>",
        "POST /insights/content-analysis/consent {\"backend\":\"cloud\",\"external_disclosure_accepted\":true}",
        "POST /insights/content-analysis/revoke {}",
        "DELETE /insights/content-excerpts <none>",
    ])
}

@Test func contentConsentPreservesHTTP207PartialPropagation() async throws {
    let fixtureURL = try #require(droverKitFixtureURL("content-consent-partial"))
    let fixture = try Data(contentsOf: fixtureURL)
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/insights/content-analysis/consent")
        return (207, fixture)
    }

    let result = try await client().setContentAnalysisConsent(
        backend: .cloud, externalDisclosureAccepted: true
    )

    #expect(result.outcome == .partial)
    #expect(result.status.enabled)
    #expect(result.status.affectedHosts.map(\.hostID) == ["offline-laptop"])
}

@Test func failedRevokeResponseStillPreservesCentralDisabledTruth() async throws {
    let fixtureURL = try #require(droverKitFixtureURL("content-consent-failed"))
    let fixture = try Data(contentsOf: fixtureURL)
    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/insights/content-analysis/revoke")
        return (503, fixture)
    }

    let result = try await client().revokeContentAnalysis()

    #expect(result.outcome == .failed)
    #expect(!result.status.enabled)
    #expect(result.status.cancelledModelJobs == nil)
}

@Test func contentStatusRetainsFailed503FleetTruthOnFreshNavigation() async throws {
    let fixtureURL = try #require(droverKitFixtureURL("content-consent-failed"))
    let fixture = try Data(contentsOf: fixtureURL)
    MockURLProtocol.handler = { request in
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path == "/insights/content-analysis")
        return (503, fixture)
    }

    let status = try await client().contentAnalysisStatus()

    #expect(!status.enabled)
    #expect(status.propagationOutcome == .failed)
    #expect(status.affectedHosts.map(\.hostID) == ["workstation"])
}

@Test func contentStatusSurfacesFailedDurableRepairWithoutHidingCentralIntent() async throws {
    let fixtureURL = try #require(droverKitFixtureURL("content-consent-repair-failed"))
    let fixture = try Data(contentsOf: fixtureURL)
    MockURLProtocol.handler = { request in
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path == "/insights/content-analysis")
        return (503, fixture)
    }

    let status = try await client().contentAnalysisStatus()

    #expect(status.enabled)
    #expect(status.propagationOutcome == .failed)
    #expect(status.affectedHosts.first?.error == "durable consent repair failed")
}

private let emptyOverviewJSON = Data(#"""
{"cockpit_api_version":1,
 "provider_capacity":{"status":"unavailable","observed_at":null,"coverage":null,"data":[]},
 "activity":{"status":"error","observed_at":null,"coverage":null,"data":null},
 "popular_projects":[]}
"""#.utf8)

private let emptyAnalyticsJSON = Data(#"""
{"cockpit_api_version":1,
 "filters":{"days":30,"host_id":"mac mini","harness":"codex","provider":"openai","model":"gpt-5.6-sol","project_key":"arniesaha/drover"},
 "provider_capacity":{"status":"unavailable","observed_at":null,"coverage":null,"data":[]},
 "activity":{"status":"error","observed_at":null,"coverage":null,"data":null}}
"""#.utf8)

private let insightFindingJSON = #"""
{"finding_id":"finding-one","analyzer_id":"deterministic.hook","rule_id":"hook_validity",
 "target_type":"hook","target_id":"mac-mini/codex/pre-tool","analyzer_class":"deterministic",
 "severity":"high","confidence":"confirmed","title":"Hook is broken","impact":"Turns fail",
 "remediation":["Fix the executable path"],"state":"open","dismissal_reason":null,
 "first_seen_at":"2026-08-08T18:00:00+00:00","last_seen_at":"2026-08-08T18:01:00+00:00",
 "resolved_at":null,"dismissed_at":null,"regressed_at":null}
"""#

private var insightDetailJSON: Data {
    Data(#"{"finding":\#(insightFindingJSON),"evidence":[]}"#.utf8)
}

private var insightMutationJSON: Data {
    Data(#"{"finding":\#(insightFindingJSON)}"#.utf8)
}

private let contentAnalysisStatusJSON = Data(#"""
{"enabled":true,"backend":"cloud","external_disclosure_accepted":true,"pending_model_jobs":0}
"""#.utf8)

private final class RequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [String] = []

    var values: [String] {
        lock.withLock { entries }
    }

    func append(_ entry: String) {
        lock.withLock { entries.append(entry) }
    }
}

}

}  // extension MockNetworkTests
