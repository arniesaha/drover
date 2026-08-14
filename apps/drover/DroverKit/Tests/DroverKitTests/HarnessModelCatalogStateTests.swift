import Foundation
import Testing
@testable import DroverKit

@Suite
struct HarnessModelCatalogPersistenceTests {
    @Test func catalogsAndSelectionsRoundTripByExactPair() throws {
        let defaults = catalogDefaults()
        let store = HarnessModelCatalogStore(defaults: defaults)
        store.save(catalog: fixtureCatalog(scope: "scope-a", model: "terra"))
        store.save(catalog: fixtureCatalog(
            hostID: "nas", scope: "scope-nas", model: "nas-model"
        ))
        store.save(selection: HarnessModelSelection(
            accountScopeID: "scope-a", model: "terra", thinkingEffort: "high"
        ), hostID: "mac-mini", harness: "codex")
        store.save(selection: HarnessModelSelection(
            accountScopeID: "scope-nas", model: "nas-model", thinkingEffort: "low"
        ), hostID: "nas", harness: "codex")
        store.save(selection: HarnessModelSelection(
            accountScopeID: "scope-agy", model: "agy-model", thinkingEffort: ""
        ), hostID: "mac-mini", harness: "agy")

        let reloaded = HarnessModelCatalogStore(defaults: defaults)

        #expect(reloaded.catalog(hostID: "mac-mini", harness: "codex")?.models[0].id
            == "terra")
        #expect(reloaded.catalog(hostID: "nas", harness: "codex")?.models[0].id
            == "nas-model")
        #expect(reloaded.selection(hostID: "mac-mini", harness: "codex")?.model
            == "terra")
        #expect(reloaded.selection(hostID: "nas", harness: "codex")?.model
            == "nas-model")
        #expect(reloaded.selection(hostID: "mac-mini", harness: "agy")?.model
            == "agy-model")
    }

    @Test func clearingOneSelectionDoesNotAffectAnotherPair() {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        store.save(selection: HarnessModelSelection(
            accountScopeID: "scope-a", model: "terra", thinkingEffort: "high"
        ), hostID: "mac-mini", harness: "codex")
        store.save(selection: HarnessModelSelection(
            accountScopeID: "scope-nas", model: "nas-model", thinkingEffort: "low"
        ), hostID: "nas", harness: "codex")

        store.clearSelection(hostID: "mac-mini", harness: "codex")

        #expect(store.selection(hostID: "mac-mini", harness: "codex") == nil)
        #expect(store.selection(hostID: "nas", harness: "codex")?.model == "nas-model")
    }

    @Test func corruptEnvelopeIsDiscarded() {
        let defaults = catalogDefaults()
        defaults.set(Data("not-json".utf8), forKey: "drover.model-catalog-store.v1")

        let store = HarnessModelCatalogStore(defaults: defaults)

        #expect(store.catalog(hostID: "mac-mini", harness: "codex") == nil)
        #expect(defaults.data(forKey: "drover.model-catalog-store.v1") == nil)
    }

    @Test func preexistingOversizedEnvelopeIsDiscarded() throws {
        let defaults = catalogDefaults()
        let hugeModel = String(repeating: "x", count: 270_000)
        let data = try JSONSerialization.data(withJSONObject: [
            "version": 1,
            "catalogs": [:],
            "selections": [
                "mac-mini": [
                    "codex": [
                        "account_scope_id": NSNull(),
                        "model": hugeModel,
                        "thinking_effort": "",
                    ],
                ],
            ],
        ])
        defaults.set(data, forKey: "drover.model-catalog-store.v1")

        let store = HarnessModelCatalogStore(defaults: defaults)

        #expect(store.selection(hostID: "mac-mini", harness: "codex") == nil)
        #expect(defaults.data(forKey: "drover.model-catalog-store.v1") == nil)
    }

    @Test func oversizedCatalogsAreNotPersisted() {
        let defaults = catalogDefaults()
        let store = HarnessModelCatalogStore(defaults: defaults)
        let tooMany = (0...256).map { index in
            HarnessModelOption(
                id: "model-\(index)", displayName: "Model \(index)",
                description: nil, isDefault: index == 0, reasoning: nil
            )
        }
        store.save(catalog: HarnessModelCatalog(
            schemaVersion: 1, hostID: "mac-mini", harness: "codex",
            accountScopeID: "scope-a", harnessVersion: nil, discoveredAt: nil,
            stale: false, staleReason: nil, models: tooMany
        ))

        #expect(store.catalog(hostID: "mac-mini", harness: "codex") == nil)

        let huge = HarnessModelOption(
            id: "huge", displayName: "Huge",
            description: String(repeating: "x", count: 270_000),
            isDefault: true, reasoning: nil
        )
        store.save(catalog: HarnessModelCatalog(
            schemaVersion: 1, hostID: "mac-mini", harness: "codex",
            accountScopeID: "scope-a", harnessVersion: nil, discoveredAt: nil,
            stale: false, staleReason: nil, models: [huge]
        ))

        #expect(store.catalog(hostID: "mac-mini", harness: "codex") == nil)
    }
}

