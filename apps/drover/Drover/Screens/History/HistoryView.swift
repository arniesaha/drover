import SwiftUI
import DroverKit

/// Every session across the user's hosts, newest activity first, reached
/// from Home. Paging, the memory window and search debounce live in
/// `HistoryModel`; this view only lays them out.
struct HistoryView: View {
    @State private var model: HistoryModel
    @State private var showFilters = false
    private let client: DroverClient

    init(client: DroverClient) {
        self.client = client
        _model = State(initialValue: HistoryModel(loader: client))
    }

    var body: some View {
        @Bindable var model = model
        List {
            if let deadline = model.busyUntil {
                Label(RetryPolicy.busyMessage(until: deadline), systemImage: "clock")
                    .droverText(.subtitle)
                    .listRowBackground(Color.clear)
            }
            ForEach(model.sections) { section in
                Section {
                    ForEach(section.rows) { row in
                        rowView(row)
                            .onAppear { Task { await model.rowAppeared(row) } }
                    }
                } header: {
                    Text(Self.dayTitle(section.day))
                        .droverText(.marker)
                }
            }
            footer
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(DroverColor.bg)
        .overlay { emptyOrErrorState }
        .searchable(text: $model.searchText, prompt: "Titles, summaries, repos")
        .refreshable { await model.refresh() }
        .navigationTitle("History")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { showFilters = true } label: {
                    Label(
                        model.filter.activeCount == 0 ? "Filters" : "Filters (\(model.filter.activeCount))",
                        systemImage: model.filter.activeCount == 0
                            ? "line.3.horizontal.decrease.circle"
                            : "line.3.horizontal.decrease.circle.fill"
                    )
                }
                .accessibilityIdentifier("history-filters")
            }
        }
        .sheet(isPresented: $showFilters) {
            HistoryFilterSheet(facets: model.facets, filter: model.filter) { filter in
                Task { await model.apply(filter) }
            }
            .presentationDetents([.medium, .large])
        }
        .task { await model.start() }
        .accessibilityIdentifier("history-list")
    }

    @ViewBuilder
    private func rowView(_ row: HistoryPager.Row) -> some View {
        if let item = row.item {
            NavigationLink {
                HistoryTranscriptView(client: client, item: item)
            } label: {
                HistoryRow(item: item)
            }
            .listRowBackground(Color.clear)
            .accessibilityIdentifier("history-row-\(item.id)")
        } else {
            // An evicted page: same height, refetched as soon as it appears.
            HistoryRowPlaceholder()
                .listRowBackground(Color.clear)
        }
    }

    @ViewBuilder
    private var footer: some View {
        if model.isLoading, !model.pager.rows.isEmpty {
            HStack { Spacer(); ProgressView(); Spacer() }
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
        } else if let error = model.errorMessage, !model.pager.rows.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Label(error, systemImage: "exclamationmark.circle")
                    .droverText(.nested, accented: true)
                Button("Retry") { Task { await model.retry() } }
            }
            .listRowBackground(Color.clear)
        } else if model.hasLoadedOnce, !model.pager.hasMore, !model.pager.rows.isEmpty {
            Text("\(model.pager.rowCount) sessions — that's everything")
                .droverText(.subtitle)
                .frame(maxWidth: .infinity)
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
        }
    }

    @ViewBuilder
    private var emptyOrErrorState: some View {
        if model.pager.rows.isEmpty {
            if let error = model.errorMessage {
                ContentUnavailableView {
                    Label("Couldn't load history", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error)
                } actions: {
                    Button("Retry") { Task { await model.retry() } }.buttonStyle(.bordered)
                }
            } else if model.isEmpty {
                if model.filter.isEmpty {
                    ContentUnavailableView(
                        "No sessions yet", systemImage: "clock.arrow.circlepath",
                        description: Text("Sessions you run on any host appear here.")
                    )
                } else {
                    ContentUnavailableView.search(text: model.filter.query)
                }
            } else if model.busyUntil == nil {
                ProgressView("Loading history…")
            }
        }
    }

    static func dayTitle(_ day: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        if day == .distantPast { return "Unknown date" }
        if calendar.isDate(day, inSameDayAs: now) { return "Today" }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
           calendar.isDate(day, inSameDayAs: yesterday) { return "Yesterday" }
        let sameYear = calendar.component(.year, from: day) == calendar.component(.year, from: now)
        return day.formatted(sameYear
            ? .dateTime.weekday(.abbreviated).month(.abbreviated).day()
            : .dateTime.month(.abbreviated).day().year())
    }
}

