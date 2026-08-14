import Foundation
import Testing
@testable import DroverKit

/// `.serialized`: several tests here mutate the process-global
/// `MockURLProtocol.handler` — see `ClientTests`' doc comment.
extension MockNetworkTests {
@Suite(.serialized)
struct LaunchModelTests {

private func testStore() -> HarnessModelCatalogStore {
    HarnessModelCatalogStore(
        defaults: UserDefaults(suiteName: "launch-model-test-\(UUID().uuidString)")!
    )
}

private func catalog(
    hostID: String = "mac-mini",
    harness: String = "codex",
    modelID: String = "gpt-5.6-terra",
    effort: String = "high"
) -> HarnessModelCatalog {
    HarnessModelCatalog(
        schemaVersion: 1,
        hostID: hostID,
        harness: harness,
        accountScopeID: "scope-\(hostID)-\(harness)",
        harnessVersion: nil,
        discoveredAt: nil,
        stale: false,
        staleReason: nil,
        models: [HarnessModelOption(
            id: modelID,
            displayName: modelID,
            description: nil,
            isDefault: false,
            reasoning: HarnessReasoningOptions(supported: [effort], default: effort)
        )]
    )
}

@Test @MainActor func defaultsPickFirstOnlineHostAndClaudeCode() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())

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
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())

    // Fixture host declares ["shell", "claude-code", "agy"] in that
    // order — structured-capable ones should surface first.
    #expect(model.availableHarnesses == ["claude-code", "agy", "shell"])
}

@Test @MainActor func shellHarnessIsNotStructured() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    model.harness = "shell"
    #expect(model.isStructured == false)
}

@Test @MainActor func interactiveAuthIsLimitedToSupportedProviders() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())

    #expect(model.supportsInteractiveAuth == true)
    model.harness = "agy"
    #expect(model.supportsInteractiveAuth == true)
    model.harness = "shell"
    #expect(model.supportsInteractiveAuth == false)
}

@Test @MainActor func noSnapshotYieldsEmptyDefaults() async throws {
    let model = LaunchModel(client: client(), snapshot: nil, store: testStore())
    #expect(model.hostID == "")
    #expect(model.harness == "")
    #expect(model.availableHosts.isEmpty)
    #expect(model.cwdSuggestions.isEmpty)
}

@Test @MainActor func switchingHostResetsHarnessWhenSelectionInvalid() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    #expect(model.hostID == "mac-mini")
    #expect(model.harness == "claude-code")

    // "nas" offers only ["shell", "codex"] — claude-code is invalid there,
    // so the harness must reset to the new host's default (structured-first
    // ordering picks "codex" over "shell").
    model.hostID = "nas"
    #expect(model.harness == "codex")
    #expect(model.availableHarnesses == ["codex", "shell"])
    #expect(model.runPreferences.hostID == "nas")
    #expect(model.runPreferences.harness == "codex")
}

@Test @MainActor func switchingHostPreservesStillValidHarness() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    model.harness = "agy" // deliberate non-default pick on mac-mini

    // "studio" offers agy too — the user's pick must stay put, not get
    // clobbered back to studio's default ("claude-code").
    model.hostID = "studio"
    #expect(model.harness == "agy")
    #expect(model.runPreferences.hostID == "studio")
    #expect(model.runPreferences.harness == "agy")
}

@Test @MainActor func switchingHostAndHarnessRestoresOnlyThatPairsPreference() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())

    model.hostID = "nas"
    model.runPreferences.apply(catalog(hostID: "nas", modelID: "nas-model"))
    model.runPreferences.selectedModel = "nas-model"
    model.runPreferences.thinkingEffort = "high"

    model.hostID = "mac-mini"
    model.harness = "agy"
    model.runPreferences.apply(catalog(harness: "agy", modelID: "agy-model"))
    model.runPreferences.selectedModel = "agy-model"
    model.hostID = "nas"

    #expect(model.runPreferences.hostID == "nas")
    #expect(model.runPreferences.harness == "codex")
    #expect(model.runPreferences.selectedModel == "nas-model")
    #expect(model.runPreferences.thinkingEffort == "high")
}

@Test @MainActor func launchPostsStructuredModeWithPromptForAgy() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    model.harness = "agy"
    model.prompt = "explain the repo layout"

    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(request.url?.path == "/harness/hosts/mac-mini/sessions")
        #expect(body["harness"] as? String == "agy")
        #expect(body["mode"] as? String == "structured")
        #expect(body["prompt"] as? String == "explain the repo layout")
        return (201, Data(#"{"session_id": "harness-xyz"}"#.utf8))
    }

    let sessionID = await model.launch()
    #expect(sessionID == "harness-xyz")
    #expect(model.launchError == nil)
}

@Test @MainActor func launchUsesCatalogStateOverrides() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    let attachment = TurnAttachment(mediaType: "image/jpeg", data: Data([0x0A, 0x0B]))
    model.harness = "codex"
    model.runPreferences.apply(catalog())
    model.prompt = "inspect this"
    model.promptAttachments = [attachment]
    model.runPreferences.selectedModel = "gpt-5.6-terra"
    model.runPreferences.thinkingEffort = "high"

    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        let images = body["images"] as! [[String: Any]]
        #expect(body["harness"] as? String == "codex")
        #expect(body["model"] as? String == "gpt-5.6-terra")
        #expect(body["thinking_effort"] as? String == "high")
        #expect(images[0]["data_base64"] as? String == attachment.data.base64EncodedString())
        return (201, Data(#"{"session_id": "harness-pref"}"#.utf8))
    }

    let sessionID = await model.launch()
    #expect(sessionID == "harness-pref")
}

@Test @MainActor func launchOmitsHarnessDefaultAndAutoOverrides() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    model.harness = "codex"
    model.runPreferences.apply(catalog())

    MockURLProtocol.handler = { request in
        let body = try! JSONSerialization.jsonObject(
            with: request.bodyStreamData()) as! [String: Any]
        #expect(body.keys.contains("model") == false)
        #expect(body.keys.contains("thinking_effort") == false)
        return (201, Data(#"{"session_id":"harness-default"}"#.utf8))
    }

    #expect(await model.launch() == "harness-default")
}

@Test @MainActor func launchPostsPtyModeWithNoPromptForShell() async throws {
    let snapshot = try HarnessSnapshot.decode(from: snapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
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
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())

    MockURLProtocol.handler = { _ in
        (400, Data(#"{"error": "host offline"}"#.utf8))
    }

    let sessionID = await model.launch()
    #expect(sessionID == nil)
    #expect(model.launchError == "host offline")
}

}

}  // extension MockNetworkTests
