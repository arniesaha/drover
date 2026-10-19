import XCTest
@testable import DroverKit

final class AccountHealthPresentationTests: XCTestCase {
    private func account(
        provider: String, label: String, used: Double = 10, status: String = "ok",
        errorCategory: String? = nil
    ) throws -> ProviderAccount {
        let error = errorCategory.map { "\"\($0)\"" } ?? "null"
        let json = """
        {"snapshot_id":"\(provider)-\(label)","dedup_key":"\(provider)-\(label)",
         "provider":"\(provider)","account_label":"\(label)","host_id":"hub",
         "status":"\(status)","observed_at":"2026-10-19T17:00:00Z","source":"test",
         "error_category":\(error),"windows":[{"kind":"five_hour","used_percent":\(used)}]}
        """
        return try JSONDecoder().decode(ProviderAccount.self, from: Data(json.utf8))
    }

    func testRealShapedAccountsCountEachAffectedSubscriptionOnce() throws {
        let accounts = try [
            account(provider: "openai", label: "arnab@example.com", used: 88),
            account(provider: "anthropic", label: "work@example.com", status: "error", errorCategory: "login_expired"),
            account(provider: "google", label: "google@example.com"),
            account(provider: "anthropic", label: "personal@example.com"),
        ]

        XCTAssertEqual(AccountHealthPresentation.count(accounts: accounts), 2)
        XCTAssertEqual(AccountHealthPresentation.badgeValue(for: 2), "2")
    }

    func testLowQuotaIsStrictlyBelowTwentyPercentRemaining() throws {
        XCTAssertNil(AccountHealthPresentation.attention(for: try account(provider: "openai", label: "at20", used: 80)))
        XCTAssertEqual(
            AccountHealthPresentation.attention(for: try account(provider: "openai", label: "at12", used: 88))?.reason,
            "12% left"
        )
    }

    func testAuthenticationAndStaleReasonsSortBeforeQuota() throws {
        let low = try account(provider: "openai", label: "low", used: 88)
        let expired = try account(provider: "anthropic", label: "expired", status: "error", errorCategory: "login_expired")
        let stale = try account(provider: "google", label: "stale", status: "stale")

        XCTAssertEqual(AccountHealthPresentation.attention(for: expired)?.reason, "login expired")
        XCTAssertEqual(AccountHealthPresentation.attention(for: stale)?.reason, "stale")
        XCTAssertEqual(AccountHealthPresentation.attention(for: [low, expired])?.reason, "login expired")
    }
}
