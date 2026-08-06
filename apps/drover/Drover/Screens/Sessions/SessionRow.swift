import SwiftUI
import NexusKit

/// One card in the fleet inbox. Two species share this view because they
/// share a shape — kicker, one loud line, one quiet line, one verb — and
/// differ only in what fills them. `SessionCardPresentation` decides all of
/// that; this file is layout and state *form*.
///
/// State is carried by form, not hue: the dot is a filled disc when the
/// session wants you, a hollow ring while it works, and a faint ring once it
/// has finished. That is deliberate — a pale fill disappears on a light
/// ground, which is why there is one accent here and no traffic-light palette.
struct SessionRow: View {
    let session: SessionSummary
    let hostTitle: String

    var body: some View {
        let card = SessionCardPresentation(session: session, hostTitle: hostTitle)

        HStack(alignment: .top, spacing: 11) {
            StateDot(attention: session.attention)
                .padding(.top, 5)

            VStack(alignment: .leading, spacing: 5) {
                // The timestamp rides the kicker row, not the title row.
                // Kickers are short and truncate gracefully; a two-line title
                // competing for the same width starved the date down to "3…".
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    if let kicker = card.kicker {
                        Image(systemName: card.harness.symbolName)
                            .font(.system(size: 10, weight: .medium))
                        Text(kicker)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    Spacer(minLength: 8)

                    if let activityDate = session.activityDate {
                        Text(activityDate, format: .relative(presentation: .numeric))
                            .lineLimit(1)
                            .fixedSize()
                    }
                }
                .droverText(.mono)
                .accessibilityElement(children: .combine)

                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let sigil = card.sigil {
                        Text(sigil).droverText(.marker)
                    }
                    Text(card.title)
                        .droverText(.body)
                        // A placeholder title is a statement about the session,
                        // not something the session said — it steps back to the
                        // quiet ramp so it can't out-shout a real one.
                        .foregroundStyle(card.isTitlePlaceholder ? DroverColor.muted : DroverColor.text)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)

                    Spacer(minLength: 0)
                }

                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(card.subtitle)
                        .droverText(.subtitle)
                        .lineLimit(1)
                        .truncationMode(.tail)

                    Spacer(minLength: 8)

                    // Outlined, never filled — the system guide is explicit
                    // that a primary action is an accent outline.
                    Text(card.action.rawValue)
                        .font(.system(.caption, design: .default, weight: .medium))
                        .foregroundStyle(isWaiting ? DroverColor.accentHi : DroverColor.muted)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                        .overlay {
                            Capsule().strokeBorder(
                                isWaiting ? AnyShapeStyle(DroverColor.accent) : AnyShapeStyle(DroverColor.line),
                                lineWidth: 1
                            )
                        }
                        .fixedSize()
                }
                .padding(.top, 1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 11)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Elevation on a dark ground is an edge plus the surface step, never a
        // stacked shadow.
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(DroverColor.line, lineWidth: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(card.title). \(card.subtitle)")
        .accessibilityHint("\(card.action.rawValue) this session")
    }

    private var isWaiting: Bool {
        session.attention == .needsApproval || session.attention == .needsInput
    }
}

/// Session and host state as *form*: filled = wants you, hollow ring =
/// working, faint ring = finished.
struct StateDot: View {
    let attention: AttentionState
    var diameter: CGFloat = 7

    var body: some View {
        Group {
            switch attention {
            case .needsApproval, .needsInput:
                Circle().fill(DroverColor.accent)
            case .working:
                Circle().strokeBorder(DroverColor.accentHi, lineWidth: 1.5)
            case .done, .errored:
                Circle().strokeBorder(DroverColor.line, lineWidth: 1)
            }
        }
        .frame(width: diameter, height: diameter)
        .accessibilityHidden(true)
    }
}
