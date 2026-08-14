import SwiftUI
import DroverKit

struct HarnessPreferenceControls: View {
    let runPreferences: HarnessModelCatalogState
    let isEditable: Bool

    @State private var showsModelPicker = false

    var body: some View {
        @Bindable var runPreferences = runPreferences
        let effort = HarnessModelPickerPresentation.effort(
            in: runPreferences.catalog,
            selectedModel: runPreferences.selectedModel,
            selectedEffort: runPreferences.thinkingEffort
        )

        HStack(spacing: 8) {
            Button {
                showsModelPicker = true
            } label: {
                PreferenceChip(
                    title: HarnessModelCatalogPresentation.modelTitle(
                        selection: runPreferences.selectedModel,
                        catalog: runPreferences.catalog
                    ),
                    systemImage: "cpu",
                    kind: "Model"
                )
            }
            .buttonStyle(.plain)

            if let effort {
                Menu {
                    ForEach(effort.choices) { choice in
                        Button(choice.title) {
                            runPreferences.thinkingEffort = choice.rawValue
                        }
                    }
                } label: {
                    PreferenceChip(
                        title: effort.title,
                        systemImage: "brain",
                        kind: "Thinking effort"
                    )
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
        .sheet(isPresented: $showsModelPicker) {
            NavigationStack {
                HarnessModelPicker(
                    runPreferences: runPreferences,
                    isEditable: isEditable
                )
            }
        }
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
