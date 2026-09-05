import Foundation
import Testing
@testable import Drover

/// The only two URLs the app hands to the system browser. A typo is a dead
/// end for someone trying to read the privacy policy before they trust the
/// app with a token, and nothing else in the build would catch it.
struct SettingsSupportLinkTests {
    @Test func supportLinksPointAtThePublishedDocs() {
        #expect(
            SettingsView.privacyURL.absoluteString
                == "https://github.com/arniesaha/drover/blob/main/docs/privacy.md"
        )
        #expect(
            SettingsView.supportURL.absoluteString
                == "https://github.com/arniesaha/drover/blob/main/docs/support.md"
        )
    }

    @Test func supportLinksAreHttpsAndCarryAHost() {
        for url in [SettingsView.privacyURL, SettingsView.supportURL] {
            #expect(url.scheme == "https")
            #expect(url.host() == "github.com")
            #expect(url.pathExtension == "md")
        }
    }
}
