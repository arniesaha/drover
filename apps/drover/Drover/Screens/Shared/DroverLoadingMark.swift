import SwiftUI

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
enum DroverLoadingMark {
    /// Nothing is drawn before this.
    static let appearAfter: TimeInterval = 0.25

    /// Chrome, not text — the base accent is the token tuned for icons,
    /// outlines and interface marks, and it resolves its own ramp.
    static let tint = DroverColor.accent

    /// Cold open only. Reconnects belong to `ReconnectingPill`, which is why
    /// this is done for good once a session has ever attached.
    static func shouldShow(hasConnectedOnce: Bool, elapsed: TimeInterval) -> Bool {
        guard !hasConnectedOnce else { return false }
        return elapsed >= appearAfter
    }
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
