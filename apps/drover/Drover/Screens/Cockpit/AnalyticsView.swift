import DroverKit
import SwiftUI

struct AnalyticsView: View {
    let store: CockpitStore
    @State private var days = 7
    @State private var host: String?
    @State private var harness: String?
    @State private var provider: String?
    @State private var model: String?
    @State private var project: String?

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                filterStrip

                if let error = store.analyticsError {
                    Label(error, systemImage: "exclamationmark.circle")
                        .droverText(.nested)
                        .foregroundStyle(DroverColor.accentHi)
                }

                if let snapshot = store.analytics {
                    providerSection(snapshot)
                    observedSection(snapshot)
                } else if store.analyticsError == nil {
                    ProgressView("Loading analytics…")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 50)
                }
            }
            .padding(14)
        }
        .background(DroverColor.bg)
        .navigationTitle("Analytics")
        .navigationBarTitleDisplayMode(.inline)
        .task { await reload() }
        .refreshable { await reload() }
    }

    private var filterStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                Menu("\(days) days") {
                    ForEach([1, 7, 14, 30, 90, 365], id: \.self) { value in
                        Button("\(value) days") { days = value; Task { await reload() } }
                    }
                }
                AnalyticsFilterMenu(title: "Host", selection: host, values: hostValues) {
                    host = $0; Task { await reload() }
                }
                AnalyticsFilterMenu(title: "Harness", selection: harness, values: harnessValues) {
                    harness = $0; Task { await reload() }
                }
                AnalyticsFilterMenu(title: "Provider", selection: provider, values: providerValues) {
                    provider = $0; Task { await reload() }
                }
                AnalyticsFilterMenu(title: "Model", selection: model, values: modelValues) {
                    model = $0; Task { await reload() }
                }
                AnalyticsFilterMenu(title: "Project", selection: project, values: projectValues) {
                    project = $0; Task { await reload() }
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .accessibilityIdentifier("analytics-filters")
    }

    @ViewBuilder
    private func providerSection(_ snapshot: AnalyticsSnapshot) -> some View {
        let accounts = snapshot.providerCapacity.data ?? []
        let section = ProviderSectionPresentation(
            status: snapshot.providerCapacity.status,
            hasRetainedValues: !accounts.isEmpty
        )
        if !accounts.isEmpty || snapshot.providerCapacity.status != .ok {
            VStack(alignment: .leading, spacing: 10) {
                CockpitSectionHeading(title: "Subscriptions", source: "Provider reported", action: nil)
                if let warning = section.warningText {
                    CockpitCard {
                        Label(warning, systemImage: "gauge.with.dots.needle.33percent")
                            .droverText(.nested)
                            .fixedSize(horizontal: false, vertical: true)
                        if let observedAt = snapshot.providerCapacity.observedAt {
                            Text("Last section update \(observedAt.formatted(date: .abbreviated, time: .shortened))")
                                .droverText(.subtitle)
                        }
                    }
                    .accessibilityIdentifier("analytics-provider-warning")
                }
                if !accounts.isEmpty {
                    ForEach(accounts, id: \.snapshotID) { account in
                        CockpitCard {
                            VStack(alignment: .leading, spacing: 5) {
                                HStack(alignment: .firstTextBaseline) {
                                    Text("\(account.provider.capitalized) · \(account.accountLabel)")
                                        .droverText(.h2)
                                    Spacer(minLength: 8)
                                    Text(section.accountStatusText(accountStatus: account.status))
                                        .droverText(.marker)
                                }
                                ForEach(Array(account.windows.enumerated()), id: \.offset) { _, window in
                                    let value = ProviderCapacityPresentation(
                                        account: account, window: window, now: .now
                                    )
                                    Text("\(window.kind.capitalized): \(value.remainingText) · \(value.resetText)")
                                        .droverText(.nested)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }
                }
            }
            .accessibilityIdentifier("analytics-provider-reported")
        }
    }

    @ViewBuilder
    private func observedSection(_ snapshot: AnalyticsSnapshot) -> some View {
        if let activity = snapshot.activity.data {
            VStack(alignment: .leading, spacing: 10) {
                CockpitSectionHeading(title: "Observed usage", source: "Drover observed", action: nil)
                CockpitCard {
                    HStack(alignment: .top, spacing: 16) {
                        analyticsMetric(format(activity.totals.sessionCount), "Sessions")
                        analyticsMetric(format(activity.totals.totalTokens), "Tokens")
                        analyticsMetric(currency(activity.totals.costUSD), "API cost")
                    }
                }

                if !activity.projects.isEmpty {
                    Text("Projects").droverText(.h3)
                    ForEach(activity.projects, id: \.projectKey) { value in
                        CockpitCard {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(value.projectKey).droverText(.h2)
                                Text("\(format(value.totalTokens)) tokens · \(value.sessionCount) sessions")
                                    .droverText(.body)
                                Text("Harnesses: \(value.harnesses.joined(separator: ", "))")
                                    .droverText(.nested)
                                Text("Hosts: \(value.hosts.joined(separator: ", "))")
                                    .droverText(.nested)
                                Text(tokenCoverage(activity))
                                    .droverText(.subtitle)
                            }
                        }
                    }
                }
            }
            .accessibilityIdentifier("analytics-drover-observed")
        }
    }

    private func analyticsMetric(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).droverText(.h2).monospacedDigit()
            Text(label).droverText(.subtitle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func tokenCoverage(_ activity: ActivitySummary) -> String {
        activity.coverage.tokenPercent.map { "\(formatPercent($0))% token coverage" }
            ?? "Token coverage unavailable"
    }

    private func reload() async {
        await store.loadAnalytics(filters: AnalyticsFilters(
            days: days, hostID: host, harness: harness, provider: provider,
            model: model, projectKey: project
        ))
    }

    private var activity: ActivitySummary? { store.analytics?.activity.data }
    private var hostValues: [String] { activity?.hosts.map(\.key).sorted() ?? [] }
    private var harnessValues: [String] { activity?.harnesses.map(\.key).sorted() ?? [] }
    private var modelValues: [String] { activity?.models.map(\.key).sorted() ?? [] }
    private var projectValues: [String] { activity?.projects.map(\.projectKey).sorted() ?? [] }
    private var providerValues: [String] {
        Array(Set((store.analytics?.providerCapacity.data ?? []).map(\.provider))).sorted()
    }
}

private struct AnalyticsFilterMenu: View {
    let title: String
    let selection: String?
    let values: [String]
    let select: (String?) -> Void

    var body: some View {
        Menu(selection ?? title) {
            Button("All \(title.lowercased())s") { select(nil) }
            ForEach(values, id: \.self) { value in
                Button(value) { select(value) }
            }
        }
    }
}
