import Foundation

/// Limits an internal TestFlight build to its configured staging origin while
/// keeping ordinary self-hosted builds configurable.
struct TestFlightEndpointPolicy {
    private struct Origin: Equatable {
        let host: String
        let port: Int

        init?(_ url: URL) {
            guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  components.scheme?.lowercased() == "https",
                  let host = components.host?.lowercased(),
                  !host.isEmpty,
                  components.user == nil,
                  components.password == nil,
                  components.query == nil,
                  components.fragment == nil,
                  // A bare "/" is the canonical root, not a path: it is what a
                  // browser copies and what `normalize_staging_url` strips on
                  // the build side. Rejecting it told a tester who pasted the
                  // correct hub that it was the wrong one.
                  components.path.isEmpty || components.path == "/"
            else {
                return nil
            }

            self.host = host
            self.port = components.port ?? 443
        }
    }

    private enum Requirement {
        case unrestricted
        case exact(Origin)
        case rejectAll
    }

    private let requirement: Requirement

    private init(requirement: Requirement) {
        self.requirement = requirement
    }

    /// A nil required origin deliberately preserves the ordinary self-hosted
    /// behavior used by Debug and non-TestFlight release builds.
    init(requiredOrigin: URL?) {
        guard let requiredOrigin else {
            requirement = .unrestricted
            return
        }
        guard let origin = Origin(requiredOrigin) else {
            requirement = .rejectAll
            return
        }
        requirement = .exact(origin)
    }

    func accepts(_ endpoint: URL) -> Bool {
        switch requirement {
        case .unrestricted:
            true
        case let .exact(requiredOrigin):
            Origin(endpoint) == requiredOrigin
        case .rejectAll:
            false
        }
    }

    static func fromBundle(_ bundle: Bundle = .main) -> TestFlightEndpointPolicy {
        guard bundle.object(forInfoDictionaryKey: "DROVER_TESTFLIGHT_STAGE_ONLY") as? String == "YES" else {
            return TestFlightEndpointPolicy(requiredOrigin: nil)
        }
        guard let stagingURLString = bundle.object(
            forInfoDictionaryKey: "DROVER_TESTFLIGHT_STAGING_URL"
        ) as? String,
              let stagingURL = URL(string: stagingURLString)
        else {
            return TestFlightEndpointPolicy(requirement: .rejectAll)
        }
        return TestFlightEndpointPolicy(requiredOrigin: stagingURL)
    }
}