/// One history row: title, where it ran, when, state, and the summary line.
struct HistoryRow: View {
    let item: HistoryItem

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(item.title)
                    .droverText(.h3)
                    .lineLimit(1)
                Spacer(minLength: 6)
                stateBadge
            }
            Text(meta)
                .droverText(.subtitle)
                .lineLimit(1)
            if let summary = item.summary {
                Text(summary)
                    .droverText(.subtitle)
                    .foregroundStyle(DroverColor.faint)
                    .lineLimit(1)
            }
        }
        .frame(minHeight: HistoryRowPlaceholder.height, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var meta: String {
        var parts: [String] = []
        if let repo = item.repo { parts.append(item.branch.map { "\(repo) · \($0)" } ?? repo) }
        parts.append(HarnessPresentation(item.harness).name)
        parts.append(item.host.retired ? "\(item.host.name) (retired)" : item.host.name)
        if let date = item.lastActivity { parts.append(date.formatted(date: .omitted, time: .shortened)) }
        if let tokens = item.tokens, tokens.total > 0 {
            parts.append("\(CompactNumber.abbreviated(tokens.total)) tok")
        }
        return parts.joined(separator: " · ")
    }

    private var stateBadge: some View {
        Text(item.state.label)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(stateColor)
    }

    /// Red is reserved for terminate (see `DroverColor`); the label carries
    /// the difference between waiting and failed.
    private var stateColor: PaletteToken {
        switch item.state {
        case .running: DroverColor.accentHi
        case .awaiting, .failed: DroverColor.warn
        case .finished: DroverColor.faint
        }
    }
}

struct HistoryRowPlaceholder: View {
    static let height: CGFloat = 58

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            RoundedRectangle(cornerRadius: 4).fill(DroverColor.line).frame(width: 180, height: 12)
            RoundedRectangle(cornerRadius: 4).fill(DroverColor.line).frame(width: 240, height: 10)
        }
        .frame(maxWidth: .infinity, minHeight: Self.height, alignment: .leading)
        .redacted(reason: .placeholder)
        .accessibilityLabel("Loading session")
    }
}

/// Filters as a sheet: state, host (retired ones labelled), harness, repo and
/// a date range. Search stays in the list's search field.
struct HistoryFilterSheet: View {
    let facets: HistoryFacets
    let onApply: (HistoryFilter) -> Void
    @State private var draft: HistoryFilter
    @State private var useSince: Bool
    @State private var useUntil: Bool
    @Environment(\.dismiss) private var dismiss

