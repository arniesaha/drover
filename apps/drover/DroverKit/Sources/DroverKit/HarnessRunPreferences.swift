public enum HarnessRunPreferences {
    public static func canChangeInExistingSession(_ harness: String) -> Bool {
        harness != "claude-code"
    }
}
