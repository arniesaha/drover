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
        .floatingChatSurface(in: Capsule())
    }
}

/// The chat dock shares the jump capsule's material, hairline and shadow.
/// Reduce Transparency replaces the material with a fully opaque surface.
struct FloatingChatSurface<S: InsettableShape>: ViewModifier {
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.forcesOpaqueChatChrome) private var forcesOpaqueChrome

    func body(content: Content) -> some View {
        content
            .background {
                if reduceTransparency || forcesOpaqueChrome {
                    shape.fill(DroverColor.sheet)
                } else {
                    shape.fill(.regularMaterial)
                }
            }
            .overlay(shape.strokeBorder(.secondary.opacity(0.18), lineWidth: 1))
            .shadow(color: .black.opacity(0.12), radius: 4, y: 2)
    }
}

extension View {
    func floatingChatSurface<S: InsettableShape>(in shape: S) -> some View {
        modifier(FloatingChatSurface(shape: shape))
    }
}

/// Fixtures can exercise the opaque path without changing simulator settings.
private struct ForcesOpaqueChatChromeKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var forcesOpaqueChatChrome: Bool {
        get { self[ForcesOpaqueChatChromeKey.self] }
        set { self[ForcesOpaqueChatChromeKey.self] = newValue }
    }
}
