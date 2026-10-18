import Foundation
import Testing
@testable import Drover

/// The only two URLs the app hands to the system browser.
///
/// `@MainActor` because `SettingsView` is a SwiftUI `View` and so is
/// main-actor isolated, which makes its statics isolated too. Without it
/// this compiles under a lenient local toolchain and fails on CI with
/// "main actor-isolated static property ... can not be referenced from a
/// nonisolated context". A typo is a dead
/// end for someone trying to read the privacy policy before they trust the
/// app with a token, and nothing else in the build would catch it.
@MainActor
struct SettingsSupportLinkTests {
    @Test func supportLinksPointAtThePublishedDocs() {
        #expect(
            SettingsView.privacyURL.absoluteString
                == "https://drover.fyi/privacy"
        )
        #expect(
            SettingsView.supportURL.absoluteString
                == "https://drover.fyi/support"
        )
    }

    @Test func supportLinksAreHttpsAndCarryAHost() {
        for url in [SettingsView.privacyURL, SettingsView.supportURL] {
            #expect(url.scheme == "https")
            #expect(url.host() == "drover.fyi")
        }
        #expect(SettingsView.privacyURL.path == "/privacy")
        #expect(SettingsView.supportURL.path == "/support")
    }
}
