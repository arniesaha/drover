import Foundation
import Testing
@testable import NexusKit

@Suite(.serialized)
struct AuthFlowModelTests {

    @Test @MainActor func startLoadsWaitingFlow() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.path == "/harness/hosts/mac-mini/auth/codex/start")
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
            return (200, Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"cancelled"}"#.utf8))
        }
        let model = AuthFlowModel(client: client(), hostID: "mac-mini", harness: "codex")
        model.flow = try JSONDecoder().decode(HarnessAuthFlow.self, from: Data(#"{"host_id":"mac-mini","harness":"codex","flow_id":"auth-flow-1","state":"waiting_for_user"}"#.utf8))

        await model.cancel()

        #expect(model.flow?.state == .cancelled)
    }
}
