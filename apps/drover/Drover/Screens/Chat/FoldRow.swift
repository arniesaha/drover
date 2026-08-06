import SwiftUI

/// The one collapsed-row treatment every fold in the transcript wears.
///
/// There are exactly three fold species — tool steps, thinking runs and
/// status runs — and giving them one container is what makes a long
/// transcript read as a few fold rows plus full-size answers instead of four
/// unrelated widgets. One radius, one hairline, one summary line, one caret.
struct FoldRow<Content: View>: View {
    let systemImage: String
    let summary: String
    var accessibilityIdentifier: String
    /// The newest run is still streaming into this row; the icon pulses and
    /// the row can't be mistaken for a finished one.
    var isStreaming: Bool = false
    @ViewBuilder var content: () -> Content

    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: systemImage)
                        .font(.system(size: 11, weight: .medium))
                        .symbolEffect(.pulse, isActive: isStreaming)
                    Text(summary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 9, weight: .semibold))
                        .rotationEffect(.degrees(isExpanded ? 180 : 0))
                }
                .droverText(.subtitle)
                .padding(.horizontal, 11)
                .padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier(accessibilityIdentifier)
            .accessibilityLabel(summary)
            .accessibilityHint(isExpanded ? "Collapse" : "Expand")

            if isExpanded {
                VStack(alignment: .leading, spacing: 7) {
                    content()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 11)
                .padding(.bottom, 10)
                .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(DroverColor.line, lineWidth: 1)
        }
    }
}
