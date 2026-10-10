import SwiftUI
import DroverKit

struct FolderBrowserSheet: View {
    let client: DroverClient
    let hostID: String
    let hostLabel: String
    let savedPaths: [String]
    let onSelect: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var navigation: [String] = []

    var body: some View {
        NavigationStack(path: $navigation) {
            screen(path: "")
                .navigationDestination(for: String.self) { path in
                    screen(path: path)
                }
        }
    }

    private func screen(path: String) -> some View {
        FolderBrowserScreen(
            client: client, hostID: hostID, hostLabel: hostLabel,
            path: path, savedPaths: savedPaths,
            navigate: { destination in
                if let index = navigation.firstIndex(of: destination) {
                    navigation = Array(navigation.prefix(index + 1))
                } else {
                    navigation.append(destination)
                }
            },
            onSelect: { selected in
                onSelect(selected)
                dismiss()
            }
        )
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") { dismiss() }
                    .accessibilityLabel("Cancel folder selection")
            }
        }
    }
}

private struct FolderBrowserScreen: View {
    @State private var model: FolderBrowserModel
    let hostLabel: String
    let savedPaths: [String]
    let navigate: (String) -> Void
    let onSelect: (String) -> Void

    init(client: DroverClient, hostID: String, hostLabel: String, path: String,
         savedPaths: [String], navigate: @escaping (String) -> Void,
         onSelect: @escaping (String) -> Void) {
        _model = State(initialValue: FolderBrowserModel(client: client, hostID: hostID, path: path))
        self.hostLabel = hostLabel
        self.savedPaths = savedPaths
        self.navigate = navigate
        self.onSelect = onSelect
    }

    var body: some View {
        @Bindable var model = model
        List {
            Section {
                Text(hostLabel).font(.subheadline).foregroundStyle(.secondary)
                    .accessibilityLabel("Selected host: \(hostLabel)")
                if !model.path.isEmpty {
                    if model.breadcrumbs.isEmpty {
                        Text(model.path)
                            .font(.caption).foregroundStyle(.secondary)
                            .accessibilityLabel("Requested folder: \(model.path)")
                    } else {
                        ScrollView(.horizontal) {
                            HStack(spacing: 4) {
                                ForEach(model.breadcrumbs) { crumb in
                                    Button(crumb.name) { navigate(crumb.path) }
                                        .frame(minHeight: 44)
                                        .accessibilityLabel("Go to folder \(crumb.name)")
                                    if crumb.path != model.listing?.path {
                                        Image(systemName: "chevron.right").accessibilityHidden(true)
                                    }
                                }
                            }
                        }
                    }
                    if let parent = model.listing?.parent {
                        Button { navigate(parent) } label: {
                            Label("Up one folder", systemImage: "arrow.up")
                                .frame(minHeight: 44)
                        }
                        .accessibilityIdentifier("folder-browser-up")
                    }
                    TextField("Filter folders", text: $model.filter)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityLabel("Filter folders in this directory")
                        .accessibilityIdentifier("folder-browser-filter")
                }
            }

            if model.path.isEmpty, !savedPaths.isEmpty {
                Section("Recent and favorite folders") {
                    ForEach(savedPaths, id: \.self) { path in
                        folderLink(name: (path as NSString).lastPathComponent,
                                   path: path, icon: "clock", isGit: false, showsPath: true)
                    }
                }
            }

            if model.isLoading {
                Section {
                    ProgressView("Loading folders...")
                        .accessibilityIdentifier("folder-browser-loading")
                }
            }
            if let failure = model.failure {
                Section {
                    Label(failure.message, systemImage: "exclamationmark.triangle")
                        .accessibilityIdentifier("folder-browser-error")
                    Button("Retry") { Task { await model.refresh() } }
                        .accessibilityLabel("Retry loading folders")
                }
            } else if let listing = model.listing {
                if model.path.isEmpty {
                    Section("Locations") {
                        ForEach(listing.roots) { root in
                            folderLink(name: root.name, path: root.path, icon: "folder", isGit: false, showsPath: true)
                        }
                        if listing.roots.isEmpty {
                            Text("No allowed locations. Configure folder roots on this host.")
                        }
                    }
                } else {
                    Section("Folders") {
                        ForEach(model.filteredEntries) { entry in
                            folderLink(name: entry.name, path: entry.path, icon: "folder", isGit: entry.isGitRepo)
                        }
                        if model.filteredEntries.isEmpty && !model.isLoading {
                            Text(model.filter.isEmpty ? "No folders" : "No matching folders")
                                .foregroundStyle(.secondary)
                                .accessibilityIdentifier("folder-browser-empty")
                        }
                        if listing.truncated {
                            Text("Listing limited. Filter to narrow the results. Very large folders may omit matches.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }

            Section {
                Button {} label: {
                    Label("New folder (coming later)", systemImage: "folder.badge.plus")
                        .foregroundStyle(.secondary)
                }
                .disabled(true)
                .accessibilityHint("Folder creation is not available yet")
            }
        }
        .navigationTitle("Choose folder")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            if !model.path.isEmpty {
                Button {
                    if model.canSelect, let path = model.listing?.path { onSelect(path) }
                } label: {
                    Text("Use this folder").frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .padding()
                .background(.bar)
                .disabled(!model.canSelect)
                .accessibilityIdentifier("folder-browser-use")
            }
        }
        .refreshable { await model.refresh() }
        .task(id: model.filter) {
            if !model.filter.isEmpty {
                do { try await Task.sleep(for: .milliseconds(250)) }
                catch { return }
            }
            await model.refresh()
        }
    }

    private func folderLink(name: String, path: String, icon: String, isGit: Bool, showsPath: Bool = false) -> some View {
        NavigationLink(value: path) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Label(name.isEmpty ? path : name, systemImage: icon)
                    if showsPath {
                        Text(path).font(.caption).foregroundStyle(.secondary)
                            .lineLimit(2).truncationMode(.middle)
                    }
                }
                Spacer()
                if isGit {
                    Label("Git", systemImage: "arrow.triangle.branch")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .frame(minHeight: 44)
        }
        .accessibilityLabel("\(name), \(isGit ? "Git repository, " : "")folder")
        .accessibilityHint("Open \(path)")
    }
}
