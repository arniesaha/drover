import XCTest
@testable import DroverKit
final class AccountQuotaPresentationTests: XCTestCase {
    func testFormatsRemainingQuota() { XCTAssertEqual(AccountQuotaPresentation.remainingText(usedPercent: 38), "62% left") }
    func testUnknownQuota() { XCTAssertEqual(AccountQuotaPresentation.remainingText(usedPercent: nil), "quota unknown") }

    func testUsesTheTightestAccountWindowForTheHarnessOnItsHost() throws {
        let accounts = try accounts(from: #"""
        [
          {"snapshot_id":"openai","dedup_key":"openai","provider":"openai","account_label":"Personal","host_id":"mac-mini","status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour","used_percent":38},{"kind":"seven_day","used_percent":64}]},
          {"snapshot_id":"anthropic","dedup_key":"anthropic","provider":"anthropic","account_label":"Team","host_id":"mac-mini","status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour","used_percent":12}]},
          {"snapshot_id":"other-host","dedup_key":"other-host","provider":"openai","account_label":"Other","host_id":"nas","status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour","used_percent":99}]}
        ]
        """#)

        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "codex", hostID: "mac-mini", accounts: accounts), "36% left")
        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "claude-code", hostID: "mac-mini", accounts: accounts), "88% left")
    }

    func testReportsUnknownOnlyWhenThatHarnessHasNoReportedPercentage() throws {
        let accounts = try accounts(from: #"""
        [{"snapshot_id":"openai","dedup_key":"openai","provider":"openai","account_label":"Personal","host_id":"mac-mini","status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour"}]}]
        """#)

        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "codex", hostID: "mac-mini", accounts: accounts), "quota unknown")
        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "shell", hostID: "mac-mini", accounts: accounts), "quota unknown")
    }

    private func accounts(from json: String) throws -> [ProviderAccount] {
        try JSONDecoder().decode([ProviderAccount].self, from: Data(json.utf8))
    }
}
