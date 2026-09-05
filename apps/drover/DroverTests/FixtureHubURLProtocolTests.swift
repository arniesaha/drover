import Foundation
import XCTest
@testable import Drover
import DroverKit

#if DEBUG
@MainActor
final class FixtureHubURLProtocolTests: XCTestCase {
    func testExactModelCatalogRouteAcceptsClientQueryParameters() async throws {
        let runID = UUID().uuidString
        let session = fixtureSession(runID: runID)
        defer { cleanup(session: session, runID: runID) }
        let client = DroverClient(
            config: ServerConfig(urlString: FixtureScenarioData.coreJourney.serverURLString)!,
            token: FixtureScenarioData.syntheticBearerToken,
            session: session
        )

        let catalog = try await client.modelCatalog(
            hostID: FixtureScenarioData.coreJourney.hostID, harness: "codex", force: true
        )

        XCTAssertEqual(catalog.hostID, FixtureScenarioData.coreJourney.hostID)
        XCTAssertEqual(catalog.harness, "codex")
    }

    func testModelCatalogNearMissRoutesReturn404() async throws {
        let runID = UUID().uuidString
        let session = fixtureSession(runID: runID)
        defer { cleanup(session: session, runID: runID) }

        for path in [
            "/harness/sessions/fixture-session/model-catalog",
            "/unexpected/model-catalog-suffix",
        ] {
            let url = URL(string: FixtureScenarioData.coreJourney.serverURLString + path)!
            let (_, response) = try await session.data(from: url)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 404, path)
        }
    }

    private func fixtureSession(runID: String) -> URLSession {
        FixtureHubURLProtocol.install(receiptState: FixtureReceiptState(runID: runID))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FixtureHubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func cleanup(session: URLSession, runID: String) {
        session.invalidateAndCancel()
        let suiteName = "com.arnab.drover.ui-fixture.\(runID)"
        UserDefaults(suiteName: suiteName)?.removePersistentDomain(forName: suiteName)
    }
}
#endif
