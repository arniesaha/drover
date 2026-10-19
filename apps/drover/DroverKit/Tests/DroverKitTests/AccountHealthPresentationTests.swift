import XCTest
@testable import DroverKit

final class AccountHealthPresentationTests: XCTestCase {
    private func needs(_ used: [Double?] = [], _ auth: Bool = false, _ error: Bool = false, _ stale: Bool = false) -> Bool {
        AccountHealthPresentation.needsAttention(usedPercents: used, hasAuthError: auth, hasProviderError: error, isStale: stale)
    }
    func testFineAndNoAccountsAreHidden() { XCTAssertFalse(needs([79])); XCTAssertFalse(needs()) }
    func testLowQuotaShows() { XCTAssertTrue(needs([85])) }
    func testAuthAndProviderErrorsShow() { XCTAssertTrue(needs([], true)); XCTAssertTrue(needs([], false, true)) }
    func testStaleShows() { XCTAssertTrue(needs([], false, false, true)) }
}