extension MockNetworkTests {
@Suite(.serialized)
struct HarnessModelCatalogStateTests {
    @Test @MainActor func cachedCatalogIsVisibleBeforeRefreshCompletes() async throws {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        store.save(catalog: fixtureCatalog(scope: "scope-a", model: "cached-model"))
        let state = HarnessModelCatalogState(client: client(), store: store)
        let gate = CatalogRequestGate()
        MockURLProtocol.handler = { _ in
            gate.requestStarted()
            gate.waitForRelease()
            return (200, stateCatalogJSON(model: "fresh-model"))
        }

        state.select(hostID: "mac-mini", harness: "codex")
        let refresh = Task { await state.refresh() }
        await gate.waitUntilStarted()

        #expect(state.catalog?.models[0].id == "cached-model")
        #expect(state.isRefreshing)

        gate.release()
        await refresh.value
        #expect(state.catalog?.models[0].id == "fresh-model")
    }

    @Test @MainActor func selectionsAreIndependentForEachHostHarnessPair() {
        let state = HarnessModelCatalogState(
            client: client(), store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )

        state.select(hostID: "mac-mini", harness: "codex")
        state.apply(fixtureCatalog(scope: "scope-mac", model: "mac-model"))
        state.selectedModel = "mac-model"
        state.thinkingEffort = "high"

        state.select(hostID: "nas", harness: "codex")
        state.apply(fixtureCatalog(
            hostID: "nas", scope: "scope-nas", model: "nas-model"
        ))
        state.selectedModel = "nas-model"
        state.thinkingEffort = "low"

        state.select(hostID: "mac-mini", harness: "agy")
        state.apply(fixtureCatalog(
            harness: "agy", scope: "scope-agy", model: "agy-model"
        ))
        state.selectedModel = "agy-model"

        state.select(hostID: "mac-mini", harness: "codex")
        #expect(state.selectedModel == "mac-model")
        #expect(state.thinkingEffort == "high")
        state.select(hostID: "nas", harness: "codex")
        #expect(state.selectedModel == "nas-model")
        #expect(state.thinkingEffort == "low")
        state.select(hostID: "mac-mini", harness: "agy")
        #expect(state.selectedModel == "agy-model")
    }

    @Test @MainActor func explicitSeedsTakePrecedenceOverSavedPreferences() {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        store.save(catalog: fixtureCatalog(scope: "scope-a", model: "saved-model",
                                           additionalModels: [HarnessModelOption(
                                            id: "seed-model", displayName: "Seed",
                                            description: nil, isDefault: false,
                                            reasoning: HarnessReasoningOptions(
                                                supported: ["low"], default: "low"
                                            )
                                           )]))
        store.save(selection: HarnessModelSelection(
            accountScopeID: "scope-a", model: "saved-model", thinkingEffort: "high"
        ), hostID: "mac-mini", harness: "codex")
        let state = HarnessModelCatalogState(client: client(), store: store)

        state.select(
            hostID: "mac-mini", harness: "codex",
            seedModel: "seed-model", seedThinkingEffort: "low"
        )

        #expect(state.selectedModel == "seed-model")
        #expect(state.thinkingEffort == "low")
    }

    @Test @MainActor func selectionWithoutCachedScopeIsNotRestored() {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        store.save(selection: HarnessModelSelection(
            accountScopeID: nil, model: "unverified-model", thinkingEffort: "high"
        ), hostID: "mac-mini", harness: "codex")
        let state = HarnessModelCatalogState(client: client(), store: store)

        state.select(hostID: "mac-mini", harness: "codex")

        #expect(state.catalog == nil)
        #expect(state.selectedModel.isEmpty)
        #expect(state.thinkingEffort.isEmpty)
    }

    @Test @MainActor func nilScopeCachedCatalogCannotRestoreNilScopeSelection() {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        store.save(catalog: fixtureCatalog(
            scope: nil,
            model: "unverified-model",
            discoveredAt: nil,
            stale: true
        ))
        store.save(selection: HarnessModelSelection(
            accountScopeID: nil,
            model: "unverified-model",
            thinkingEffort: "high"
        ), hostID: "mac-mini", harness: "codex")
        let state = HarnessModelCatalogState(client: client(), store: store)

        state.select(hostID: "mac-mini", harness: "codex")

        #expect(state.catalog?.accountScopeID == nil)
        #expect(state.catalog?.stale == true)
        #expect(state.selectedModel.isEmpty)
        #expect(state.thinkingEffort.isEmpty)
    }

