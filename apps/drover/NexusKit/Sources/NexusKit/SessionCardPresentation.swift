import Foundation

public struct SessionCardPresentation: Sendable, Equatable {
    public let harness: HarnessPresentation
    public let title: String
    public let metadataText: String
    public let projectName: String?

    public init(session: SessionSummary, hostTitle: String) {
        harness = HarnessPresentation(session.harness)
        projectName = Self.projectName(for: session.cwd)

        if let preview = Self.nonEmpty(session.preview) {
            title = preview
        } else if let projectName {
            title = "\(harness.name) session in \(projectName)"
        } else if harness.name == "Session" {
            title = "Session"
        } else {
            title = "\(harness.name) session"
        }

        let metadataParts = [
            harness.name,
            Self.statusLabel(for: session.attention),
            Self.nonEmpty(hostTitle),
            projectName,
        ].compactMap { $0 }
        metadataText = metadataParts.joined(separator: " · ")
    }

    private static func projectName(for cwd: String?) -> String? {
        guard let cwd = nonEmpty(cwd) else { return nil }
        return URL(fileURLWithPath: cwd).lastPathComponent
    }

    private static func nonEmpty(_ value: String?) -> String? {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func statusLabel(for attention: AttentionState) -> String {
        switch attention {
        case .needsApproval: return "Needs approval"
        case .needsInput: return "Waiting on you"
        case .working: return "Running"
        case .done: return "Exited"
        case .errored: return "Error"
        }
    }
}
