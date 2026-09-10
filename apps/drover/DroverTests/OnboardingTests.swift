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
        XCTAssertEqual(mode.commandTitle, "Install Command")
        XCTAssertEqual(
            mode.guidanceText,
            "Run the command in your terminal on your machine. When it finishes, it will print a pairing QR code."
        )
        XCTAssertEqual(mode.copyButtonLabel, "Copy Command")
    }

    func testServerSetupHostModeMetadata() {
        let mode = ServerSetupMode.host
        XCTAssertEqual(mode.rawValue, "Worker Host")
        XCTAssertEqual(mode.subtitle, "Additional Machine")
        XCTAssertEqual(mode.commandTitle, "Pairing Command")
        XCTAssertEqual(mode.copyButtonLabel, "Copy Pairing Command")
    }

    func testWorkerHostCommandStartsPairingOnPrimaryHub() {
        XCTAssertEqual(
            ServerSetupMode.host.installCommand,
            "drover-server pair-host --name <host-name>"
        )
        XCTAssertFalse(ServerSetupMode.host.installCommand.contains("install.sh --join"))
    }

    func testWorkerHostCopyExplainsWhereEachCommandRuns() {
        XCTAssertEqual(
            ServerSetupMode.host.guidanceText,
            "Run this command on the primary hub. Then run the installer command it prints on the additional machine."
        )
    }

    func testServerSetupGuideViewInstantiates() {
        let guideHub = ServerSetupGuideView(
            context: .firstComputer,
            initialMode: .hub
        )
        XCTAssertNotNil(guideHub)

        let guideHost = ServerSetupGuideView(
            context: .connectedFleet,
            initialMode: .host
        )
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

    func testStageOnlyConfigurationRejectsBeforeValidationAndPreservesConnection() async throws {
        let suiteName = "drover.stage-policy.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        let tokenStore = TokenStore(service: "drover-stage-policy-\(UUID().uuidString)")
        let validatorCalls = ValidatorCallCounter()
        defer {
            try? tokenStore.delete()
            defaults.removePersistentDomain(forName: suiteName)
        }
        let existingOrigin = try XCTUnwrap(URL(string: "https://stage.example.test"))
        let existingConfig = try XCTUnwrap(ServerConfig(urlString: existingOrigin.absoluteString))
        existingConfig.save(defaults: defaults)
        try tokenStore.save("existing-token")
        let environment = AppEnvironment(
            defaults: defaults,
            tokenStore: tokenStore,
            endpointPolicy: TestFlightEndpointPolicy(requiredOrigin: existingOrigin),
            validator: { _, _ in
                await validatorCalls.recordCall()
                return nil
            },
            launchEnvironment: [:]
        )
        try XCTSkipUnless(environment.client != nil, "Keychain unavailable")
        let previousConfig = environment.config
        let replacementCredential = "synthetic-stage-replacement-token"

        let outcome = await environment.configure(
            urlString: "http://127.0.0.1:7080",
            token: replacementCredential
        )

        guard case let .failure(message) = outcome else {
            return XCTFail("an off-stage endpoint must be rejected")
        }
        let validationCallCount = await validatorCalls.callCount()
        XCTAssertEqual(message, "This TestFlight build connects only to its staging hub.")
        XCTAssertEqual(validationCallCount, 0)
        XCTAssertEqual(environment.config, previousConfig)
        XCTAssertEqual(ServerConfig.load(defaults: defaults), previousConfig)
        XCTAssertEqual(tokenStore.load(), "existing-token")
    }

    func testUITestDeviceNameOverridesTheSimulatorName() {
        XCTAssertEqual(
            UITestOverrides.pairingDeviceName(
                environment: ["DROVER_UI_TEST_DEVICE_NAME": "Drover UI E2E 1234"],
                fallback: "iPhone 17 Pro"
            ),
            "Drover UI E2E 1234"
        )
    }
}

private actor ValidatorCallCounter {
    private var value = 0

    func recordCall() {
        value += 1
    }

    func callCount() -> Int {
        value
    }
}
