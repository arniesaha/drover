import XCTest
@testable import DroverKit
final class AccountQuotaPresentationTests: XCTestCase {
    func testFormatsRemainingQuota() { XCTAssertEqual(AccountQuotaPresentation.remainingText(usedPercent: 38), "62% left") }
    func testUnknownQuota() { XCTAssertEqual(AccountQuotaPresentation.remainingText(usedPercent: nil), "quota unknown") }

    func testUsesTheHubReportedAccountHarnessRelationship() throws {
        let accounts = try accounts(from: #"""
        [
          {"snapshot_id":"first","dedup_key":"first","provider":"provider-one","account_label":"Personal","host_id":"mac-mini","harnesses":["reviewer"],"status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour","used_percent":38},{"kind":"seven_day","used_percent":64}]},
          {"snapshot_id":"second","dedup_key":"second","provider":"provider-two","account_label":"Team","host_id":"mac-mini","harnesses":["builder"],"status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour","used_percent":12}]},
          {"snapshot_id":"other-host","dedup_key":"other-host","provider":"provider-one","account_label":"Other","host_id":"nas","harnesses":["reviewer"],"status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour","used_percent":99}]}
        ]
        """#)

        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "reviewer", hostID: "mac-mini", accounts: accounts), "36% left")
        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "builder", hostID: "mac-mini", accounts: accounts), "88% left")
    }

    func testReportsUnknownOnlyWhenThatHarnessHasNoReportedPercentage() throws {
        let accounts = try accounts(from: #"""
        [{"snapshot_id":"provider-one","dedup_key":"provider-one","provider":"provider-one","account_label":"Personal","host_id":"mac-mini","harnesses":["editor"],"status":"ok","observed_at":"2026-10-01T00:00:00Z","source":"fixture","windows":[{"kind":"five_hour"}]}]
        """#)

        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "editor", hostID: "mac-mini", accounts: accounts), "quota unknown")
        XCTAssertEqual(AccountQuotaPresentation.remainingText(harness: "unrelated", hostID: "mac-mini", accounts: accounts), "quota unknown")
    }

    private func accounts(from json: String) throws -> [ProviderAccount] {
        try JSONDecoder().decode([ProviderAccount].self, from: Data(json.utf8))
    }
}
