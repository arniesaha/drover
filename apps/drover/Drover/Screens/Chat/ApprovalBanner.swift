import SwiftUI
import NexusKit

/// Pinned above the composer whenever `ChatModel.pendingApproval != nil`:
/// the tool name, a compact input summary, and Approve/Deny actions. Purely
/// presentational — the actual `approve(_:)` call lives on `ChatModel`.
struct ApprovalBanner: View {
    let approval: HarnessMessage
    /// True while a decision is already in flight — disables both buttons
    /// so a slow network can't collect a double-submission.
    let isBusy: Bool
    let onApprove: () -> Void
    let onDeny: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(toolName, systemImage: "hand.raised.fill")
                .font(.headline)

            if let inputSummary, !inputSummary.isEmpty {
                Text(inputSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }

            HStack {
                Button(role: .destructive, action: onDeny) {
                    Text("Deny").frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(.red)

                Button(action: onApprove) {
                    Text("Approve").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
            }
            .disabled(isBusy)
        }
        .padding(12)
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.orange.opacity(0.4)))
        .padding(.horizontal)
        .padding(.vertical, 8)
    }

    private var toolName: String {
        approval.payload["tool"]?.stringValue ?? "Approval needed"
    }

    private var inputSummary: String? {
        approval.payload["input"]?.displayString
    }
}
