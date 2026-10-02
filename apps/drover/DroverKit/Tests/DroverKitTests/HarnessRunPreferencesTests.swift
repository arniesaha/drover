import Testing
@testable import DroverKit

@Suite
struct HarnessRunPreferencesTests {
    /// Mid-session editability follows the advertised `turn_preferences`
    /// flag (Claude's persistent process fixes them at startup), never a name.
    @Test func existingSessionEditabilityFollowsTheAdvertisedCapability() {
        func editable(_ name: String) -> Bool {
            HarnessRunPreferences.canChangeInExistingSession(HarnessControls(offer: v1Offer(name)))
        }
        #expect(editable("claude-code") == false)
        #expect(editable("codex") == true)
        #expect(editable("agy") == true)
        #expect(editable("deepseek-harness") == true)
        #expect(editable("shell") == false)
    }

    @Test func aHostThatPredatesTheFlagKeepsPreferencesLocked() {
        let legacy = HarnessOffer(name: "codex", enabled: true, advertisement: .legacy)
        let withoutFlag = HarnessOffer(
            name: "codex",
            capabilities: HarnessCapabilities(launchModes: [.structured], modelCatalog: true)
        )
        #expect(HarnessRunPreferences.canChangeInExistingSession(HarnessControls(offer: legacy)) == false)
        #expect(HarnessRunPreferences.canChangeInExistingSession(HarnessControls(offer: withoutFlag)) == false)
        #expect(HarnessRunPreferences.canChangeInExistingSession(.unresolved) == false)
    }
}
