import Foundation
import Testing
@testable import NexusKit

@Suite(.serialized)
struct AuthFlowModelTests {

    @Test @MainActor func startLoadsWaitingFlow() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/start")
            #expect(request.httpMethod == "POST")
            #expect(request.httpBody == Data("{}".utf8))
            #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user","login_url":"https://example.test","user_code":"ABCD-EFGH"}"#.utf8))
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
            #expect(request.httpBody == Data("{}".utf8))
            #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))

        await model.cancel()

        #expect(model.flow?.state == .cancelled)
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

    @Test @MainActor func pollingRequestErrorStopsPolling() async throws {
        let state = PollTestState()
        MockURLProtocol.handler = { _ in
            state.incrementRequests()
            return (500, Data(#"{"error":"poll failed"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try waitingFlow()

        model.startPolling(every: 0.01)

        try await waitUntil { model.errorMessage != nil }
        try await Task.sleep(for: .milliseconds(50))
        #expect(state.requestCount == 1)
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

        func incrementRequests() { withLock { requests += 1 } }
        func markStarted() { withLock { started = true } }
        func markReturned() { withLock { returned = true } }

        private func withLock<T>(_ operation: () -> T) -> T {
            lock.lock()
            defer { lock.unlock() }
            return operation()
        }
    }
}
