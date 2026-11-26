import DroverKit
import SwiftUI

struct AdvancedSettingsView: View {
    let client: DroverClient

    var body: some View {
        List {
            NavigationLink {
                ConfigurationReviewView(client: client)
            } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Configuration review")
                    Text("Optional AI review · findings, setup and privacy")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .accessibilityIdentifier("advanced-configuration-review")
        }
        .navigationTitle("Advanced")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
    }
}

struct ConfigurationReviewView: View {
    let client: DroverClient
    @State private var store: CockpitStore
    @State private var tab = ReviewTab.findings
    @State private var historical = false
    @State private var findings: [InsightSummary] = []
    @State private var cursor: String?
    @State private var loading = false
    @State private var error: String?
    @State private var generation = 0

    private enum ReviewTab: String, CaseIterable {
        case findings = "Findings"
        case setup = "Setup & privacy"
    }

    init(client: DroverClient) {
        self.client = client
        _store = State(initialValue: CockpitStore(client: client))
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Configuration review section", selection: $tab) {
                ForEach(ReviewTab.allCases, id: \.self) { value in
                    Text(value.rawValue).tag(value)
                }
            }
            .pickerStyle(.segmented)
            .padding()
            .accessibilityIdentifier("configuration-review-tabs")
            if tab == .findings {
                findingsTab
            } else {
                Form {
                    Section("About configuration review") {
                        Text("Optional AI review of selected agent configuration and instruction files. It does not summarize sessions. Core features and deterministic checks work independently.")
                    }
                    Section("Readiness") {
                        LabeledContent("Selected files", value: "Not verified")
                        Text("Configure allowed roots and review targets on the server. This app cannot verify which files are selected.")
                        LabeledContent("Model availability", value: "Not verified")
                        LabeledContent("Last successful review", value: "Not reported")
                        Text("Permission alone does not mean a review has run. No model is contacted by opening this page.")
                    }
                    ContentAnalysisSettings(client: client)
                }
                .accessibilityIdentifier("configuration-review-setup")
            }
        }
        .background(DroverColor.bg)
        .navigationTitle("Configuration review")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
        .task {
            do { store.updateCapability(from: try await client.snapshot()) }
            catch { /* Findings remain independently readable; actions stay gated. */ }
        }
    }

    private var findingsTab: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                Text("AI configuration review") .droverText(.h3)
                Text("Model suggestions about selected configuration files, not confirmed faults. Deterministic checks remain in Inbox → Insights.")
                    .droverText(.subtitle)
                    .fixedSize(horizontal: false, vertical: true)
                Picker("Review collection", selection: $historical) {
                    Text("Current").tag(false)
                    Text("Historical").tag(true)
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("configuration-review-collection")
                if historical {
                    Text("Retained evidence, not current action requests.").droverText(.subtitle)
                }
                if let error {
                    Label(error, systemImage: "exclamationmark.circle")
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Retry findings") { Task { await load() } }
                        .disabled(loading)
                }
                if loading { ProgressView("Loading findings…") }
                if findings.isEmpty, !loading, error == nil {
                    ContentUnavailableView(
                        historical ? "No historical AI findings" : "No AI findings yet",
                        systemImage: "doc.text.magnifyingglass",
                        description: Text("No configuration-review findings were returned. This does not prove that configured files were reviewed or that they are issue-free.")
                    )
                    Button("Review setup & privacy") { tab = .setup }
                        .accessibilityIdentifier("configuration-review-open-setup")
                }
                ForEach(findings, id: \.findingID) { finding in
                    NavigationLink {
                        InsightDetailView(client: client, store: store, summary: finding)
                    } label: {
                        InsightFeedCard(insight: finding, state: store.state(forFindingID: finding.findingID))
                    }
                    .buttonStyle(.plain)
                }
                if cursor != nil {
                    Button("Load more findings") { Task { await load(more: true) } }
                        .disabled(loading)
                }
            }
            .padding(14)
            .padding(.bottom, DroverRootPageLayout.dockClearance)
        }
        .accessibilityIdentifier("configuration-review-findings")
        .task(id: historical) { await load() }
        .refreshable { await load() }
    }

    private func load(more: Bool = false) async {
        if more, loading { return }
        generation += 1
        let requestGeneration = generation
        let next = more ? cursor : nil
        if !more { findings = []; cursor = nil }
        loading = true
        error = nil
        defer { if requestGeneration == generation { loading = false } }
        do {
            let page = try await client.insights(filters: InsightFilters(
                analyzerClass: .model, cursor: next, historical: historical
            ))
            guard requestGeneration == generation, !Task.isCancelled else { return }
            // Model class alone is not sufficient provenance for configuration review.
            let matches = page.findings.filter {
                $0.analyzerClass == .model && $0.analyzerID == "model.configuration"
            }
            let existing = Set(findings.map(\.findingID))
            findings.append(contentsOf: matches.filter { !existing.contains($0.findingID) })
            cursor = page.nextCursor
        } catch DroverError.busy {
            guard requestGeneration == generation else { return }
            self.error = "The server is busy. Retained findings are unchanged. Try again shortly, or open Setup & privacy."
        } catch is CancellationError {
            return
        } catch {
            guard requestGeneration == generation else { return }
            self.error = "Could not load review findings. Your retained evidence has not been removed. Check the connection and retry."
        }
    }
}
