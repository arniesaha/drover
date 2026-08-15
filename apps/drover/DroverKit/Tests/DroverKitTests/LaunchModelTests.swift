import Foundation
import Testing
@testable import DroverKit

/// Thread-safe: `MockURLProtocol.handler` runs off the main actor.
private final class SnapshotRequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func bump() { lock.lock(); count += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}

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

// MARK: - Snapshot loading

/// The sheet can open before the fleet snapshot exists (deep link, cold
/// start). `init` then has nothing to derive a host from, so the fetch has to
/// do it — otherwise `hostID` stays empty, Launch stays disabled, and the
/// feature cannot start a session at all.
@Test @MainActor func aLateSnapshotDerivesTheDefaultsInitCouldNot() async throws {
    let model = LaunchModel(client: client(), snapshot: nil, store: testStore())
    #expect(model.hostID.isEmpty)

    MockURLProtocol.handler = { request in
        #expect(request.url?.path == "/harness")
        return (200, snapshotJSON)
    }

    await model.refreshSnapshot()

    #expect(model.hostID == "mac-mini")
    #expect(model.harness == "claude-code")
    #expect(model.availableHosts.map(\.id) == ["mac-mini"])
    #expect(model.cwdSuggestions == ["/Users/arnabmac/jenny/nexus", "/Volumes/M2 1/drover"])
    #expect(model.runPreferences.hostID == "mac-mini")
    #expect(model.runPreferences.harness == "claude-code")
    #expect(model.snapshotError == nil)
    #expect(model.isFetchingSnapshot == false)
}

/// The flip side: a refresh is not allowed to move a selection the user made
/// and the new snapshot still offers.
@Test @MainActor func aRefreshKeepsAHostTheSnapshotStillOffers() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    model.hostID = "nas"

    MockURLProtocol.handler = { _ in (200, multiHostSnapshotJSON) }
    await model.refreshSnapshot()

    #expect(model.hostID == "nas")
    #expect(model.harness == "codex")
}

/// When the selected host is gone from the new snapshot the selection is
/// unusable for the same reason an empty one is, so it falls back.
@Test @MainActor func aRefreshThatDropsTheSelectedHostFallsBack() async throws {
    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())
    model.hostID = "studio"

    // `snapshotJSON` lists mac-mini only.
    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    await model.refreshSnapshot()

    #expect(model.hostID == "mac-mini")
    #expect(model.harness == "claude-code")
}

/// A snapshot in hand is authoritative, an empty suggestion list included —
/// that is a fresh install with no recent sessions, not a stale read. Asking
/// again cannot change it, and asking again on every answer turned the sheet
/// into a request storm with Launch pinned disabled.
@Test @MainActor func anEmptySuggestionListNeverStormsTheServer() async throws {
    let counter = SnapshotRequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        return (200, multiHostSnapshotJSON)
    }

    let snapshot = try HarnessSnapshot.decode(from: multiHostSnapshotJSON)
    #expect(snapshot.cwdSuggestions.isEmpty)
    let model = LaunchModel(client: client(), snapshot: snapshot, store: testStore())

    await model.loadSnapshotIfNeeded()
    // Host and harness changes re-filter the suggestions already in hand;
    // neither is a reason to re-download the fleet.
    model.hostID = "nas"
    model.harness = "shell"
    try await Task.sleep(for: .milliseconds(50))

    #expect(counter.value == 0)
    #expect(model.isFetchingSnapshot == false)
}

/// Two callers overlapping must not each raise and lower the same spinner —
/// the first to finish used to clear it while the second was still in flight,
/// re-enabling Launch mid-fetch.
@Test @MainActor func overlappingRefreshesShareOneRequest() async throws {
    let counter = SnapshotRequestCounter()
    MockURLProtocol.handler = { _ in
        counter.bump()
        return (200, snapshotJSON)
    }
    let model = LaunchModel(client: client(), snapshot: nil, store: testStore())

    async let first: Void = model.refreshSnapshot()
    async let second: Void = model.loadSnapshotIfNeeded()
    _ = await (first, second)

    #expect(counter.value == 1)
    #expect(model.isFetchingSnapshot == false)
    #expect(model.hostID == "mac-mini")
}

/// A swallowed failure leaves the sheet with no hosts, no suggestions and
/// nothing said. `launchError` never covered this — only `launch()` sets it.
@Test @MainActor func aFailedFetchSaysWhyInsteadOfGoingQuiet() async throws {
    let model = LaunchModel(client: client(), snapshot: nil, store: testStore())
    MockURLProtocol.handler = { _ in (401, Data()) }

    await model.refreshSnapshot()

    #expect(model.snapshotError == "token rejected — check Settings")
    #expect(model.launchError == nil)
    #expect(model.snapshot == nil)
    #expect(model.isFetchingSnapshot == false)
}

/// A retry that works clears the message it replaced.
@Test @MainActor func aSucceedingRetryClearsTheError() async throws {
    let model = LaunchModel(client: client(), snapshot: nil, store: testStore())
    MockURLProtocol.handler = { _ in (401, Data()) }
    await model.refreshSnapshot()
    #expect(model.snapshotError != nil)

    MockURLProtocol.handler = { _ in (200, snapshotJSON) }
    await model.refreshSnapshot()

    #expect(model.snapshotError == nil)
    #expect(model.hostID == "mac-mini")
}

}

}  // extension MockNetworkTests
