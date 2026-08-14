import Foundation
import Observation

@MainActor
@Observable
public final class HarnessModelCatalogState {
    private let client: DroverClient
    private let store: HarnessModelCatalogStore
    private var selectionGeneration = 0
    private var refreshGeneration = 0
    private var isReconciling = false

    public private(set) var hostID = ""
    public private(set) var harness = ""
    public private(set) var catalog: HarnessModelCatalog?
    public private(set) var isRefreshing = false
    public private(set) var statusMessage: String?
    public var selectedModel = "" { didSet { selectionDidChange() } }
    public var thinkingEffort = "" { didSet { selectionDidChange() } }

    public init(client: DroverClient, store: HarnessModelCatalogStore) {
        self.client = client
        self.store = store
    }

    public var modelOverride: String? { normalized(selectedModel) }
    public var thinkingEffortOverride: String? { normalized(thinkingEffort) }

    public func select(
        hostID: String,
        harness: String,
        seedModel: String? = nil,
        seedThinkingEffort: String? = nil
    ) {
        selectionGeneration &+= 1
        refreshGeneration &+= 1
        isRefreshing = false
        self.hostID = hostID
        self.harness = harness
        statusMessage = nil

        let cached = store.catalog(hostID: hostID, harness: harness)
        let saved = store.selection(hostID: hostID, harness: harness)
        let cachedScope = concreteAccountScope(cached?.accountScopeID)
        let scopedSelection = cachedScope.flatMap { scope in
            saved.flatMap { selection in
                selection.accountScopeID == scope ? selection : nil
            }
        }

        reconcileWithoutCallbacks {
            catalog = cached
            selectedModel = seedModel ?? scopedSelection?.model ?? ""
            thinkingEffort = seedThinkingEffort ?? scopedSelection?.thinkingEffort ?? ""
            reconcileSelection()
        }
    }

    public func refresh(force: Bool = false) async {
        guard !hostID.isEmpty, !harness.isEmpty else { return }
        refreshGeneration &+= 1
        let requestGeneration = refreshGeneration
        let expectedSelectionGeneration = selectionGeneration
        let expectedHostID = hostID
        let expectedHarness = harness
        isRefreshing = true

        defer {
            if requestGeneration == refreshGeneration,
               expectedSelectionGeneration == selectionGeneration {
                isRefreshing = false
            }
        }

        do {
            let fresh = try await client.modelCatalog(
                hostID: expectedHostID,
                harness: expectedHarness,
                force: force
            )
            guard requestGeneration == refreshGeneration,
                  expectedSelectionGeneration == selectionGeneration,
                  hostID == expectedHostID,
                  harness == expectedHarness else { return }
            apply(fresh)
        } catch {
            guard requestGeneration == refreshGeneration,
                  expectedSelectionGeneration == selectionGeneration,
                  hostID == expectedHostID,
                  harness == expectedHarness else { return }
            if let droverError = error as? DroverError, droverError.isCancellation {
                return
            }
            if let cached = catalog {
                let reason = (error as? DroverError).map { droverError -> String in
                    if case .transport = droverError { return "offline" }
                    return "refresh_failed"
                } ?? "refresh_failed"
                let stale = cached.markingStale(reason: reason)
                catalog = stale
                store.save(catalog: stale)
            }
            statusMessage = error.localizedDescription
        }
    }

    public func apply(_ freshCatalog: HarnessModelCatalog) {
        guard freshCatalog.hostID == hostID, freshCatalog.harness == harness else { return }

        let previousCatalog = catalog
        let scopeChanged = previousCatalog != nil
            && previousCatalog?.accountScopeID != freshCatalog.accountScopeID
        let modelWasRemoved = !selectedModel.isEmpty
            && freshCatalog.model(id: selectedModel) == nil
        let lostSelection = scopeChanged || modelWasRemoved

        reconcileWithoutCallbacks {
            catalog = freshCatalog
            if lostSelection {
                selectedModel = ""
                thinkingEffort = ""
            } else {
                reconcileSelection()
            }
        }

        statusMessage = lostSelection
            ? "The previous model is unavailable for this account."
            : nil
        store.save(catalog: freshCatalog)
        persistSelection()
    }

    private func selectionDidChange() {
        guard !isReconciling else { return }
        reconcileWithoutCallbacks {
            reconcileSelection()
        }
        persistSelection()
    }

    private func reconcileSelection() {
        guard let catalog else { return }

        if !selectedModel.isEmpty, catalog.model(id: selectedModel) == nil {
            selectedModel = ""
            thinkingEffort = ""
        }

        guard !thinkingEffort.isEmpty else { return }
        guard let reasoning = catalog.reasoning(for: selectedModel),
              reasoning.supported.contains(thinkingEffort) else {
            thinkingEffort = ""
            return
        }
    }

    private func persistSelection() {
        guard !hostID.isEmpty,
              !harness.isEmpty,
              let accountScopeID = concreteAccountScope(catalog?.accountScopeID) else { return }
        store.save(
            selection: HarnessModelSelection(
                accountScopeID: accountScopeID,
                model: selectedModel,
                thinkingEffort: thinkingEffort
            ),
            hostID: hostID,
            harness: harness
        )
    }

    private func reconcileWithoutCallbacks(_ operation: () -> Void) {
        let wasReconciling = isReconciling
        isReconciling = true
        operation()
        isReconciling = wasReconciling
    }

    private func concreteAccountScope(_ accountScopeID: String?) -> String? {
        guard let accountScopeID,
              !accountScopeID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        return accountScopeID
    }

    private func normalized(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
