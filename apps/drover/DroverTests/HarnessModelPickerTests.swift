import Foundation
import Testing
@testable import Drover
@testable import DroverKit

@Suite
struct HarnessModelPickerTests {
    @Test func harnessDefaultIsAlwaysFirstAndNativeOrderIsPreserved() {
        let catalog = catalog(models: [
            model(id: "native.second", displayName: "Second native model"),
            model(id: "native.first", displayName: "First native model")
        ])

        let rows = HarnessModelPickerPresentation.rows(in: catalog, query: "")

        #expect(rows.map(\.selection) == ["", "native.second", "native.first"])
        #expect(rows.first?.title == "Harness default")
    }

    @Test func exactIDsAndDescriptionsRemainSearchable() {
        let catalog = catalog(models: [
            model(
                id: "provider.identifier-v2",
                displayName: "Future model",
                description: "Optimized for careful reviews"
            ),
            model(id: "provider.other", displayName: "Other model")
        ])

        let identifierMatches = HarnessModelPickerPresentation.rows(
            in: catalog, query: "identifier-v2"
        )
        let descriptionMatches = HarnessModelPickerPresentation.rows(
            in: catalog, query: "CAREFUL"
        )

        #expect(identifierMatches.map(\.selection) == ["", "provider.identifier-v2"])
        #expect(descriptionMatches.map(\.selection) == ["", "provider.identifier-v2"])
    }

    @Test func harnessDefaultUsesTheNamedModelsReasoningChoices() {
        let catalog = catalog(models: [
            model(id: "provider.manual", displayName: "Manual model"),
            model(
                id: "provider.native-default",
                displayName: "Native default",
                isDefault: true,
                reasoning: HarnessReasoningOptions(
                    supported: ["low", "ultra-next"], default: "ultra-next"
                )
            )
        ])

        let effort = HarnessModelPickerPresentation.effort(
            in: catalog, selectedModel: "", selectedEffort: ""
        )

        #expect(effort?.title == "Auto (Ultra Next)")
        #expect(effort?.choices.map(\.rawValue) == ["", "low", "ultra-next"])
        #expect(effort?.choices.map(\.title) == ["Auto (Ultra Next)", "Low", "Ultra Next"])
    }

    @Test func modelWithoutReasoningHidesTheEffortControl() {
        let catalog = catalog(models: [
            model(id: "provider.no-reasoning", displayName: "No reasoning")
        ])

        let effort = HarnessModelPickerPresentation.effort(
            in: catalog,
            selectedModel: "provider.no-reasoning",
            selectedEffort: ""
        )

        #expect(effort == nil)
    }

    @Test func staleAndNeverRefreshedStatesExposeRetryCopy() {
        let now = Date(timeIntervalSince1970: 1_755_195_720)
        let stale = catalog(
            discoveredAt: now.addingTimeInterval(-(5 * 60 + 4)),
            stale: true,
            staleReason: "offline",
            models: [model(id: "provider.cached", displayName: "Cached model")]
        )

        let staleStatus = HarnessModelPickerPresentation.status(
            catalog: stale, statusMessage: nil, now: now
        )
        let neverStatus = HarnessModelPickerPresentation.status(
            catalog: nil, statusMessage: nil, now: now
        )

        #expect(staleStatus.freshnessText == "Last updated 5m ago")
        #expect(staleStatus.detailText == "Host is offline.")
        #expect(staleStatus.retryTitle == "Retry")
        #expect(neverStatus.freshnessText == "Never refreshed")
        #expect(neverStatus.retryTitle == "Retry")
    }

    @Test func pickerRefreshesOnlyWhenItsDisplayedCatalogIsOldOrStale() {
        let now = Date(timeIntervalSince1970: 1_755_195_720)
        let recent = catalog(
            discoveredAt: now.addingTimeInterval(-299),
            models: [model(id: "provider.recent", displayName: "Recent model")]
        )
        let old = catalog(
            discoveredAt: now.addingTimeInterval(-300),
            models: [model(id: "provider.old", displayName: "Old model")]
        )
        let stale = catalog(
            discoveredAt: now.addingTimeInterval(-1),
            stale: true,
            staleReason: "timeout",
            models: [model(id: "provider.stale", displayName: "Stale model")]
        )

        #expect(!HarnessModelPickerPresentation.shouldRefreshOnPresentation(
            catalog: recent, now: now
        ))
        #expect(HarnessModelPickerPresentation.shouldRefreshOnPresentation(
            catalog: old, now: now
        ))
        #expect(HarnessModelPickerPresentation.shouldRefreshOnPresentation(
            catalog: stale, now: now
        ))
        #expect(HarnessModelPickerPresentation.shouldRefreshOnPresentation(
            catalog: nil, now: now
        ))
    }

    private func catalog(
        discoveredAt: Date? = Date(timeIntervalSince1970: 1_755_195_720),
        stale: Bool = false,
        staleReason: String? = nil,
        models: [HarnessModelOption]
    ) -> HarnessModelCatalog {
        HarnessModelCatalog(
            schemaVersion: 1,
            hostID: "test-host",
            harness: "test-harness",
            accountScopeID: "test-scope",
            harnessVersion: "test-version",
            discoveredAt: discoveredAt,
            stale: stale,
            staleReason: staleReason,
            models: models
        )
    }

    private func model(
        id: String,
        displayName: String,
        description: String? = nil,
        isDefault: Bool = false,
        reasoning: HarnessReasoningOptions? = nil
    ) -> HarnessModelOption {
        HarnessModelOption(
            id: id,
            displayName: displayName,
            description: description,
            isDefault: isDefault,
            reasoning: reasoning
        )
    }
}
