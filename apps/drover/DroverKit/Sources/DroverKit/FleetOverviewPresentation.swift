import Foundation

/// Bird's-eye presentation for fleet-wide active session distribution.
///
/// Answers at a glance:
/// - Which hosts are active
/// - Active sessions per host
/// - Active sessions per project
/// - Active sessions per project × host (matrix)
///
/// Derived client-side from the existing `HarnessSnapshot` payload without
/// issuing new hub queries.
public struct FleetOverviewPresentation: Equatable, Sendable {
    public struct HostItem: Equatable, Sendable, Identifiable {
        public let id: String
        public let title: String
        public let presence: HostPresence
        public let activeCount: Int
        public var isOnline: Bool { presence == .online }

        public init(id: String, title: String, presence: HostPresence, activeCount: Int) {
            self.id = id
            self.title = title
            self.presence = presence
            self.activeCount = activeCount
        }
    }

    public struct ProjectItem: Equatable, Sendable, Identifiable {
        public let name: String
        public let activeCount: Int
        public var id: String { name }

        public init(name: String, activeCount: Int) {
            self.name = name
            self.activeCount = activeCount
        }
    }

    public let hosts: [HostItem]
    public let projects: [ProjectItem]
    /// Matrix of active session counts keyed by [project: [hostID: count]]
    public let matrix: [String: [String: Int]]
    public let totalActiveSessions: Int
    public let activeHostCount: Int

    public init(hosts: [HostSummary], sessions: [SessionSummary]) {
        // Active sessions only (excluding retired hosts/sessions)
        let activeSessions = sessions.filter {
            guard $0.hostRetiredAt == nil else { return false }
            switch $0.attention {
            case .needsApproval, .needsInput, .working: return true
            case .done, .errored: return false
            }
        }

        totalActiveSessions = activeSessions.count

        var countByHost: [String: Int] = [:]
        var countByProject: [String: Int] = [:]
        var cellMatrix: [String: [String: Int]] = [:]

        for session in activeSessions {
            countByHost[session.hostID, default: 0] += 1
            let proj = session.projectName
            countByProject[proj, default: 0] += 1
            cellMatrix[proj, default: [:]][session.hostID, default: 0] += 1
        }

        var allHosts = hosts
        let knownHostIDs = Set(hosts.map(\.id))
        for hostID in countByHost.keys where !knownHostIDs.contains(hostID) {
            allHosts.append(HostSummary(id: hostID, displayName: hostID, status: "offline"))
        }

        self.hosts = allHosts.map { host in
            HostItem(
                id: host.id,
                title: host.title,
                presence: host.presence,
                activeCount: countByHost[host.id] ?? 0
            )
        }.sorted { a, b in
            // Online hosts first, then by active count descending, then alphabetical title
            if (a.presence == .online) != (b.presence == .online) {
                return a.presence == .online
            }
            if a.activeCount != b.activeCount {
                return a.activeCount > b.activeCount
            }
            return a.title.localizedCaseInsensitiveCompare(b.title) == .orderedAscending
        }

        self.activeHostCount = self.hosts.filter { $0.isOnline || $0.activeCount > 0 }.count

        self.projects = countByProject.map { ProjectItem(name: $0.key, activeCount: $0.value) }
            .sorted { a, b in
                if a.activeCount != b.activeCount {
                    return a.activeCount > b.activeCount
                }
                return a.name.localizedCaseInsensitiveCompare(b.name) == .orderedAscending
            }

        self.matrix = cellMatrix
    }

    /// Convenience initializer from a snapshot.
    public init(snapshot: HarnessSnapshot?) {
        self.init(hosts: snapshot?.hosts ?? [], sessions: snapshot?.sessions ?? [])
    }

    /// Active session count for a specific (project, host) intersection.
    public func count(project: String, hostID: String) -> Int {
        matrix[project]?[hostID] ?? 0
    }
}
