import DroverKit
import SwiftUI

struct InsightsView: View {
    let client: DroverClient
    let store: CockpitStore
    @State private var state: InsightState?
    @State private var severity: InsightSeverity?
    @State private var confidence: InsightConfidence?
    @State private var analyzerClass: InsightAnalyzerClass?
    @State private var host: String?
    @State private var harness: String?
    @State private var targetType: String?

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                filterStrip
                if let error = store.insightsError {
                    Label(error, systemImage: "exclamationmark.circle")
                        .droverText(.nested)
                        .foregroundStyle(DroverColor.accentHi)
                }
                if store.isLoadingInsights {
                    ProgressView("Loading insights…")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 40)
                        .accessibilityIdentifier("insights-loading")
                } else if store.insights.isEmpty, store.insightsError == nil {
                    ContentUnavailableView(
                        "No matching insights", systemImage: "checkmark.circle",
                        description: Text("Try another filter or check again later.")
                    )
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 40)
                } else {
                    ForEach(store.insights, id: \.findingID) { insight in
                        NavigationLink {
                            InsightDetailView(client: client, store: store, summary: insight)
                        } label: {
                            InsightFeedCard(insight: insight, state: store.state(forFindingID: insight.findingID))
                        }
                        .buttonStyle(.plain)
                    }
                    if store.nextInsightsCursor != nil {
                        Button {
                            Task { await store.loadMoreInsights() }
                        } label: {
                            if store.isLoadingMoreInsights {
                                ProgressView()
                            } else {
                                Text("Load more")
                            }
                        }
                            .disabled(store.isLoadingMoreInsights)
                            .buttonStyle(.bordered)
                            .frame(maxWidth: .infinity)
                            .accessibilityIdentifier("insights-load-more")
                    }
                }
            }
            .padding(14)
        }
        .background(DroverColor.bg)
        .navigationTitle("Insights")
        .navigationBarTitleDisplayMode(.inline)
        .task { await reload() }
        .refreshable { await reload() }
    }

    private var filterStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                enumMenu("State", selection: state, values: InsightState.allFilterCases) {
                    state = $0; Task { await reload() }
                }
                enumMenu("Severity", selection: severity, values: InsightSeverity.allCases) {
                    severity = $0; Task { await reload() }
                }
                enumMenu("Confidence", selection: confidence, values: InsightConfidence.allCases) {
                    confidence = $0; Task { await reload() }
                }
                enumMenu("Analyzer", selection: analyzerClass, values: InsightAnalyzerClass.allCases) {
                    analyzerClass = $0; Task { await reload() }
                }
                TextField("Host", text: optionalBinding($host))
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 120)
                    .submitLabel(.search)
                    .onSubmit { Task { await reload() } }
                    .accessibilityLabel("Filter insights by host")
                TextField("Harness", text: optionalBinding($harness))
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 120)
                    .submitLabel(.search)
                    .onSubmit { Task { await reload() } }
                    .accessibilityLabel("Filter insights by harness")
                textMenu("Target", selection: targetType, values: availableTargets) {
                    targetType = $0; Task { await reload() }
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .accessibilityIdentifier("insights-filters")
    }

    private func enumMenu<Value: RawRepresentable>(
        _ title: String, selection: Value?, values: [Value], select: @escaping (Value?) -> Void
    ) -> some View where Value.RawValue == String {
        Menu(selection?.rawValue.capitalized ?? title) {
            Button("All \(title.lowercased())") { select(nil) }
            ForEach(values, id: \.rawValue) { value in
                Button(value.rawValue.capitalized) { select(value) }
            }
        }
    }

    private func textMenu(
        _ title: String, selection: String?, values: [String], select: @escaping (String?) -> Void
    ) -> some View {
        Menu(selection ?? title) {
            Button("All \(title.lowercased())s") { select(nil) }
            ForEach(values, id: \.self) { value in Button(value) { select(value) } }
        }
    }

    private func optionalBinding(_ value: Binding<String?>) -> Binding<String> {
        Binding(
            get: { value.wrappedValue ?? "" },
            set: {
                let trimmed = $0.trimmingCharacters(in: .whitespacesAndNewlines)
                value.wrappedValue = trimmed.isEmpty ? nil : trimmed
            }
        )
    }

    private func reload() async {
        await store.loadInsights(filters: InsightFilters(
            state: state, severity: severity, confidence: confidence,
            analyzerClass: analyzerClass, host: host, harness: harness,
            targetType: targetType
        ))
    }

    private var availableTargets: [String] {
        Array(Set(store.insights.map(\.targetType))).sorted()
    }
}

private struct InsightFeedCard: View {
    let insight: InsightSummary
    let state: InsightState?

    var body: some View {
        let value = InsightPresentation(insight: insight)
        CockpitCard {
            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 6) {
                    Text(value.severityText).droverText(.marker)
                    Text(value.sourceText).droverText(.subtitle)
                    Text(value.confidenceText).droverText(.subtitle)
                    Spacer(minLength: 6)
                    Text((state ?? insight.state).rawValue.capitalized).droverText(.subtitle)
                }
                Text(insight.title).droverText(.h2).fixedSize(horizontal: false, vertical: true)
                Text("\(insight.targetType.replacingOccurrences(of: "_", with: " ")) · \(insight.targetID)")
                    .droverText(.mono)
                    .fixedSize(horizontal: false, vertical: true)
                if let uncertainty = value.uncertaintyText {
                    Text(uncertainty).droverText(.subtitle)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(value.severityText), \(value.confidenceText), \(value.sourceText), \(insight.title), \(insight.targetID)")
        .accessibilityIdentifier("insight-\(insight.findingID)")
    }
}

private extension InsightState {
    static let allFilterCases: [Self] = [.open, .acknowledged, .dismissed, .resolved, .regressed]
}

private extension InsightSeverity {
    static let allCases: [Self] = [.critical, .high, .medium, .low]
}

private extension InsightConfidence {
    static let allCases: [Self] = [.confirmed, .likely, .speculative]
}

private extension InsightAnalyzerClass {
    static let allCases: [Self] = [.deterministic, .model]
}
