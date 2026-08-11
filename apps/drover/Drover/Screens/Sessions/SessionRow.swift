import SwiftUI
import DroverKit

/// One card in the fleet inbox. Two species share this view because they
/// share a shape — kicker, one loud line, one quiet line, one verb — and
/// differ only in what fills them. `SessionCardPresentation` decides all of
/// that; this file is layout and state *form*.
///
/// State is carried by form, not hue: the dot is a filled disc when the
/// session wants you, a hollow ring while it works, and a faint ring once it
/// has finished. That is deliberate — a pale fill disappears on a light
/// ground, which is why there is one accent here and no traffic-light palette.
///
/// Staleness is carried the same way. When the hub is unreachable the inbox
/// keeps rendering the last-known snapshot — right, and it stays — but a card
/// drawn from it used to look identical to a live one, down to a relative
/// timestamp that kept counting up against *now* while the data underneath it
/// was frozen (#81). A stale card breaks its dot to a dashed ring, freezes
/// that timestamp against the snapshot, and trades its verb for the snapshot's
/// own age. Same statement the provider capacity strip has always made.
struct SessionRow: View {
    let session: SessionSummary
    let hostTitle: String
    /// How far behind the snapshot this card was drawn from is. The default is
    /// "never stale", so previews and any caller without a store are unchanged.
    var freshness: SnapshotFreshness = .live

    var body: some View {
        let card = SessionCardPresentation(
            session: session,
            hostTitle: hostTitle,
            freshness: freshness
        )

        HStack(alignment: .top, spacing: 11) {
            StateDot(attention: session.attention, isStale: card.isStale)
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

                    // A stale card's timestamp is a static string measured
                    // against the snapshot, not a live formatter measured
                    // against now. The ticking version is the whole deception:
                    // `lastActivity` is frozen inside the snapshot while the
                    // formatter recomputes every render, so a card nobody has
                    // refreshed in ten minutes still counts up like live data.
                    if let frozen = card.frozenActivityText {
                        Text(frozen)
                            .lineLimit(1)
                            .fixedSize()
                    } else if let activityDate = session.activityDate {
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

                    if let action = card.action {
                        // Outlined, never filled — the system guide is explicit
                        // that a primary action is an accent outline.
                        Text(action.rawValue)
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
                    } else if let note = card.staleNote {
                        // Deliberately not a capsule: the outline is what says
                        // "this is a thing you do", and there is nothing here
                        // to do. It occupies the same slot at the same padding
                        // so the card's height does not move when a link drops.
                        Text(note)
                            .font(.system(.caption, design: .default, weight: .medium))
                            .foregroundStyle(DroverColor.faint)
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .fixedSize()
                            .accessibilityIdentifier("session-stale-note")
                    }
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
        .accessibilityLabel(accessibilityLabel(for: card))
        .accessibilityHint(accessibilityHint(for: card))
    }

    /// A dashed ring is invisible to VoiceOver, so staleness is said out loud
    /// — before the state phrase it is qualifying, not after it.
    private func accessibilityLabel(for card: SessionCardPresentation) -> String {
        guard let note = card.staleNote else {
            return "\(card.title). \(card.subtitle)"
        }
        return "\(note). \(card.title). \(card.subtitle)"
    }

    private func accessibilityHint(for card: SessionCardPresentation) -> String {
        guard let action = card.action else {
            return "Showing last reported state. Open to load live state."
        }
        return "\(action.rawValue) this session"
    }

    private var isWaiting: Bool {
        session.attention == .needsApproval || session.attention == .needsInput
    }
}

/// Session and host state as *form*: filled = wants you, hollow ring =
/// working, faint ring = finished — and a broken ring when the state itself is
/// last-known rather than current.
struct StateDot: View {
    let attention: AttentionState
    /// Overrides the state form entirely rather than tinting it. A stale card's
    /// `attention` is the field we cannot vouch for, so drawing it as a
    /// confident filled disc would be asserting the very thing in doubt.
    var isStale: Bool = false
    var diameter: CGFloat = 7

    var body: some View {
        Group {
            if isStale {
                // A broken ring, at the working ring's weight: 7pt of dashed
                // hairline reads as nothing at all.
                Circle().strokeBorder(
                    DroverColor.muted,
                    style: StrokeStyle(lineWidth: 1.5, dash: [2, 2])
                )
            } else {
                switch attention {
                case .needsApproval, .needsInput:
                    Circle().fill(DroverColor.accent)
                case .working:
                    Circle().strokeBorder(DroverColor.accentHi, lineWidth: 1.5)
                case .done, .errored:
                    Circle().strokeBorder(DroverColor.line, lineWidth: 1)
                }
            }
        }
        .frame(width: diameter, height: diameter)
        .accessibilityHidden(true)
    }
}
