import Testing
@testable import DroverKit

@Suite
struct HarnessRunPreferencesTests {
    @Test func existingSessionEditabilityMatchesHarnessLifecycle() {
        #expect(HarnessRunPreferences.canChangeInExistingSession("claude-code") == false)
        #expect(HarnessRunPreferences.canChangeInExistingSession("codex") == true)
        #expect(HarnessRunPreferences.canChangeInExistingSession("gemini") == true)
    }
}
