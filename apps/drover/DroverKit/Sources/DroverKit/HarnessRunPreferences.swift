public enum HarnessRunPreferences {
    /// Whether model and effort may change between turns of a running
    /// session. Read from the advertised `turn_preferences` capability: a
    /// harness whose process fixes them at startup (or a host that predates
    /// the flag) keeps them locked.
    public static func canChangeInExistingSession(_ controls: HarnessControls) -> Bool {
        controls.canChangePreferencesMidSession
    }
}