    @Test @MainActor func nilScopeCatalogNeverPersistsSelectionChanges() {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        let state = HarnessModelCatalogState(client: client(), store: store)
        state.select(hostID: "mac-mini", harness: "codex")
        state.apply(fixtureCatalog(
            scope: nil,
            model: "session-model",
            discoveredAt: nil,
            stale: true
        ))

        state.selectedModel = "session-model"
        state.thinkingEffort = "high"

        #expect(state.modelOverride == "session-model")
        #expect(state.thinkingEffortOverride == "high")
        #expect(store.selection(hostID: "mac-mini", harness: "codex") == nil)
    }

    @Test @MainActor func accountChangeResetsAnIncompatibleSelection() async throws {
        let defaults = catalogDefaults()
        let store = HarnessModelCatalogStore(defaults: defaults)
        let state = HarnessModelCatalogState(client: client(), store: store)
        state.select(hostID: "mac-mini", harness: "codex")
        state.apply(fixtureCatalog(scope: "scope-a", model: "gpt-5.6-terra"))
        state.selectedModel = "gpt-5.6-terra"
        state.thinkingEffort = "high"

        state.apply(fixtureCatalog(scope: "scope-b", model: "gpt-6-new"))

        #expect(state.selectedModel.isEmpty)
        #expect(state.thinkingEffort.isEmpty)
        #expect(state.modelOverride == nil)
        #expect(state.thinkingEffortOverride == nil)
        #expect(state.statusMessage
            == "The previous model is unavailable for this account.")
    }

    @Test @MainActor func removedModelResetsToHarnessDefaultAndCannotBeSubmitted() {
        let state = HarnessModelCatalogState(
            client: client(), store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )
        state.select(hostID: "mac-mini", harness: "codex")
        state.apply(fixtureCatalog(scope: "scope-a", model: "old-model"))
        state.selectedModel = "old-model"
        state.thinkingEffort = "high"

        state.apply(fixtureCatalog(scope: "scope-a", model: "new-model"))

        #expect(state.selectedModel.isEmpty)
        #expect(state.thinkingEffort.isEmpty)
        #expect(state.modelOverride == nil)
        #expect(state.statusMessage
            == "The previous model is unavailable for this account.")
    }

    @Test @MainActor func changingModelClearsUnsupportedExplicitEffort() {
        let lowOnly = HarnessModelOption(
            id: "low-only", displayName: "Low only", description: nil,
            isDefault: false,
            reasoning: HarnessReasoningOptions(supported: ["low"], default: "low")
        )
        let state = HarnessModelCatalogState(
            client: client(), store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )
        state.select(hostID: "mac-mini", harness: "codex")
        state.apply(fixtureCatalog(
            scope: "scope-a", model: "full-model", additionalModels: [lowOnly]
        ))
        state.selectedModel = "full-model"
        state.thinkingEffort = "high"

        state.selectedModel = "low-only"

        #expect(state.selectedModel == "low-only")
        #expect(state.thinkingEffort.isEmpty)
        #expect(state.thinkingEffortOverride == nil)
    }

    @Test @MainActor func harnessDefaultWithoutNamedDefaultClearsEffort() {
        let onlyModel = HarnessModelOption(
            id: "manual", displayName: "Manual", description: nil,
            isDefault: false,
            reasoning: HarnessReasoningOptions(supported: ["high"], default: "high")
        )
        let catalog = HarnessModelCatalog(
            schemaVersion: 1, hostID: "mac-mini", harness: "codex",
            accountScopeID: "scope-a", harnessVersion: nil, discoveredAt: nil,
            stale: false, staleReason: nil, models: [onlyModel]
        )
        let state = HarnessModelCatalogState(
            client: client(), store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )
        state.select(hostID: "mac-mini", harness: "codex", seedThinkingEffort: "high")

        state.apply(catalog)

        #expect(state.selectedModel.isEmpty)
        #expect(state.thinkingEffort.isEmpty)
    }

    @Test @MainActor func autoAndHarnessDefaultExposeNilOverrides() {
        let state = HarnessModelCatalogState(
            client: client(), store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )
        state.select(hostID: "mac-mini", harness: "codex")

        #expect(state.modelOverride == nil)
        #expect(state.thinkingEffortOverride == nil)

        state.apply(fixtureCatalog(scope: "scope-a", model: "future-model",
                                   supportedEfforts: ["galactic"]))
        state.selectedModel = "future-model"
        state.thinkingEffort = "galactic"

        #expect(state.modelOverride == "future-model")
        #expect(state.thinkingEffortOverride == "galactic")
    }

    @Test @MainActor func lateResponseCannotReplaceCurrentPair() async {
        let state = HarnessModelCatalogState(
            client: concurrentCatalogClient(),
            store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )
        let oldGate = CatalogRequestGate()
        ConcurrentCatalogURLProtocol.handler = { request in
            if request.url?.path.contains("/mac-mini/") == true {
                oldGate.requestStarted()
                oldGate.waitForRelease()
                return (200, stateCatalogJSON(model: "late-mac"))
            }
            return (200, stateCatalogJSON(
                hostID: "nas", scope: "scope-nas", model: "current-nas"
            ))
        }
        defer { ConcurrentCatalogURLProtocol.handler = nil }

        state.select(hostID: "mac-mini", harness: "codex")
        let oldRefresh = Task { await state.refresh() }
        await oldGate.waitUntilStarted()
        state.select(hostID: "nas", harness: "codex")
        await state.refresh()
        oldGate.release()
        await oldRefresh.value

        #expect(state.hostID == "nas")
        #expect(state.catalog?.hostID == "nas")
        #expect(state.catalog?.models[0].id == "current-nas")
    }

    @Test @MainActor func transportFailureRetainsCachedCatalogAsStale() async {
        let store = HarnessModelCatalogStore(defaults: catalogDefaults())
        store.save(catalog: fixtureCatalog(scope: "scope-a", model: "cached-model"))
        let state = HarnessModelCatalogState(client: client(), store: store)
        state.select(hostID: "mac-mini", harness: "codex")
        MockURLProtocol.transportError = URLError(.notConnectedToInternet)
        defer { MockURLProtocol.transportError = nil }

        await state.refresh()

        #expect(state.catalog?.models[0].id == "cached-model")
        #expect(state.catalog?.stale == true)
        #expect(state.catalog?.staleReason == "offline")
        #expect(store.catalog(hostID: "mac-mini", harness: "codex")?.stale == true)
    }

    @Test @MainActor func forcedStateRefreshSendsOne() async {
        MockURLProtocol.handler = { request in
            #expect(request.url?.query == "harness=codex&refresh=1")
            return (200, stateCatalogJSON())
        }
        let state = HarnessModelCatalogState(
            client: client(), store: HarnessModelCatalogStore(defaults: catalogDefaults())
        )
        state.select(hostID: "mac-mini", harness: "codex")

        await state.refresh(force: true)

        #expect(state.catalog?.models[0].id == "fresh-model")
    }
}
}  // extension MockNetworkTests

