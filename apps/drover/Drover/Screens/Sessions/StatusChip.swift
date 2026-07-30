import NexusKit
import SwiftUI

/// Compact per-session status chip. Colors mirror SessionRow's
/// attention tinting so the two never disagree.
struct StatusChip: View {
    let attention: AttentionState

    var body: some View {
        Text(label)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }

    private var label: String {
        switch attention {
        case .needsApproval: return "Needs approval"
        case .needsInput: return "Waiting on you"
        case .working: return "Running"
        case .done: return "Exited"
        case .errored: return "Error"
        }
    }

    private var color: Color {
        switch attention {
        case .needsApproval: return .orange
        case .needsInput: return .blue
        case .working: return .green
        case .done: return .gray
        case .errored: return .red
        }
    }
}
