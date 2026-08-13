import Foundation
import Testing
@testable import DroverKit

extension MockNetworkTests {
@Suite(.serialized)
struct AuthFlowModelTests {

    @Test @MainActor func startLoadsWaitingFlow() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/start")
            #expect(request.httpMethod == "POST")
            #expect(request.bodyStreamData() == Data("{}".utf8))
            #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
            return (202, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user","login_url":"https://example.test","user_code":"ABCD-EFGH"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")

        await model.start()

        #expect(model.flow?.flowID == "auth-flow-1")
        #expect(model.flow?.state == .waitingForUser)
        #expect(model.errorMessage == nil)
    }

    @Test @MainActor func cancelUpdatesTerminalFlow() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/flows/auth-flow-1/cancel")
            #expect(request.httpMethod == "POST")
            #expect(request.bodyStreamData() == Data("{}".utf8))
            #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))

        await model.cancel()

        #expect(model.flow?.state == .cancelled)
    }

    @Test @MainActor func submitCodePostsTheTypedTextAndAppliesTheSnapshot() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.path
                == "/harness/hosts/mac-mini/auth/claude-code/flows/auth-flow-1/input")
            #expect(request.httpMethod == "POST")
            #expect(request.bodyStreamData() == Data(#"{"text":"PASTED-CODE"}"#.utf8))
            return (200, Data(#"{"host_id":"mac-mini","harness":"claude-code","flow_id":"auth-flow-1","state":"waiting_for_user","supports_input":true}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "claude-code")
        model.flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: Data(#"{"host_id":"mac-mini","harness":"claude-code","flow_id":"auth-flow-1","state":"waiting_for_user","supports_input":true}"#.utf8))
        model.codeEntry = "  PASTED-CODE\n"

        await model.submitCode()

        #expect(model.flow?.supportsInput == true)
        #expect(model.errorMessage == nil)
        // Cleared so a second paste cannot silently resend the first.
        #expect(model.codeEntry.isEmpty)
    }

    @Test @MainActor func submitCodeIgnoresBlankEntryWithoutCallingTheHost() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            state.incrementRequests()
            return (200, Data(#"{"host_id":"mac-mini","harness":"claude-code","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "claude-code")
        model.flow = try waitingFlow()
        model.codeEntry = "   "

        await model.submitCode()

        #expect(state.requestCount == 0)
    }

    @Test @MainActor func terminalOnlyHarnessIsReportedWithoutStartingAFlow() async throws {
        // harnessd answers 409 rather than launching a TUI that can only die
        // with "bubbletea: error opening TTY". A host that refuses the start
        // is telling us the mode changed, so the model re-reads status
        // instead of leaving the user on a dead error string.
        let state = PollTestState()
        MockURLProtocol.handler = { request in
            if request.url?.path.hasSuffix("/start") == true {
                state.incrementRequests()
                return (409, Data(#"{"error":"agy can only be signed in from a terminal session","harness":"agy","sign_in":"terminal"}"#.utf8))
            }
            return (200, Data(#"{"host_id":"nas","harness":"agy","state":"unknown","sign_in":"terminal"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "nas", harness: "agy")

        await model.start()

        #expect(state.requestCount == 1)
        #expect(model.requiresTerminalSignIn)
        #expect(model.flow == nil)
    }

    @Test @MainActor func statusMarksTerminalOnlyHarnessesBeforeAnyStart() async throws {
        MockURLProtocol.handler = { _ in
            (200, Data(#"{"host_id":"nas","harness":"agy","state":"unknown","sign_in":"terminal"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "nas", harness: "agy")

        await model.refreshStatus()

        #expect(model.requiresTerminalSignIn)
    }

    @Test @MainActor func pollingAppliesTerminalFlowThenStops() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            state.incrementRequests()
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"authenticated"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 0.01)

        try await waitUntil { model.flow?.state == .authenticated }
        try await Task.sleep(for: .milliseconds(50))
        #expect(state.requestCount == 1)
    }

    @Test @MainActor func pollingPermanentRequestErrorStopsPolling() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            state.incrementRequests()
            return (400, Data(#"{"error":"poll failed"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 0.01)

        try await waitUntil { model.errorMessage != nil }
        try await Task.sleep(for: .milliseconds(50))
        #expect(state.requestCount == 1)
    }

    @Test @MainActor func pollingUnhandledClientErrorStopsPolling() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            state.incrementRequests()
            return (422, Data(#"{"error":"poll rejected"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 0.01)

        try await waitUntil { model.errorMessage != nil }
        try await Task.sleep(for: .milliseconds(50))
        #expect(state.requestCount == 1)
        #expect(model.errorMessage == "poll rejected")
    }

    @Test @MainActor func pollingRetriesTransientErrorsAndRecovers() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            if state.incrementRequests() < 3 {
                return (500, Data(#"{"error":"poll failed"}"#.utf8))
            }
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"authenticated"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 0.01)

        try await waitUntil { model.flow?.state == .authenticated }
        #expect(state.requestCount == 3)
        #expect(model.errorMessage == nil)
    }

    @Test @MainActor func failedCancellationResumesPolling() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { request in
            state.incrementRequests()
            if request.url?.path.hasSuffix("/cancel") == true {
                return (500, Data(#"{"error":"cancel failed"}"#.utf8))
            }
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"authenticated"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        await model.cancel()

        try await waitUntil { model.flow?.state == .authenticated }
        #expect(state.requestCount == 2)
        #expect(model.errorMessage == nil)
    }

    @Test @MainActor func stoppedPollingIgnoresAnInFlightPollResponse() async throws {
        let releasePoll = DispatchSemaphore(value: 0)
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            state.markStarted()
            _ = releasePoll.wait(timeout: .now() + 1)
            state.markReturned()
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"authenticated"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 60)
        try await waitUntil { state.didStart }

        model.stopPolling()
        releasePoll.signal()

        try await waitUntil { state.didReturn }
        try await Task.sleep(for: .milliseconds(50))
        #expect(model.flow?.state == .waitingForUser)
    }

    @Test @MainActor func cancelledFlowIgnoresAnInFlightPollResponse() async throws {
        let releasePoll = DispatchSemaphore(value: 0)
        let state = PollTestState()
        MockURLProtocol.handler = { request in
            if request.url?.path.hasSuffix("/cancel") == true {
                return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
            }

            state.markStarted()
            _ = releasePoll.wait(timeout: .now() + 1)
            state.markReturned()
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 60)
        try await waitUntil { state.didStart }

        let cancelTask = Task { await model.cancel() }
        try await Task.sleep(for: .milliseconds(50))

        releasePoll.signal()
        await cancelTask.value
        #expect(model.flow?.state == .cancelled)

        try await waitUntil { state.didReturn }
        try await Task.sleep(for: .milliseconds(50))
        #expect(model.flow?.state == .cancelled)
    }

    @MainActor private func waitingFlow() throws -> HarnessAuthFlow {
        try JSONDecoder().decode(HarnessAuthFlow.self, from: Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))
    }

    @MainActor private func waitUntil(
        timeout: Duration = .seconds(1), _ condition: @MainActor @Sendable () -> Bool
    ) async throws {
        let deadline = ContinuousClock.now + timeout
        while !condition() {
            guard ContinuousClock.now < deadline else { throw PollTimeoutError() }
            try await Task.sleep(for: .milliseconds(10))
        }
    }

    private struct PollTimeoutError: Error {}

    private final class PollTestState: @unchecked Sendable {
        private let lock = NSLock()
        private var requests = 0
        private var started = false
        private var returned = false

        var requestCount: Int { withLock { requests } }
        var didStart: Bool { withLock { started } }
        var didReturn: Bool { withLock { returned } }

        @discardableResult
        func incrementRequests() -> Int { withLock { requests += 1; return requests } }
        func markStarted() { withLock { started = true } }
        func markReturned() { withLock { returned = true } }

        private func withLock<T>(_ operation: () -> T) -> T {
            lock.lock()
            defer { lock.unlock() }
            return operation()
        }
    }
}

}  // extension MockNetworkTests
