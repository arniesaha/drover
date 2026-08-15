import SwiftUI
import DroverKit

/// The cold-open indicator: a spinner, shown only once the wait is long
/// enough to be worth acknowledging.
///
/// A cold open costs four serialized round trips — a newest page plus three
/// older chunks, each committed before the next is issued so a dropped request
/// costs one chunk instead of the whole window (issue #79). On loopback that is
/// ~30ms. From a phone over Tailscale it is comfortably over a second, and
/// until now the screen was simply empty for all of it.
///
/// A spinner rather than a brand mark, deliberately. The design system's
/// loading vocabulary is exactly two things — a pulse on a fold's glyph while
/// its run streams, and a spinner in the reconnecting pill — and it says in as
/// many words that product UI carries no imagery. A mascot here would have been
/// a third idiom and an exception to that rule; this is neither.
///
/// The delay is the part that carries its weight. Without it a local open,
/// which lands in tens of milliseconds, flashes a spinner on every session you
/// touch, and that reads as jank rather than as reassurance.
///
/// When to draw it is no longer decided here: `ColdOpenTracker.state` picks
/// between silence, this spinner and the unreachable state below, and it lives
/// in DroverKit because both the chat and terminal screens need the same
/// answer and because a rule about when a screen gives up deserves tests that
/// do not need a simulator (#170).
enum DroverLoadingMark {
    /// Chrome, not text — the base accent is the token tuned for icons,
    /// outlines and interface marks, and it resolves its own ramp.
    static let tint = DroverColor.accent
}

/// The cold-open state: a spinner, centred, in the accent.
struct DroverLoadingMarkView: View {
    var body: some View {
        // The platform indicator rather than a hand-rolled ring: it already
        // honours Reduce Motion and VoiceOver, and it is the same control the
        // reconnecting pill uses, so the two states look related.
        ProgressView()
            .controlSize(.large)
            .tint(DroverLoadingMark.tint)
            .accessibilityIdentifier("chat.coldOpen.mark")
            .accessibilityLabel("Catching up")
    }
}

/// The cold open's third state: the connection is not coming, said out loud.
///
/// Shaped after the inbox's own unreachable state (`SessionsView`, same
/// `ContentUnavailableView`, same title, same Retry) rather than as new
/// chrome — a phone that cannot reach the fleet should not look like three
/// different products depending on which screen was open when it happened.
struct ColdOpenFailureView: View {
    let detail: String
    let accessibilityID: String
    let onRetry: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("Can't reach the Drover server", systemImage: "wifi.exclamationmark")
        } description: {
            Text(detail)
        } actions: {
            Button("Retry", action: onRetry)
                .buttonStyle(.bordered)
                .accessibilityIdentifier("\(accessibilityID)-retry")
        }
        // Opaque on purpose: this overlays a transcript in chat and a black
        // SwiftTerm view in terminal, and the second one would otherwise read
        // as text floating on the shell.
        .background(DroverColor.bg)
        .accessibilityIdentifier(accessibilityID)
    }
}
