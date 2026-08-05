import SwiftUI
import PhotosUI
import NexusKit

/// The "new session" sheet: host/harness pickers, a cwd field with a
/// suggestions menu, and (for structured harnesses only) a starting prompt.
/// All defaulting/validation/network logic lives in `LaunchModel` — this view
/// only renders it and reports the launched session back to the caller.
struct LaunchView: View {
    @State private var model: LaunchModel
    @Environment(\.dismiss) private var dismiss
    @State private var isLaunching = false
    @State private var showAuth = false
    @State private var pickerItems: [PhotosPickerItem] = []
    private let client: NexusClient
    private static let maxCombinedBytes = 6 * 1024 * 1024

    /// Called once `launch()` succeeds, with the new session's id and
    /// whether it's structured (so the caller knows Chat vs. terminal), plus
    /// the selected harness for the destination title/icon.
    let onLaunched: (String, Bool, String) -> Void

    init(client: NexusClient, snapshot: HarnessSnapshot?, onLaunched: @escaping (String, Bool, String) -> Void) {
        _model = State(initialValue: LaunchModel(client: client, snapshot: snapshot))
        self.client = client
        self.onLaunched = onLaunched
    }

    var body: some View {
        @Bindable var model = model

        Form {
            Section("Host") {
                Picker("Host", selection: $model.hostID) {
                    ForEach(model.availableHosts) { host in
                        Text(host.title)
                            .tag(host.id)
                    }
                }
            }

            Section("Harness") {
                Picker("Harness", selection: $model.harness) {
                    ForEach(model.availableHarnesses, id: \.self) { name in
                        Text(name).tag(name)
                    }
                }

                if model.supportsInteractiveAuth {
                    Button {
                        showAuth = true
                    } label: {
                        Label("Sign in to \(model.harness)", systemImage: "person.badge.key")
                    }
                    .disabled(model.hostID.isEmpty)
                }
            }

            Section("Working directory") {
                HStack {
                    TextField("cwd (optional)", text: $model.cwd)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()

                    if !model.cwdSuggestions.isEmpty {
                        Menu {
                            ForEach(model.cwdSuggestions, id: \.self) { suggestion in
                                Button(suggestion) { model.cwd = suggestion }
                            }
                        } label: {
                            Image(systemName: "clock.arrow.circlepath")
                        }
                    }
                }
            }

            if model.isStructured {
                Section("Starting prompt") {
                    HarnessPreferenceControls(
                        harness: model.harness,
                        selectedModel: $model.selectedModel,
                        thinkingEffort: $model.thinkingEffort
                    )

                    if !model.promptAttachments.isEmpty {
                        attachmentStrip
                    }

                    TextEditor(text: $model.prompt)
                        .frame(minHeight: 100)

                    PhotosPicker(selection: $pickerItems, maxSelectionCount: 4,
                                 matching: .images) {
                        Label("Attach image", systemImage: "paperclip")
                    }
                    .accessibilityIdentifier("launch-attach")
                }
            }

            if let launchError = model.launchError {
                Section {
                    Text(launchError).foregroundStyle(.red)
                }
            }

            Section {
                Button {
                    Task { await launch() }
                } label: {
                    if isLaunching {
                        ProgressView()
                    } else {
                        Text("Launch")
                    }
                }
                .disabled(isLaunching || model.hostID.isEmpty || model.harness.isEmpty)
                .accessibilityIdentifier("launch-confirm-button")
            }
        }
        .navigationTitle("New Session")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") { dismiss() }
            }
        }
        .sheet(isPresented: $showAuth) {
            NavigationStack {
                HarnessAuthSheet(client: client, hostID: model.hostID, harness: model.harness)
            }
        }
        .onChange(of: pickerItems) { _, items in
            guard !items.isEmpty else { return }
            pickerItems = []
            Task { await load(items) }
        }
    }

    private var attachmentStrip: some View {
        @Bindable var model = model

        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(model.promptAttachments.enumerated()), id: \.offset) { index, attachment in
                    thumbnail(for: attachment)
                        .overlay(alignment: .topTrailing) {
                            Button {
                                model.promptAttachments.remove(at: index)
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .font(.caption)
                                    .foregroundStyle(.white, .black.opacity(0.6))
                            }
                            .accessibilityLabel("Remove attachment")
                        }
                        .accessibilityIdentifier("launch-attachment")
                }
            }
        }
    }

    @ViewBuilder
    private func thumbnail(for attachment: TurnAttachment) -> some View {
        if let image = UIImage(data: attachment.data) {
            Image(uiImage: image)
                .resizable()
                .scaledToFill()
                .frame(width: 44, height: 44)
                .clipShape(RoundedRectangle(cornerRadius: 8))
        } else {
            Image(systemName: "photo")
                .frame(width: 44, height: 44)
                .background(.secondary.opacity(0.2),
                            in: RoundedRectangle(cornerRadius: 8))
        }
    }

    private func load(_ items: [PhotosPickerItem]) async {
        @Bindable var model = model

        for item in items {
            guard let raw = try? await item.loadTransferable(type: Data.self),
                  let jpeg = ImageDownscaler.jpegData(from: raw) else { continue }
            let combined = model.promptAttachments.reduce(0) { $0 + $1.data.count }
            guard combined + jpeg.count <= Self.maxCombinedBytes else { continue }
            model.promptAttachments.append(TurnAttachment(mediaType: "image/jpeg", data: jpeg))
        }
    }

    private func launch() async {
        isLaunching = true
        defer { isLaunching = false }
        guard let sessionID = await model.launch() else { return }
        onLaunched(sessionID, model.isStructured, model.harness)
        dismiss()
    }
}
