import SwiftUI
import NexusKit

/// The "blocked on you" slot, pinned above the composer.
///
/// Today it holds one thing — a tool approval — but it is deliberately shaped
/// as a slot rather than an approval banner, because sign-in-required and
/// signed-out are the same sentence to the reader: the harness has stopped and
/// wants a decision. Adding those variants is a case here, not a new surface.
///
/// Both actions are outlined. The system guide reserves fills entirely ("the
/// primary is an accent outline, never a fill"), and this is a mono palette —
/// a green Approve beside a red Deny would be the only saturated thing on the
/// screen and would encode by hue what the layout already says by position.
struct DecisionBlock: View {
    let approval: HarnessMessage
    /// True while a decision is already in flight — disables both actions so
    /// a slow network can't collect a double-submission.
    let isBusy: Bool
    let onApprove: () -> Void
    let onDeny: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Image(systemName: "hand.raised.fill")
                    .font(.system(size: 11, weight: .medium))
                Text("Approval needed")
            }
            .droverText(.h3)
            .foregroundStyle(DroverColor.accentHi)

            Text(toolName)
                .droverText(.body)

            if let inputSummary, !inputSummary.isEmpty {
                // The thing being approved is almost always a command or a
                // path — a machine string, so it gets the artifact treatment
                // rather than being allowed to wrap into prose.
                Text(inputSummary)
                    .droverText(.mono)
                    .lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 7)
                    .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(DroverColor.line, lineWidth: 1)
                    }
            }

            HStack(spacing: 9) {
                action("Deny", tint: DroverColor.line, label: DroverColor.muted, run: onDeny)
                    .accessibilityIdentifier("approval-deny")
                action("Allow once", tint: DroverColor.accent, label: DroverColor.accentHi, run: onApprove)
                    .accessibilityIdentifier("approval-allow")
            }
            .disabled(isBusy)
            .opacity(isBusy ? 0.45 : 1)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverColor.accentTint, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(DroverColor.accent, lineWidth: 1)
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
    }

    private func action(
        _ title: String, tint: PaletteToken, label: PaletteToken, run: @escaping () -> Void
    ) -> some View {
        Button(action: run) {
            Text(title)
                .font(.system(.subheadline, design: .default, weight: .medium))
                .foregroundStyle(label)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 9)
                .overlay { Capsule().strokeBorder(tint, lineWidth: 1) }
        }
        .buttonStyle(.plain)
    }

    private var toolName: String {
        approval.payload["tool"]?.stringValue ?? "The harness is waiting on a decision"
    }

    private var inputSummary: String? {
        approval.payload["input"]?.displayString
    }
}
