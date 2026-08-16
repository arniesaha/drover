import XCTest
import SwiftUI
@testable import Drover
@testable import DroverKit

@MainActor
final class OnboardingTests: XCTestCase {

    func testOnboardingStepsOrderAndMetadata() {
        let steps = OnboardingStep.allCases
        XCTAssertEqual(steps.count, 3)
        XCTAssertEqual(steps[0], .welcome)
        XCTAssertEqual(steps[0].title, "Welcome")
        XCTAssertEqual(steps[1], .serverSetup)
        XCTAssertEqual(steps[1].title, "Server Setup")
        XCTAssertEqual(steps[2], .pair)
        XCTAssertEqual(steps[2].title, "Pair & Connect")
    }

    func testServerSetupHubModeMetadata() {
        let mode = ServerSetupMode.hub
        XCTAssertEqual(mode.rawValue, "Primary Hub")
        XCTAssertEqual(mode.subtitle, "First Machine")
        XCTAssertEqual(
            mode.descriptionText,
            "Runs `drover-server` to coordinate sessions, manage credentials, and serve this app."
        )
        XCTAssertEqual(
            mode.installCommand,
            "curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash"
        )
        XCTAssertEqual(mode.copyButtonLabel, "Copy Command")
    }

    func testServerSetupHostModeMetadata() {
        let mode = ServerSetupMode.host
        XCTAssertEqual(mode.rawValue, "Worker Host")
        XCTAssertEqual(mode.subtitle, "Additional Machine")
        XCTAssertEqual(
            mode.descriptionText,
            "Runs `drover-harnessd` on another Mac, Linux box, or NAS to execute agent sessions."
        )
        XCTAssertEqual(
            mode.installCommand,
            "curl -fsSL https://raw.githubusercontent.com/arniesaha/drover/main/install.sh | bash -s -- --join '<hub-url>'"
        )
        XCTAssertEqual(mode.copyButtonLabel, "Copy Join Command")
    }

    func testServerSetupContentCustomKnownURL() {
        let modeBinding = Binding<ServerSetupMode>.constant(.host)
        let customURL = "http://192.168.1.50:7080"
        let content = ServerSetupContent(selectedMode: modeBinding, knownServerURL: customURL)
        XCTAssertNotNil(content)
    }

    func testServerSetupGuideViewInstantiates() {
        let guideHub = ServerSetupGuideView(initialMode: .hub)
        XCTAssertNotNil(guideHub)

        let guideHost = ServerSetupGuideView(initialMode: .host, knownServerURL: "http://100.64.0.1:7080")
        XCTAssertNotNil(guideHost)
    }

    func testOnboardingViewInstantiatesWithEnvironment() {
        let suiteName = "drover.onboarding.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let store = TokenStore(service: "drover-onboarding-\(UUID().uuidString)")
        defer {
            try? store.delete()
            defaults.removePersistentDomain(forName: suiteName)
        }

        let environment = AppEnvironment(defaults: defaults, tokenStore: store)
        var finished = false
        let view = OnboardingView(environment: environment, onFinished: { finished = true })
        XCTAssertNotNil(view)
        XCTAssertFalse(finished)
    }

    func testManualPairingPayloadGeneration() throws {
        let model = PairingModel(serverURLString: "192.168.1.100:7080")
        model.manualCode = "ABCD-1234"
        let payload = try XCTUnwrap(model.manualPayload())
        XCTAssertEqual(payload.code, "ABCD-1234")
        XCTAssertEqual(payload.serverURL.absoluteString, "http://192.168.1.100:7080")
    }
}
