import SwiftUI
import DroverKit

struct HarnessPreferenceControls: View {
    let harness: String
    let isEditable: Bool
    @Binding var selectedModel: String
    @Binding var thinkingEffort: String

    var body: some View {
        HStack(spacing: 8) {
            Menu {
                Button("Default") { selectedModel = "" }
                ForEach(HarnessRunPreferences.modelSuggestions(for: harness), id: \.self) { model in
                    Button(model) { selectedModel = model }
                }
            } label: {
                PreferenceChip(title: modelLabel, systemImage: "cpu", kind: "Model")
            }
            .buttonStyle(.plain)

            if HarnessRunPreferences.supportsThinkingEffort(harness) {
                Menu {
                    Button("Default") { thinkingEffort = "" }
                    ForEach(HarnessRunPreferences.thinkingEfforts, id: \.self) { effort in
                        Button(effort.capitalized) { thinkingEffort = effort }
                    }
                } label: {
                    PreferenceChip(title: thinkingLabel, systemImage: "brain", kind: "Thinking effort")
                }
                .buttonStyle(.plain)
            }

            if !isEditable {
                Image(systemName: "lock.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Run preferences locked for this session")
            }
        }
        .layoutPriority(1)
        .disabled(!isEditable)
        .opacity(isEditable ? 1 : 0.7)
    }

    private var modelLabel: String {
        selectedModel.isEmpty ? "Default" : selectedModel
    }

    private var thinkingLabel: String {
        thinkingEffort.isEmpty ? "Auto" : thinkingEffort.capitalized
    }
}

private struct PreferenceChip: View {
    let title: String
    let systemImage: String
    let kind: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage)
                .imageScale(.medium)
            Text(title)
                .lineLimit(1)
                .minimumScaleFactor(0.82)
                .allowsTightening(true)
        }
        .font(.callout.weight(.medium))
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(.secondary.opacity(0.18)))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(kind): \(title)")
    }
}
