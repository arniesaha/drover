import SwiftUI

/// The inbox's frame: the header row above the list and the primary action
/// below it. Both are app-level chrome rather than anything about a session,
/// which is why they live here and not in `SessionsView`'s body — and why they
/// are separately renderable, since a `ScrollView` between them hides them
/// from any snapshot of the screen as a whole.

/// The wordmark, then the only two app-level controls there are.
///
/// It sits above the scroll view, not inside it: the theme toggle and the way
/// into Settings are the two things that must stay reachable while the list
/// below is empty, scrolled, or failing to load. Both controls are outlined
/// squares rather than bare glyphs — at this size the outline is what makes
/// them read as controls on either ground.
struct InboxChromeRow: View {
    let onToggleTheme: () -> Void
    let onOpenSettings: () -> Void

    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: 8) {
            Text("Drover")
                .font(.system(.caption2, design: .monospaced, weight: .semibold))
                .tracking(1.4)
                .textCase(.uppercase)
                .foregroundStyle(DroverColor.faint)
                .accessibilityIdentifier("drover-wordmark")

            Spacer(minLength: 8)

            ChromeButton(symbol: DroverAppearance.symbolName(displaying: colorScheme),
                         label: DroverAppearance.accessibilityLabel(displaying: colorScheme),
                         identifier: "theme-toggle",
                         action: onToggleTheme)

            ChromeButton(symbol: "slider.horizontal.3",
                         label: "Settings",
                         identifier: "settings-button",
                         action: onOpenSettings)
        }
        .padding(.horizontal, 18)
        .padding(.top, 6)
        .padding(.bottom, 2)
    }
}

private struct ChromeButton: View {
    let symbol: String
    let label: String
    let identifier: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(DroverColor.muted)
                .frame(width: 36, height: 36)
                .overlay {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(DroverColor.line, lineWidth: 1)
                }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier)
    }
}

/// The footer: one full-width primary action rather than a floating pill,
/// pinned below the list so it never scrolls away. Outlined on the ground
/// tone, never a filled bar — the system guide reserves fills for nothing at
/// this scale.
struct NewSessionBar: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Label("New Session", systemImage: "plus")
                .font(.system(.subheadline, design: .default, weight: .medium))
                .foregroundStyle(DroverColor.accentHi)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 13)
                .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(DroverColor.accent, lineWidth: 1)
                }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("launch-button")
        .padding(.horizontal, 18)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }
}

/// Finished sessions collapse to one outlined row carrying the count — a
/// card's shape at the quiet end of the ramp, so the archive never competes
/// with anything still running.
struct FinishedRow: View {
    let count: Int
    let isExpanded: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text("Finished")
                    .font(.system(.subheadline, design: .default))
                Spacer(minLength: 8)
                Text("\(count)")
                    .font(.system(.caption, design: .monospaced).monospacedDigit())
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .semibold))
                    .rotationEffect(.degrees(isExpanded ? 90 : 0))
            }
            .foregroundStyle(DroverColor.faint)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("finished-toggle")
        .accessibilityLabel("Finished, \(count)")
    }
}
