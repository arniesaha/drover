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
    /// Which number every distribution list is ordered by. One control for all
    /// of them: comparing "ranked by sessions" in one section against "ranked
    /// by tokens" in the next is exactly the confusion this is meant to remove.
    @State private var rank: DistributionRank = .sessions

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                filterStrip

                if let notice = store.analyticsRefreshNotice {
                    AnalyticsRefreshBanner(message: notice)
                }

                if let error = store.analyticsError {
                    Label(error, systemImage: "exclamationmark.circle")
                        .droverText(.nested, accented: true)
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

    /// Wraps rather than scrolling sideways: a filter parked off the right
    /// edge is one nobody knows exists, and at accessibility text sizes three
    /// chips can already exceed the screen width on their own.
    private var filterStrip: some View {
        FlowLayout(spacing: 8, lineSpacing: 8) {
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
        .buttonStyle(.bordered)
        .controlSize(.small)
        .accessibilityIdentifier("analytics-filters")
    }

    @ViewBuilder
    private func providerSection(_ snapshot: AnalyticsSnapshot) -> some View {
        let accounts = snapshot.providerCapacity.data ?? []
        let section = ProviderSectionPresentation(
            status: snapshot.providerCapacity.status,
            // Without this the server's actual reason never reaches the user
            // here, unlike ProviderCapacitySection, and every failure reads as
            // the same generic sentence.
            message: store.analyticsError,
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
                    // One row per subscription, not per host — the same Codex
                    // account signed in on three machines was three identical
                    // rows with nothing to tell them apart.
                    ForEach(ProviderSubscriptionGrouping.group(accounts)) { subscription in
                        CockpitCard {
                            VStack(alignment: .leading, spacing: 5) {
                                HStack(alignment: .firstTextBaseline) {
                                    Text(subscription.title)
                                        .droverText(.h2)
                                    Spacer(minLength: 8)
                                    Text(section.accountStatusText(accountStatus: subscription.status))
                                        .droverText(.marker)
                                }
                                Text(subscription.hostsText)
                                    .droverText(.subtitle)
                                // Every window, with a bar each. The cockpit
                                // card shows only the tightest one so the
                                // strip can hold its shape; this is where the
                                // rest are meant to be found.
                                ForEach(Array(subscription.windows.enumerated()), id: \.offset) { _, window in
                                    ProviderWindowRow(
                                        account: subscription.representative, window: window
                                    )
                                }
                                if let reason = subscription.reasonText {
                                    Label(reason, systemImage: "exclamationmark.triangle")
                                        .droverText(.subtitle, accented: true)
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
        let presentation = ObservedUsageSectionPresentation(section: snapshot.activity)
        VStack(alignment: .leading, spacing: 10) {
            CockpitSectionHeading(title: "Observed usage", source: "Drover observed", action: nil)
            if let warningText = presentation.warningText {
                CockpitCard {
                    Label(warningText, systemImage: "exclamationmark.triangle")
                        .droverText(.nested)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel(presentation.accessibilityLabel ?? warningText)
                .accessibilityIdentifier(ObservedUsageSectionPresentation.identifier)
            } else if let activity = snapshot.activity.data {
                let metadata = ObservedAggregatePresentation(
                    metadata: activity.metadata,
                    fallbackCoverage: activity.coverage
                )
                // Said once, here, instead of on every row below.
                Text("\(metadata.freshnessText) · \(metadata.coverageText)")
                    .droverText(.subtitle)
                    .fixedSize(horizontal: false, vertical: true)
                CockpitCard {
                    // A wider minimum than the old 88pt: at accessibility
                    // sizes three columns crushed "Observed sessions" into a
                    // stack of single words. This reflows to two-up, then one.
                    LazyVGrid(
                        columns: [GridItem(.adaptive(minimum: 116), alignment: .leading)],
                        alignment: .leading,
                        spacing: 12
                    ) {
                        analyticsMetric(
                            format(activity.totals.sessionCount),
                            "Observed sessions"
                        )
                        analyticsMetric(format(activity.totals.totalTokens), "Tokens")
                        analyticsMetric(currency(activity.totals.costUSD), "API cost")
                    }
                }

                distributionSection(
                    title: "Projects", singular: "project", glyph: "folder",
                    dimension: .projects,
                    entries: store.analyticsProjects.map {
                        DistributionPresentationBuilder.Entry(
                            key: $0.projectKey,
                            sessionCount: $0.sessionCount,
                            totalTokens: $0.totalTokens,
                            metadata: $0.metadata,
                            secondaryText: projectContributors($0)
                        )
                    },
                    activity: activity
                )
                distributionSection(
                    title: "Harnesses", singular: "harness", glyph: "cpu",
                    dimension: .harnesses,
                    entries: entries(store.analyticsHarnesses), activity: activity
                )
                distributionSection(
                    title: "Hosts", singular: "host", glyph: "desktopcomputer",
                    dimension: .hosts,
                    entries: entries(store.analyticsHosts), activity: activity
                )
                distributionSection(
                    title: "Models", singular: "model", glyph: "sparkles",
                    dimension: .models,
                    entries: entries(store.analyticsModels), activity: activity
                )
            }
        }
        .accessibilityIdentifier("analytics-drover-observed")
    }

    @ViewBuilder
    private func distributionSection(
        title: String,
        singular: String,
        glyph: String,
        dimension: AnalyticsDimension,
        entries: [DistributionPresentationBuilder.Entry],
        activity: ActivitySummary
    ) -> some View {
        let section = DistributionPresentationBuilder.section(
            title: title,
            singular: singular,
            entries: entries,
            rank: rank,
            sectionMetadata: activity.metadata,
            fallbackCoverage: activity.coverage
        )
        DistributionSectionView(
            section: section,
            glyph: glyph,
            onToggleRank: { rank = rank.other },
            trailing: AnyView(paginationControls(dimension))
        )
        .accessibilityIdentifier("analytics-distribution-\(dimension.rawValue)")
    }

    private func entries(_ values: [ActivityBreakdown]) -> [DistributionPresentationBuilder.Entry] {
        values.map {
            DistributionPresentationBuilder.Entry(
                key: $0.key,
                sessionCount: $0.sessionCount,
                totalTokens: $0.totalTokens,
                metadata: $0.metadata
            )
        }
    }

    @ViewBuilder
    private func paginationControls(_ dimension: AnalyticsDimension) -> some View {
        if let error = store.analyticsPaginationError(for: dimension) {
            Text(error).droverText(.subtitle, accented: true)
        }
        if store.nextAnalyticsCursor(for: dimension) != nil {
            Button {
                Task { await store.loadMoreAnalytics(dimension) }
            } label: {
                if store.isLoadingAnalytics(dimension) {
                    ProgressView()
                } else {
                    Text("Load more")
                }
            }
            .buttonStyle(.bordered)
            .accessibilityIdentifier("analytics-load-more-\(dimension.rawValue)")
        }
    }

    /// Who touched this project. Unlike source/freshness/coverage this is
    /// different on every row, so it is not what the redesign collapses.
    private func projectContributors(_ value: ProjectActivity) -> String {
        let harnesses = contributors(
            value.harnesses,
            attributedSessionCount: value.harnessAttributedSessionCount,
            totalSessionCount: value.sessionCount
        )
        let hosts = contributors(
            value.hosts,
            attributedSessionCount: value.hostAttributedSessionCount,
            totalSessionCount: value.sessionCount
        )
        return "\(harnesses) · \(hosts)"
    }

    private func contributors(
        _ values: [String], attributedSessionCount: Int, totalSessionCount: Int
    ) -> String {
        AnalyticsContributorPresentation(
            values: values,
            attributedSessionCount: attributedSessionCount,
            totalSessionCount: totalSessionCount
        ).text
    }

    private func analyticsMetric(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).droverText(.h2).monospacedDigit()
            Text(label).droverText(.subtitle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func reload() async {
        await store.loadAnalytics(filters: AnalyticsFilters(
            days: days, hostID: host, harness: harness, provider: provider,
            model: model, projectKey: project
        ))
    }

    private var activity: ActivitySummary? { store.analytics?.activity.data }
    private var hostValues: [String] { store.analyticsHosts.map(\.key).sorted() }
    private var harnessValues: [String] { store.analyticsHarnesses.map(\.key).sorted() }
    private var modelValues: [String] { store.analyticsModels.map(\.key).sorted() }
    private var projectValues: [String] { store.analyticsProjects.map(\.projectKey).sorted() }
    private var providerValues: [String] {
        Array(Set((store.analytics?.providerCapacity.data ?? []).map(\.provider))).sorted()
    }
}

/// One quota window, with its bar. The cockpit strip shows only the window
/// closest to exhaustion so its cards can hold a common height; this screen is
/// where the other windows have to be legible, so each gets the same treatment
/// the headline gets on the card.
private struct ProviderWindowRow: View {
    let account: ProviderAccount
    let window: ProviderWindow

    var body: some View {
        let value = ProviderHeadline(account: account, window: window, now: .now)
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(value.windowTitle).droverText(.h3)
                Spacer(minLength: 0)
                Text(value.usedText)
                    .droverText(.subtitle, accented: value.isCritical)
            }
            CapacityBar(fraction: value.fraction, isCritical: value.isCritical)
            if let detail = value.detailText {
                Text(detail)
                    .droverText(.subtitle)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 3)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            [value.windowTitle, value.usedText, value.detailText]
                .compactMap { $0 }.joined(separator: ", ")
        )
    }
}

struct AnalyticsRefreshBanner: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "arrow.clockwise.circle")
            .droverText(.nested)
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(presentation.accessibilityLabel)
            .accessibilityIdentifier(AnalyticsRefreshBannerPresentation.identifier)
    }

    private var presentation: AnalyticsRefreshBannerPresentation {
        AnalyticsRefreshBannerPresentation(message: message)
    }
}

struct AnalyticsRefreshBannerPresentation: Equatable {
    static let identifier = "analytics-refresh-notice"
    let message: String
    var accessibilityLabel: String { message }
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
