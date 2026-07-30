import SwiftUI

/// Persistent layer-1 banner: the hub is unreachable but the list keeps
/// rendering last-known state beneath it. Auto-retry continues via the
/// store's poll loop; the button is the manual path.
struct UnreachableBanner: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Label(message, systemImage: "wifi.exclamationmark")
                .font(.footnote)
                .lineLimit(2)
            Spacer()
            Button("Retry", action: retry)
                .font(.footnote.weight(.semibold))
                .buttonStyle(.bordered)
                .controlSize(.mini)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.orange.opacity(0.15))
        .accessibilityIdentifier("hub-unreachable-banner")
    }
}
