import Foundation
import Testing
@testable import NexusKit

/// `.serialized`: several tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
@Suite(.serialized)
struct LaunchModelTests {

@Test @MainActor func defaultsPickFirstOnlineHostAndClaudeCode() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)

    #expect(model.hostID == "mac-mini")
    #expect(model.harness == "claude-code")
    #expect(model.availableHosts.map(\.id) == ["mac-mini"])
    // Suggestions are filtered to the selected host (host-agnostic
    // favorites always pass); the fixture's "nas" entry must not leak in.
    #expect(model.cwdSuggestions == ["/Users/arnabmac/jenny/nexus", "/Volumes/M2 1/drover"])
    #expect(model.isStructured == true)
}

@Test @MainActor func availableHarnessesPutsStructuredCapableFirst() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)

    // Fixture host declares ["shell", "claude-code", "gemini"] in that
    // order — structured-capable ones should surface first.
    #expect(model.availableHarnesses == ["claude-code", "gemini", "shell"])
}

@Test @MainActor func shellHarnessIsNotStructured() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)
    model.harness = "shell"
    #expect(model.isStructured == false)
}

@Test @MainActor func noSnapshotYieldsEmptyDefaults() async throws {
    let model = LaunchModel(client: client(), snapshot: nil)
    #expect(model.hostID == "")
    #expect(model.harness == "")
    #expect(model.availableHosts.isEmpty)
    #expect(model.cwdSuggestions.isEmpty)
}

@Test @MainActor func switchingHostResetsHarnessWhenSelectionInvalid() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)
    #expect(model.hostID == "mac-mini")
    #expect(model.harness == "claude-code")

    // "nas" offers only ["shell", "codex"] — claude-code is invalid there,
    // so the harness must reset to the new host's default (structured-first
    // ordering picks "codex" over "shell").
    model.hostID = "nas"
    #expect(model.harness == "codex")
    #expect(model.availableHarnesses == ["codex", "shell"])
}

@Test @MainActor func switchingHostPreservesStillValidHarness() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)
    model.harness = "gemini" // deliberate non-default pick on mac-mini

    // "studio" offers gemini too — the user's pick must stay put, not get
    // clobbered back to studio's default ("claude-code").
    model.hostID = "studio"
    #expect(model.harness == "gemini")
}

@Test @MainActor func launchPostsStructuredModeWithPromptForGemini() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)
    model.harness = "gemini"
    model.prompt = "explain the repo layout"

    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(request.url?.path == "/harness/hosts/mac-mini/sessions")
        #expect(body["harness"] as? String == "gemini")
        #expect(body["mode"] as? String == "structured")
        #expect(body["prompt"] as? String == "explain the repo layout")
        return (201, Data(#"{"session_id": "harness-xyz"}"#.utf8))
    }

    let sessionID = await model.launch()
    #expect(sessionID == "harness-xyz")
    #expect(model.launchError == nil)
}

@Test @MainActor func launchPostsPtyModeWithNoPromptForShell() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)
    model.harness = "shell"
    // Even if the model somehow retained prompt text, shell must omit it —
    // the view never shows the prompt field when `isStructured` is false.
    model.prompt = "should never be sent"

    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body["harness"] as? String == "shell")
        #expect(body["mode"] as? String == "pty")
        #expect(body["prompt"] == nil)
        return (201, Data(#"{"session_id": "harness-abc"}"#.utf8))
    }

    let sessionID = await model.launch()
    #expect(sessionID == "harness-abc")
}

@Test @MainActor func launchFailureSetsLaunchErrorAndReturnsNil() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot)

    MockURLProtocol.handler = { _ in
        (400, Data(#"{"error": "host offline"}"#.utf8))
    }

    let sessionID = await model.launch()
    #expect(sessionID == nil)
    #expect(model.launchError == "host offline")
}

}
