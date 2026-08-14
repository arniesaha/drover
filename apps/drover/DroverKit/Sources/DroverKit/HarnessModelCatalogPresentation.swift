import Foundation

public enum HarnessModelCatalogPresentation {
    public static func filteredModels(
        in catalog: HarnessModelCatalog?,
        query: String
    ) -> [HarnessModelOption] {
        guard let models = catalog?.models else { return [] }
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return models }
        return models.filter { model in
            model.displayName.localizedCaseInsensitiveContains(query)
                || model.id.localizedCaseInsensitiveContains(query)
                || model.description?.localizedCaseInsensitiveContains(query) == true
        }
    }

    public static func modelTitle(
        selection: String,
        catalog: HarnessModelCatalog?
    ) -> String {
        guard !selection.isEmpty else { return "Harness default" }
        return catalog?.model(id: selection)?.displayName ?? selection
    }

    public static func effortTitle(
        selection: String,
        catalog: HarnessModelCatalog?
    ) -> String {
        guard selection.isEmpty else { return title(forRawEffort: selection) }
        guard let nativeDefault = catalog?.namedDefault?.reasoning?.default else {
            return "Auto"
        }
        return "Auto (\(title(forRawEffort: nativeDefault)))"
    }

    public static func staleText(
        _ catalog: HarnessModelCatalog,
        now: Date
    ) -> String? {
        guard catalog.stale else { return nil }
        guard let discoveredAt = catalog.discoveredAt else { return "Never refreshed" }
        let age = max(0, now.timeIntervalSince(discoveredAt))
        return "Last updated \(SnapshotFreshness.ageText(age)) ago"
    }

    public static func title(forRawEffort effort: String) -> String {
        effort
            .replacingOccurrences(of: "-", with: " ")
            .replacingOccurrences(of: "_", with: " ")
            .split(whereSeparator: \.isWhitespace)
            .map { word in
                guard let first = word.first else { return "" }
                return first.uppercased() + word.dropFirst().lowercased()
            }
            .joined(separator: " ")
    }
}
