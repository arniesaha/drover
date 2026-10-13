import DroverKit
import SwiftUI

struct SessionActivityView: View {
    let activity: SessionActivityPresentation
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    @State private var showsDetails = false

    var body: some View {
        Group {
            if dynamicTypeSize.isAccessibilitySize && !telemetry.isEmpty {
                Button { showsDetails = true } label: { summary }
                    .buttonStyle(.plain)
                    .accessibilityHint("Show activity details")
            } else {
                summary
            }
        }
        .sheet(isPresented: $showsDetails) {
            NavigationStack {
                ScrollView {
                    Text(telemetry)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                }
                .navigationTitle(activity.title)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { showsDetails = false }
                    }
                }
            }
        }
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if !dynamicTypeSize.isAccessibilitySize {
                    Image(systemName: activity.isActive ? "circle.dotted" : "circle")
                        .symbolEffect(.pulse, isActive: activity.isActive && !reduceMotion)
                        .accessibilityHidden(true)
                }
                Text(activity.title)
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.primary)
                if !dynamicTypeSize.isAccessibilitySize, activity.isActive, let start = activity.startedAt, start <= Date.now {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(start, style: .timer)
                            .font(.caption.monospaced()).foregroundStyle(.secondary)
                            .monospacedDigit()
                            .fixedSize(horizontal: true, vertical: true)
                            .accessibilityLabel("Elapsed time")
                            .accessibilityValue(FoldSummary.duration(max(0, context.date.timeIntervalSince(start))))
                    }
                }
            }
            if !dynamicTypeSize.isAccessibilitySize, let detail = activity.detail, !detail.isEmpty {
                Text(detail).font(.caption.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            if !dynamicTypeSize.isAccessibilitySize, activity.isActive {
                FlowLayout(spacing: 8, lineSpacing: 4) {
                    if activity.completedSteps > 0 {
                        Label("\(activity.completedSteps) steps completed", systemImage: "checkmark")
                    }
                    if let updated = activity.updatedAt {
                        HStack(spacing: 4) {
                            Text("Last event")
                            Text(updated, format: .relative(presentation: .numeric))
                        }
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .fixedSize(horizontal: false, vertical: true)
        .background(.ultraThinMaterial.opacity(0.75), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(.secondary.opacity(0.18), lineWidth: 1)
        }
        // At accessibility sizes reserve the dock for the current phase.
        // Details and VoiceOver retain telemetry without forcing multiple
        // large rows above the composer.
        .accessibilityElement(children: dynamicTypeSize.isAccessibilitySize ? .combine : .contain)
        .accessibilityValue(dynamicTypeSize.isAccessibilitySize ? telemetry : "")
        .accessibilityIdentifier("chat-activity")
    }

    private var telemetry: String {
        var parts: [String] = []
        if let detail = activity.detail, !detail.isEmpty { parts.append(detail) }
        if activity.isActive {
            if let start = activity.startedAt {
                parts.append("Elapsed time: \(FoldSummary.duration(max(0, Date.now.timeIntervalSince(start))))")
            }
            if activity.completedSteps > 0 { parts.append("\(activity.completedSteps) steps completed") }
            if let updated = activity.updatedAt {
                parts.append("Last event: \(updated.formatted(.relative(presentation: .numeric)))")
            }
        }
        return parts.joined(separator: ", ")
    }
}
