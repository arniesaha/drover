import XCTest
@testable import DroverKit
final class AccountQuotaPresentationTests: XCTestCase {
    func testFormatsRemainingQuota() { XCTAssertEqual(AccountQuotaPresentation.remainingText(usedPercent: 38), "62% left") }
    func testUnknownQuota() { XCTAssertEqual(AccountQuotaPresentation.remainingText(usedPercent: nil), "quota unknown") }
}
