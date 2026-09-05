import SwiftUI

/// The line that says a send failed or queued, sitting directly above the
/// composer.
///
/// It used to be bare `.caption` text in the raw system orange, which is a
/// dark-mode colour: ~8:1 on our dark ground and ~1.8:1 on the light one. In
/// light mode the one message telling you your turn did not go out was very
/// nearly invisible, in the strip of screen a keyboard-up layout gets looked
/// at least.
///
/// So it is given the weight of a status, not a footnote: an icon to catch
/// the eye, a tinted ground to separate it from the transcript above and the
/// composer below, and a ramp that clears the AA floor in both themes.
struct ChatHintBanner: View {
    private let hint: String
    private let actionTitle: String?
    private let onAction: (() -> Void)?

    init(_ hint: String, actionTitle: String? = nil, onAction: (() -> Void)? = nil) {
        self.hint = hint
        self.actionTitle = actionTitle
        self.onAction = onAction
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(DroverColor.warn)
                .accessibilityHidden(true)
            // Wraps rather than truncates: these carry the reason a turn did
            // not send, and a tail-truncated reason is no reason at all.
            Text(hint)
                .font(.caption)
                .foregroundStyle(DroverColor.warn)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let actionTitle, let onAction {
                Button(actionTitle, action: onAction)
                    .font(.caption.weight(.semibold))
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("chat-hint-action")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(DroverColor.warn.opacity(0.12),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .padding(.horizontal, 16)
        .padding(.bottom, 4)
        // One element, read as one sentence. Announced rather than left for
        // the user to find: it appears in response to an action they just
        // took and were told nothing else about.
        // The retry must remain a separate actionable element when present;
        // combining the row would make VoiceOver announce the label but hide
        // the button that resolves the delivery ambiguity.
        .accessibilityElement(children: actionTitle == nil ? .combine : .contain)
        .accessibilityLabel(hint)
        .accessibilityAddTraits(.isStaticText)
        .accessibilityIdentifier("chat-hint")
    }
}
