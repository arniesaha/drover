import Foundation

public struct HarnessReasoningOptions: Codable, Sendable, Equatable {
    public let supported: [String]
    public let `default`: String?

    public init(supported: [String], default: String?) {
        self.supported = supported
        self.default = `default`
    }
}

public struct HarnessModelOption: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let displayName: String
    public let description: String?
    public let isDefault: Bool
    public let reasoning: HarnessReasoningOptions?

    public init(
        id: String,
        displayName: String,
        description: String?,
        isDefault: Bool,
        reasoning: HarnessReasoningOptions?
    ) {
        self.id = id
        self.displayName = displayName
        self.description = description
        self.isDefault = isDefault
        self.reasoning = reasoning
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case displayName = "display_name"
        case description
        case isDefault = "is_default"
        case reasoning
    }
}

public struct HarnessModelCatalog: Codable, Sendable, Equatable {
    public let schemaVersion: Int
    public let hostID: String
    public let harness: String
    public let accountScopeID: String?
    public let harnessVersion: String?
    public let discoveredAt: Date?
    public let stale: Bool
    public let staleReason: String?
    public let models: [HarnessModelOption]

    public init(
        schemaVersion: Int,
        hostID: String,
        harness: String,
        accountScopeID: String?,
        harnessVersion: String?,
        discoveredAt: Date?,
        stale: Bool,
        staleReason: String?,
        models: [HarnessModelOption]
    ) {
        self.schemaVersion = schemaVersion
        self.hostID = hostID
        self.harness = harness
        self.accountScopeID = accountScopeID
        self.harnessVersion = harnessVersion
        self.discoveredAt = discoveredAt
        self.stale = stale
        self.staleReason = staleReason
        self.models = models
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        guard schemaVersion == 1 else {
            throw DecodingError.dataCorruptedError(
                forKey: .schemaVersion,
                in: container,
                debugDescription: "Unsupported model catalog schema version \(schemaVersion)"
            )
        }

        let discoveredAt: Date?
        if let rawDate = try container.decodeIfPresent(String.self, forKey: .discoveredAt) {
            guard let parsed = WireDate.parse(rawDate) else {
                throw DecodingError.dataCorruptedError(
                    forKey: .discoveredAt,
                    in: container,
                    debugDescription: "Invalid model catalog discovery date"
                )
            }
            discoveredAt = parsed
        } else {
            discoveredAt = nil
        }

        self.init(
            schemaVersion: schemaVersion,
            hostID: try container.decode(String.self, forKey: .hostID),
            harness: try container.decode(String.self, forKey: .harness),
            accountScopeID: try container.decodeIfPresent(
                String.self, forKey: .accountScopeID
            ),
            harnessVersion: try container.decodeIfPresent(
                String.self, forKey: .harnessVersion
            ),
            discoveredAt: discoveredAt,
            stale: try container.decode(Bool.self, forKey: .stale),
            staleReason: try container.decodeIfPresent(String.self, forKey: .staleReason),
            models: try container.decode([HarnessModelOption].self, forKey: .models)
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(schemaVersion, forKey: .schemaVersion)
        try container.encode(hostID, forKey: .hostID)
        try container.encode(harness, forKey: .harness)
        try encodeNullable(accountScopeID, to: &container, forKey: .accountScopeID)
        try encodeNullable(harnessVersion, to: &container, forKey: .harnessVersion)
        if let discoveredAt {
            try container.encode(
                WireDate.withFractionalSeconds.string(from: discoveredAt),
                forKey: .discoveredAt
            )
        } else {
            try container.encodeNil(forKey: .discoveredAt)
        }
        try container.encode(stale, forKey: .stale)
        try encodeNullable(staleReason, to: &container, forKey: .staleReason)
        try container.encode(models, forKey: .models)
    }

    public var namedDefault: HarnessModelOption? {
        models.first(where: \.isDefault)
    }

    public func model(id: String) -> HarnessModelOption? {
        models.first { $0.id == id }
    }

    public func reasoning(for selectedModel: String) -> HarnessReasoningOptions? {
        if selectedModel.isEmpty {
            return namedDefault?.reasoning
        }
        return model(id: selectedModel)?.reasoning
    }

    public func markingStale(reason: String?) -> HarnessModelCatalog {
        HarnessModelCatalog(
            schemaVersion: schemaVersion,
            hostID: hostID,
            harness: harness,
            accountScopeID: accountScopeID,
            harnessVersion: harnessVersion,
            discoveredAt: discoveredAt,
            stale: true,
            staleReason: reason,
            models: models
        )
    }

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case hostID = "host_id"
        case harness
        case accountScopeID = "account_scope_id"
        case harnessVersion = "harness_version"
        case discoveredAt = "discovered_at"
        case stale
        case staleReason = "stale_reason"
        case models
    }
}

private func encodeNullable<Key: CodingKey>(
    _ value: String?,
    to container: inout KeyedEncodingContainer<Key>,
    forKey key: Key
) throws {
    if let value {
        try container.encode(value, forKey: key)
    } else {
        try container.encodeNil(forKey: key)
    }
}
