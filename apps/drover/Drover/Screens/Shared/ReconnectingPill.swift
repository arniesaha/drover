import SwiftUI

/// Thin layer-3 "reconnecting…" pill shared by chat and terminal.
/// Visibility gating (hasConnectedOnce / isConnected) stays with each
/// screen; this is presentation only.
struct ReconnectingPill: View {
    let accessibilityID: String

    var body: some View {
        HStack(spacing: 6) {
            ProgressView()
                .scaleEffect(0.7)
            Text("Reconnecting…")
                .font(.caption)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.secondary.opacity(0.15), in: Capsule())
        .accessibilityIdentifier(accessibilityID)
    }
}