    init(facets: HistoryFacets, filter: HistoryFilter, onApply: @escaping (HistoryFilter) -> Void) {
        self.facets = facets
        self.onApply = onApply
        _draft = State(initialValue: filter)
        _useSince = State(initialValue: filter.since != nil)
        _useUntil = State(initialValue: filter.until != nil)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("State") {
                    ForEach(HistoryState.allCases, id: \.self) { state in
                        toggleRow(state.label, isOn: draft.states.contains(state)) {
                            draft.states.formSymmetricDifference([state])
                        }
                    }
                }
                if !facets.hosts.isEmpty {
                    Section("Host") {
                        ForEach(facets.hosts) { host in
                            toggleRow(host.retired ? "\(host.name) (retired)" : host.name,
                                      isOn: draft.hosts.contains(host.id)) {
                                draft.hosts.formSymmetricDifference([host.id])
                            }
                        }
                    }
                }
                if !facets.harnesses.isEmpty {
                    Section("Harness") {
                        ForEach(facets.harnesses, id: \.self) { harness in
                            toggleRow(HarnessPresentation(harness).name, isOn: draft.harnesses.contains(harness)) {
                                draft.harnesses.formSymmetricDifference([harness])
                            }
                        }
                    }
                }
                if !facets.repos.isEmpty {
                    Section("Repository") {
                        ForEach(facets.repos, id: \.self) { repo in
                            toggleRow(repo, isOn: draft.repos.contains(repo)) {
                                draft.repos.formSymmetricDifference([repo])
                            }
                        }
                    }
                }
                Section("Date") {
                    Toggle("From", isOn: $useSince)
                    if useSince {
                        DatePicker("From", selection: binding(\.since, default: -7), displayedComponents: .date)
                    }
                    Toggle("To", isOn: $useUntil)
                    if useUntil {
                        DatePicker("To", selection: binding(\.until, default: 0), displayedComponents: .date)
                    }
                }
            }
            .navigationTitle("Filter history")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Clear") {
                        draft = HistoryFilter(query: draft.query)
                        useSince = false
                        useUntil = false
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Apply") {
                        var applied = draft
                        let calendar = Calendar.current
                        applied.since = useSince ? draft.since.map { calendar.startOfDay(for: $0) } : nil
                        // "To" includes the whole chosen day; the API's bound is exclusive.
                        applied.until = useUntil
                            ? draft.until.flatMap { calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: $0)) }
                            : nil
                        onApply(applied)
                        dismiss()
                    }
                    .accessibilityIdentifier("history-filters-apply")
                }
            }
        }
    }

    private func toggleRow(_ title: String, isOn: Bool, toggle: @escaping () -> Void) -> some View {
        Button(action: toggle) {
            HStack {
                Text(title).foregroundStyle(DroverColor.text)
                Spacer()
                if isOn { Image(systemName: "checkmark").foregroundStyle(DroverColor.accentHi) }
            }
        }
    }

    private func binding(_ key: WritableKeyPath<HistoryFilter, Date?>, default days: Int) -> Binding<Date> {
        Binding(
            get: {
                draft[keyPath: key]
                    ?? Calendar.current.date(byAdding: .day, value: days, to: Date()) ?? Date()
            },
            set: { draft[keyPath: key] = $0 }
        )
    }
}

/// Read-only transcript for a history row: the newest 200 events, then older
/// pages by sequence on demand.
struct HistoryTranscriptView: View {
    let item: HistoryItem
    @State private var model: HistoryTranscriptModel

    init(client: DroverClient, item: HistoryItem) {
        self.item = item
        _model = State(initialValue: HistoryTranscriptModel(sessionID: item.id, loader: client))
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                header
                if model.canLoadOlder {
                    Button("Load earlier messages") { Task { await model.loadOlder() } }
                        .buttonStyle(.bordered)
                        .frame(maxWidth: .infinity)
                        .accessibilityIdentifier("history-transcript-older")
                } else if model.isCapped, model.hasOlder {
                    Text("Showing the latest \(HistoryTranscriptModel.maxMessages) messages.")
                        .droverText(.subtitle)
                }
                ForEach(model.messages.filter { !$0.text.isEmpty }) { message in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(message.role.isEmpty ? message.type.rawValue : message.role)
                            .droverText(.marker)
                        Text(message.displayText)
                            .droverText(.body)
                            .textSelection(.enabled)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let error = model.errorMessage {
                    Label(error, systemImage: "exclamationmark.circle")
                        .droverText(.nested, accented: true)
                } else if model.isLoading {
                    ProgressView().frame(maxWidth: .infinity)
                } else if model.hasLoadedOnce, model.messages.isEmpty {
                    Text(item.hasTranscript ? "No readable messages." : "No transcript recorded for this session.")
                        .droverText(.subtitle)
                }
            }
            .padding(14)
        }
        .defaultScrollAnchor(.bottom)
        .background(DroverColor.bg)
        .navigationTitle(item.title)
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.loadNewest() }
        .accessibilityIdentifier("history-transcript")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text([item.repo, HarnessPresentation(item.harness).name, item.host.name, item.state.label]
                .compactMap { $0 }.joined(separator: " · "))
                .droverText(.subtitle)
            if let summary = item.summary {
                Text(summary).droverText(.body)
            }
        }
    }
}
