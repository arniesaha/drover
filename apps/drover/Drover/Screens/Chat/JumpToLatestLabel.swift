import SwiftUI

/// Keep the floating control small even when a text label would fill the
/// transcript. The owning button supplies its full spoken label and count.
struct JumpToLatestLabel: View {
    let newEventCount: Int
    let hasUnseenContent: Bool
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.down")
            if !dynamicTypeSize.isAccessibilitySize {
                Text("Jump to latest")
                    .lineLimit(1)
                if newEventCount > 0 {
                    Text("\(newEventCount)").monospacedDigit()
                }
            }
            if (dynamicTypeSize.isAccessibilitySize && newEventCount > 0)
                || (newEventCount == 0 && hasUnseenContent) {
                Circle().frame(width: 6, height: 6).accessibilityHidden(true)
            }
        }
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(.primary)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .frame(minWidth: 44, minHeight: 44)
        .background(.ultraThinMaterial.opacity(0.75), in: Capsule())
        .overlay(Capsule().strokeBorder(.secondary.opacity(0.18), lineWidth: 1))
        .shadow(color: .black.opacity(0.12), radius: 4, y: 2)
    }
}
