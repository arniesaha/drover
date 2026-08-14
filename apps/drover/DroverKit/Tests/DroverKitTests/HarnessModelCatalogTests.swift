import Foundation
import Testing
@testable import DroverKit

@Suite
struct HarnessModelCatalogTests {
    @Test func decodesUnknownModelsEffortsAndFields() throws {
        let data = Data(#"""
        {
          "schema_version":1,
          "host_id":"mac-mini",
          "harness":"codex",
          "account_scope_id":"scope-a",
          "harness_version":"0.147.0",
          "discovered_at":"2026-08-14T18:22:00Z",
          "stale":false,
          "stale_reason":null,
          "future_top_level":true,
          "models":[{
            "id":"gpt-7-nova",
            "display_name":"GPT-7 Nova",
            "description":"Future model",
            "is_default":true,
            "reasoning":{"supported":["low","galactic"],"default":"galactic"},
            "future_model_field":"kept-compatible"
          }]
        }
        """#.utf8)

        let catalog = try JSONDecoder().decode(HarnessModelCatalog.self, from: data)

        #expect(catalog.models[0].id == "gpt-7-nova")
        #expect(catalog.models[0].reasoning?.supported == ["low", "galactic"])
        #expect(catalog.namedDefault?.id == "gpt-7-nova")
        #expect(catalog.model(id: "gpt-7-nova")?.displayName == "GPT-7 Nova")
        #expect(catalog.reasoning(for: "")?.default == "galactic")
        #expect(catalog.reasoning(for: "gpt-7-nova")?.supported == ["low", "galactic"])
    }

    @Test func neverRefreshedEnvelopeAllowsNullMetadata() throws {
        let data = Data(#"""
        {"schema_version":1,"host_id":"mac-mini","harness":"codex",
         "account_scope_id":null,"harness_version":null,"discovered_at":null,
         "stale":true,"stale_reason":"offline","models":[]}
        """#.utf8)
        let catalog = try JSONDecoder().decode(HarnessModelCatalog.self, from: data)

        #expect(catalog.accountScopeID == nil)
        #expect(catalog.harnessVersion == nil)
        #expect(catalog.discoveredAt == nil)
        #expect(HarnessModelCatalogPresentation.staleText(catalog, now: .now)
            == "Never refreshed")
    }

    @Test func rejectsUnknownSchemaVersion() {
        let data = Data(#"""
        {"schema_version":2,"host_id":"mac-mini","harness":"codex",
         "stale":false,"models":[]}
        """#.utf8)

        #expect(throws: DecodingError.self) {
            _ = try JSONDecoder().decode(HarnessModelCatalog.self, from: data)
        }
    }

    @Test func encoderUsesSchemaV1SnakeCaseAndISO8601() throws {
        let discoveredAt = try #require(WireDate.parse("2026-08-14T18:22:00Z"))
        let catalog = fixtureCatalog(
            scope: "scope-a", model: "gpt-7-nova", discoveredAt: discoveredAt
        )

        let data = try JSONEncoder().encode(catalog)
        let object = try #require(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        let model = try #require((object["models"] as? [[String: Any]])?.first)
        let discovered = try #require(object["discovered_at"] as? String)

        #expect(object["schema_version"] as? Int == 1)
        #expect(object["host_id"] as? String == "mac-mini")
        #expect(object["account_scope_id"] as? String == "scope-a")
        #expect(object["schemaVersion"] == nil)
        #expect(model["display_name"] as? String == "GPT-7 Nova")
        #expect(model["is_default"] as? Bool == true)
        #expect(WireDate.parse(discovered) == discoveredAt)
        #expect(try JSONDecoder().decode(HarnessModelCatalog.self, from: data) == catalog)
    }

    @Test func markingStaleChangesOnlyStaleMetadata() {
        let catalog = fixtureCatalog(scope: "scope-a", model: "gpt-7-nova")
        let stale = catalog.markingStale(reason: "offline")

        #expect(stale.schemaVersion == catalog.schemaVersion)
        #expect(stale.hostID == catalog.hostID)
        #expect(stale.harness == catalog.harness)
        #expect(stale.accountScopeID == catalog.accountScopeID)
        #expect(stale.harnessVersion == catalog.harnessVersion)
        #expect(stale.discoveredAt == catalog.discoveredAt)
        #expect(stale.models == catalog.models)
        #expect(stale.stale)
        #expect(stale.staleReason == "offline")
    }

    @Test func presentationSearchesAllModelTextAndPreservesUnknownValues() {
        let second = HarnessModelOption(
            id: "claude_future_v2",
            displayName: "Claude Future",
            description: "Optimized for careful reviews",
            isDefault: false,
            reasoning: HarnessReasoningOptions(
                supported: ["galactic-mode"], default: "galactic-mode"
            )
        )
        let catalog = fixtureCatalog(
            scope: "scope-a", model: "gpt-7-nova", additionalModels: [second]
        )

        #expect(HarnessModelCatalogPresentation.filteredModels(
            in: catalog, query: "gpt-7-nova"
        ).map(\.id) == ["gpt-7-nova"])
        #expect(HarnessModelCatalogPresentation.filteredModels(
            in: catalog, query: "CLAUDE"
        ).map(\.id) == ["claude_future_v2"])
        #expect(HarnessModelCatalogPresentation.filteredModels(
            in: catalog, query: "careful"
        ).map(\.id) == ["claude_future_v2"])
        #expect(HarnessModelCatalogPresentation.modelTitle(
            selection: "", catalog: catalog
        ) == "Harness default")
        #expect(HarnessModelCatalogPresentation.modelTitle(
            selection: "future-unknown", catalog: catalog
        ) == "future-unknown")
        #expect(HarnessModelCatalogPresentation.title(
            forRawEffort: "galactic-mode"
        ) == "Galactic Mode")
        #expect(HarnessModelCatalogPresentation.effortTitle(
            selection: "quantum_flux", catalog: catalog
        ) == "Quantum Flux")
    }

    @Test func staleTextUsesInjectedNowAndHidesForFreshCatalog() throws {
        let discoveredAt = try #require(WireDate.parse("2026-08-14T18:22:00Z"))
        let stale = fixtureCatalog(
            scope: "scope-a", model: "gpt-7-nova", discoveredAt: discoveredAt
        ).markingStale(reason: "offline")
        let now = discoveredAt.addingTimeInterval(5 * 60 + 4)

        #expect(HarnessModelCatalogPresentation.staleText(stale, now: now)
            == "Last updated 5m ago")
        #expect(HarnessModelCatalogPresentation.staleText(
            fixtureCatalog(scope: "scope-a", model: "gpt-7-nova"), now: now
        ) == nil)
    }
}

extension MockNetworkTests {
@Suite(.serialized)
struct HarnessModelCatalogClientTests {
    @Test func requestEncodesPathQueryAndSendsBearer() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.httpMethod == "GET")
            #expect(request.url?.absoluteString.contains(
                "/harness/hosts/mac%20mini/model-catalog?"
            ) == true)
            #expect(request.url?.query == "harness=claude%20code&refresh=0")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
            return (200, modelCatalogJSON(hostID: "mac mini", harness: "claude code"))
        }

        let catalog = try await client().modelCatalog(
            hostID: "mac mini", harness: "claude code"
        )

        #expect(catalog.hostID == "mac mini")
        #expect(catalog.harness == "claude code")
    }

    @Test func forcedRefreshSendsOne() async throws {
        MockURLProtocol.handler = { request in
            #expect(request.url?.query == "harness=codex&refresh=1")
            return (200, modelCatalogJSON())
        }

        _ = try await client().modelCatalog(
            hostID: "mac-mini", harness: "codex", force: true
        )
    }

    @Test func requestUsesNormalDroverErrorMapping() async {
        MockURLProtocol.handler = { _ in
            (401, Data(#"{"error":"authentication required"}"#.utf8))
        }

        await #expect(throws: DroverError.unauthorized) {
            _ = try await client().modelCatalog(hostID: "mac-mini", harness: "codex")
        }
    }
}
}  // extension MockNetworkTests

func fixtureCatalog(
    hostID: String = "mac-mini",
    harness: String = "codex",
    scope: String?,
    model: String,
    supportedEfforts: [String] = ["low", "high"],
    discoveredAt: Date? = Date(timeIntervalSince1970: 1_755_195_720),
    stale: Bool = false,
    additionalModels: [HarnessModelOption] = []
) -> HarnessModelCatalog {
    let primary = HarnessModelOption(
        id: model,
        displayName: model == "gpt-7-nova" ? "GPT-7 Nova" : model,
        description: model == "gpt-7-nova" ? "Future model" : nil,
        isDefault: true,
        reasoning: HarnessReasoningOptions(
            supported: supportedEfforts,
            default: supportedEfforts.first
        )
    )
    return HarnessModelCatalog(
        schemaVersion: 1,
        hostID: hostID,
        harness: harness,
        accountScopeID: scope,
        harnessVersion: "0.147.0",
        discoveredAt: discoveredAt,
        stale: stale,
        staleReason: stale ? "offline" : nil,
        models: [primary] + additionalModels
    )
}

private func modelCatalogJSON(
    hostID: String = "mac-mini",
    harness: String = "codex",
    scope: String? = "scope-a",
    model: String = "gpt-5.6-terra"
) -> Data {
    let scopeJSON = scope.map { #""\#($0)""# } ?? "null"
    return Data(#"""
    {"schema_version":1,"host_id":"\#(hostID)","harness":"\#(harness)",
     "account_scope_id":\#(scopeJSON),"harness_version":"0.147.0",
     "discovered_at":"2026-08-14T18:22:00Z","stale":false,"stale_reason":null,
     "models":[{"id":"\#(model)","display_name":"\#(model)",
     "description":null,"is_default":true,
     "reasoning":{"supported":["low","high"],"default":"low"}}]}
    """#.utf8)
}