private func catalogDefaults() -> UserDefaults {
    UserDefaults(suiteName: "catalog-test-\(UUID().uuidString)")!
}

private final class CatalogRequestGate: @unchecked Sendable {
    private let started = DispatchSemaphore(value: 0)
    private let released = DispatchSemaphore(value: 0)

    func requestStarted() {
        started.signal()
    }

    func waitUntilStarted() async {
        await withCheckedContinuation { continuation in
            DispatchQueue.global().async { [started] in
                started.wait()
                continuation.resume()
            }
        }
    }

    func waitForRelease() {
        released.wait()
    }

    func release() {
        released.signal()
    }
}

private final class ConcurrentCatalogURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let request = request
        DispatchQueue.global().async { [weak self] in
            guard let self, let handler = Self.handler else { return }
            let (status, body) = handler(request)
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil
            )!
            self.client?.urlProtocol(
                self, didReceive: response, cacheStoragePolicy: .notAllowed
            )
            self.client?.urlProtocol(self, didLoad: body)
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}
}

private func concurrentCatalogClient() -> DroverClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ConcurrentCatalogURLProtocol.self]
    return DroverClient(
        config: ServerConfig(urlString: "http://test.local:7080")!,
        token: "test-token",
        session: URLSession(configuration: configuration)
    )
}

private func stateCatalogJSON(
    hostID: String = "mac-mini",
    harness: String = "codex",
    scope: String = "scope-a",
    model: String = "fresh-model"
) -> Data {
    Data(#"""
    {"schema_version":1,"host_id":"\#(hostID)","harness":"\#(harness)",
     "account_scope_id":"\#(scope)","harness_version":"0.147.0",
     "discovered_at":"2026-08-14T18:22:00Z","stale":false,"stale_reason":null,
     "models":[{"id":"\#(model)","display_name":"\#(model)",
     "description":null,"is_default":true,
     "reasoning":{"supported":["low","high"],"default":"low"}}]}
    """#.utf8)
}
