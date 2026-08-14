import Foundation

public struct HarnessModelSelection: Codable, Sendable, Equatable {
    public let accountScopeID: String?
    public let model: String
    public let thinkingEffort: String

    public init(accountScopeID: String?, model: String, thinkingEffort: String) {
        self.accountScopeID = accountScopeID
        self.model = model
        self.thinkingEffort = thinkingEffort
    }

    private enum CodingKeys: String, CodingKey {
        case accountScopeID = "account_scope_id"
        case model
        case thinkingEffort = "thinking_effort"
    }
}

/// A small last-known-good catalog and preference cache. It deliberately holds
/// only opaque account scopes and model choices; bearer credentials and account
/// identity labels never enter this envelope.
public final class HarnessModelCatalogStore {
    public static let defaultsKey = "drover.model-catalog-store.v1"
    public static let maximumPersistedModels = 256
    public static let maximumEncodedBytes = 256 * 1024

    private let defaults: UserDefaults
    private var envelope: Envelope

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        guard let data = defaults.data(forKey: Self.defaultsKey) else {
            envelope = Envelope()
            return
        }
        guard data.count <= Self.maximumEncodedBytes else {
            defaults.removeObject(forKey: Self.defaultsKey)
            envelope = Envelope()
            return
        }
        do {
            let decoded = try JSONDecoder().decode(Envelope.self, from: data)
            let modelCountsAreBounded = decoded.catalogs.values.allSatisfy { catalogs in
                catalogs.values.allSatisfy {
                    $0.models.count <= Self.maximumPersistedModels
                }
            }
            guard decoded.version == Envelope.currentVersion, modelCountsAreBounded else {
                defaults.removeObject(forKey: Self.defaultsKey)
                envelope = Envelope()
                return
            }
            envelope = decoded
        } catch {
            defaults.removeObject(forKey: Self.defaultsKey)
            envelope = Envelope()
        }
    }

    public func catalog(hostID: String, harness: String) -> HarnessModelCatalog? {
        envelope.catalogs[hostID]?[harness]
    }

    public func save(catalog: HarnessModelCatalog) {
        guard catalog.models.count <= Self.maximumPersistedModels else { return }
        var candidate = envelope
        candidate.catalogs[catalog.hostID, default: [:]][catalog.harness] = catalog
        persist(candidate)
    }

    public func selection(hostID: String, harness: String) -> HarnessModelSelection? {
        envelope.selections[hostID]?[harness]
    }

    public func save(
        selection: HarnessModelSelection,
        hostID: String,
        harness: String
    ) {
        var candidate = envelope
        candidate.selections[hostID, default: [:]][harness] = selection
        persist(candidate)
    }

    public func clearSelection(hostID: String, harness: String) {
        guard envelope.selections[hostID]?[harness] != nil else { return }
        var candidate = envelope
        candidate.selections[hostID]?[harness] = nil
        if candidate.selections[hostID]?.isEmpty == true {
            candidate.selections[hostID] = nil
        }
        persist(candidate)
    }

    private func persist(_ candidate: Envelope) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(candidate),
              data.count <= Self.maximumEncodedBytes else { return }
        defaults.set(data, forKey: Self.defaultsKey)
        envelope = candidate
    }

    private struct Envelope: Codable {
        static let currentVersion = 1

        var version = currentVersion
        var catalogs: [String: [String: HarnessModelCatalog]] = [:]
        var selections: [String: [String: HarnessModelSelection]] = [:]
    }
}
