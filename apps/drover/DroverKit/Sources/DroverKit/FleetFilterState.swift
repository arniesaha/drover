import Foundation

/// Helper for determining canonical project names from session summaries.
public extension SessionSummary {
    /// Canonical project identifier for filtering and grouping.
    /// Prefers explicit `repoName` (e.g. "arniesaha/drover"), falling back to
    /// the last component of `cwd` (e.g. "drover"), or "Unknown".
    var projectName: String {
        if let repo = repoName?.trimmingCharacters(in: .whitespacesAndNewlines), !repo.isEmpty {
            return repo
        }
        if let cwd = cwd?.trimmingCharacters(in: .whitespacesAndNewlines), !cwd.isEmpty {
            let last = URL(fileURLWithPath: cwd).lastPathComponent
            if !last.isEmpty && last != "/" {
                return last
            }
        }
        return "Unknown"
    }
}

/// State of active host and project filters applied to the fleet sessions inbox.
///
/// Filters combine conjunctively (AND): when both `hostID` and `project` are set,
/// only sessions matching BOTH criteria are displayed.
public struct FleetFilterState: Equatable, Sendable {
    public var hostID: String?
    public var project: String?

    public init(hostID: String? = nil, project: String? = nil) {
        self.hostID = hostID
        self.project = project
    }

    /// True when any filtering criteria is actively applied.
    public var isFiltered: Bool {
        hostID != nil || project != nil
    }

    /// Clears both host and project filters.
    public mutating func clear() {
        hostID = nil
        project = nil
    }

    /// Evaluates whether a given session satisfies the active filter criteria.
    public func matches(session: SessionSummary) -> Bool {
        if let hostID, session.hostID != hostID {
            return false
        }
        if let project {
            let sessionProject = session.projectName
            let matchesExact = sessionProject.caseInsensitiveCompare(project) == .orderedSame
            let matchesBasename = sessionProject.split(separator: "/").last.map(String.init)?.caseInsensitiveCompare(project) == .orderedSame
            let queryBasename = project.split(separator: "/").last.map(String.init)?.caseInsensitiveCompare(sessionProject) == .orderedSame
            if !matchesExact && matchesBasename != true && queryBasename != true {
                return false
            }
        }
        return true
    }

    /// Filters a collection of session summaries.
    public func filter(_ sessions: [SessionSummary]) -> [SessionSummary] {
        guard isFiltered else { return sessions }
        return sessions.filter(matches)
    }

    // MARK: - Persistence

    public static let hostKey = "drover.filter.hostID"
    public static let projectKey = "drover.filter.project"

    /// Loads persisted filter preferences.
    public static func load(from defaults: UserDefaults = .standard) -> FleetFilterState {
        let savedHost = defaults.string(forKey: hostKey)
        let savedProject = defaults.string(forKey: projectKey)
        return FleetFilterState(
            hostID: savedHost?.isEmpty == false ? savedHost : nil,
            project: savedProject?.isEmpty == false ? savedProject : nil
        )
    }

    /// Persists current filter preferences to user defaults.
    public func save(to defaults: UserDefaults = .standard) {
        if let hostID, !hostID.isEmpty {
            defaults.set(hostID, forKey: Self.hostKey)
        } else {
            defaults.removeObject(forKey: Self.hostKey)
        }

        if let project, !project.isEmpty {
            defaults.set(project, forKey: Self.projectKey)
        } else {
            defaults.removeObject(forKey: Self.projectKey)
        }
    }
}
