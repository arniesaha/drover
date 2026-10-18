import DroverKit
import SwiftUI

/// Compact bird's-eye overview of the fleet:
/// Shows active hosts, active sessions per host, active sessions per project,
/// and active sessions per project × host matrix.
///
/// Tapping a matrix cell applies BOTH the host and project filters.
/// Tapping a host header applies the host filter.
/// Tapping a project header applies the project filter.
struct FleetOverviewView: View {
    let overview: FleetOverviewPresentation
    let filterState: FleetFilterState
    let onSelectCell: (_ hostID: String, _ project: String) -> Void
    let onSelectHost: (_ hostID: String) -> Void
    let onSelectProject: (_ project: String) -> Void

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            headerRow

            if overview.projects.isEmpty {
                Text("No active sessions across the fleet.")
                    .droverText(.subtitle)
                    .padding(.vertical, 8)
            } else if dynamicTypeSize.isAccessibilitySize {
                accessibilityGroupedList
            } else {
                compactMatrix
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .fixedSize(horizontal: false, vertical: true)
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(DroverColor.line, lineWidth: 1)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("fleet-birds-eye-view")
    }

    private var headerRow: some View {
        HStack(spacing: 8) {
            Image(systemName: "square.grid.3x3.fill")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(DroverColor.accentHi)

            Text("Bird's-Eye Overview")
                .font(.system(.caption, design: .monospaced, weight: .semibold))
                .textCase(.uppercase)
                .tracking(1.0)
                .foregroundStyle(DroverColor.text)

            Spacer(minLength: 8)

            Text("\(overview.totalActiveSessions) active · \(overview.activeHostCount) live")
                .droverText(.mono)
                .foregroundStyle(DroverColor.muted)
                .accessibilityIdentifier("birds-eye-summary")
        }
    }

    // MARK: - Standard Compact Matrix

    private var compactMatrix: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 6) {
                // Column headers: Project | Host 1 | Host 2 ... | Total
                HStack(spacing: 6) {
                    Text("Project")
                        .font(.system(.caption2, design: .monospaced, weight: .semibold))
                        .foregroundStyle(DroverColor.faint)
                        .frame(width: 100, alignment: .leading)

                    ForEach(overview.hosts) { host in
                        OverviewHostHeaderCell(
                            host: host,
                            isSelected: filterState.hostID == host.id,
                            onSelect: { onSelectHost(host.id) }
                        )
                    }

                    Text("Total")
                        .font(.system(.caption2, design: .monospaced, weight: .semibold))
                        .foregroundStyle(DroverColor.faint)
                        .frame(width: 38, alignment: .trailing)
                }
                .padding(.bottom, 2)

                Divider().background(DroverColor.line)

                // Project Rows
                ForEach(overview.projects) { project in
                    HStack(spacing: 6) {
                        Button {
                            onSelectProject(project.name)
                        } label: {
                            Text(project.name)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(filterState.project == project.name ? DroverColor.accentHi : DroverColor.text)
                                .lineLimit(1)
                                .truncationMode(.middle)
                                .frame(width: 100, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Filter by project \(project.name), \(project.activeCount) active")
                        .accessibilityIdentifier("overview-project-row-\(project.name)")

                        ForEach(overview.hosts) { host in
                            let count = overview.count(project: project.name, hostID: host.id)
                            let isCellSelected = filterState.hostID == host.id && filterState.project == project.name
                            OverviewMatrixCell(
                                count: count,
                                isSelected: isCellSelected,
                                projectName: project.name,
                                host: host,
                                onSelect: { onSelectCell(host.id, project.name) }
                            )
                        }

                        Button {
                            onSelectProject(project.name)
                        } label: {
                            Text("\(project.activeCount)")
                                .font(.system(.caption, design: .monospaced, weight: .semibold))
                                .foregroundStyle(filterState.project == project.name ? DroverColor.accentHi : DroverColor.muted)
                                .frame(width: 38, alignment: .trailing)
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.vertical, 2)
                }

                Divider().background(DroverColor.line)

                // Totals Row
                HStack(spacing: 6) {
                    Text("Active")
                        .font(.system(.caption2, design: .monospaced, weight: .semibold))
                        .foregroundStyle(DroverColor.faint)
                        .frame(width: 100, alignment: .leading)

                    ForEach(overview.hosts) { host in
                        Button {
                            onSelectHost(host.id)
                        } label: {
                            Text("\(host.activeCount)")
                                .font(.system(.caption, design: .monospaced, weight: .semibold))
                                .foregroundStyle(filterState.hostID == host.id ? DroverColor.accentHi : DroverColor.muted)
                                .frame(minWidth: 54, alignment: .center)
                        }
                        .buttonStyle(.plain)
                    }

                    Text("\(overview.totalActiveSessions)")
                        .font(.system(.caption, design: .monospaced, weight: .bold))
                        .foregroundStyle(DroverColor.accentHi)
                        .frame(width: 38, alignment: .trailing)
                }
                .padding(.top, 2)
            }
            .padding(.trailing, 8)
        }
    }

    // MARK: - Dynamic Type Accessibility Layout

    private var accessibilityGroupedList: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(overview.projects) { project in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Button {
                            onSelectProject(project.name)
                        } label: {
                            Text(project.name)
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(filterState.project == project.name ? DroverColor.accentHi : DroverColor.text)
                        }
                        .buttonStyle(.plain)

                        Spacer()

                        Text("\(project.activeCount) active")
                            .droverText(.mono)
                            .foregroundStyle(DroverColor.muted)
                    }

                    // Tappable host pills for this project
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(overview.hosts) { host in
                                let count = overview.count(project: project.name, hostID: host.id)
                                if count > 0 {
                                    let isSelected = filterState.hostID == host.id && filterState.project == project.name
                                    OverviewPillCell(
                                        host: host,
                                        projectName: project.name,
                                        count: count,
                                        isSelected: isSelected,
                                        onSelect: { onSelectCell(host.id, project.name) }
                                    )
                                }
                            }
                        }
                    }
                }
                .padding(10)
                .background(DroverColor.bg, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
        }
    }
}

// MARK: - Subviews

private struct HostPresenceDot: View {
    let host: FleetOverviewPresentation.HostItem

    var body: some View {
        if !host.isOnline {
            Circle()
                .strokeBorder(DroverColor.line, lineWidth: 1)
                .frame(width: 6, height: 6)
        } else if host.activeCount > 0 {
            Circle()
                .fill(DroverColor.accentHi)
                .frame(width: 6, height: 6)
        } else {
            Circle()
                .fill(DroverColor.muted)
                .frame(width: 6, height: 6)
        }
    }
}

private struct OverviewHostHeaderCell: View {
    let host: FleetOverviewPresentation.HostItem
    let isSelected: Bool
    let onSelect: () -> Void

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 4) {
                HostPresenceDot(host: host)
                Text(shortTitle(for: host.title))
                    .font(.system(.caption2, design: .default, weight: .medium))
                    .lineLimit(1)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background {
                if isSelected {
                    RoundedRectangle(cornerRadius: 6).fill(DroverColor.accentTint)
                }
            }
            .overlay {
                if isSelected {
                    RoundedRectangle(cornerRadius: 6).strokeBorder(DroverColor.accent, lineWidth: 1)
                }
            }
            .foregroundStyle(isSelected ? AnyShapeStyle(DroverColor.accentHi) : AnyShapeStyle(DroverColor.muted))
            .frame(minWidth: 54, alignment: .center)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Filter by host \(host.title), \(host.activeCount) active")
        .accessibilityIdentifier("overview-host-header-\(host.id)")
    }

    private func shortTitle(for title: String) -> String {
        let parts = title.split(separator: "-").map(String.init)
        if parts.count > 1, let last = parts.last, last.count <= 6 {
            return last
        }
        return title
    }
}

private struct OverviewMatrixCell: View {
    let count: Int
    let isSelected: Bool
    let projectName: String
    let host: FleetOverviewPresentation.HostItem
    let onSelect: () -> Void

    var body: some View {
        if count > 0 {
            Button(action: onSelect) {
                Text("\(count)")
                    .font(.system(.caption, design: .monospaced).weight(.medium))
                    .frame(minWidth: 44, minHeight: 26)
                    .background {
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(isSelected ? AnyShapeStyle(DroverColor.accent) : AnyShapeStyle(DroverColor.accentTint))
                    }
                    .foregroundStyle(isSelected ? AnyShapeStyle(DroverColor.bg) : AnyShapeStyle(DroverColor.accentHi))
                    .overlay {
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .strokeBorder(isSelected ? AnyShapeStyle(DroverColor.accentHi) : AnyShapeStyle(DroverColor.accent.opacity(0.3)), lineWidth: 1)
                    }
                    .frame(minWidth: 54, alignment: .center)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(projectName) on \(host.title), \(count) active sessions")
            .accessibilityIdentifier("overview-cell-\(projectName)-\(host.id)")
        } else {
            Text("–")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(DroverColor.line)
                .frame(minWidth: 54, minHeight: 26, alignment: .center)
        }
    }
}

private struct OverviewPillCell: View {
    let host: FleetOverviewPresentation.HostItem
    let projectName: String
    let count: Int
    let isSelected: Bool
    let onSelect: () -> Void

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 4) {
                HostPresenceDot(host: host)
                Text("\(host.title): \(count)")
                    .font(.caption)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background {
                Capsule().fill(isSelected ? AnyShapeStyle(DroverColor.accent) : AnyShapeStyle(DroverColor.accentTint))
            }
            .foregroundStyle(isSelected ? AnyShapeStyle(DroverColor.bg) : AnyShapeStyle(DroverColor.accentHi))
            .overlay {
                Capsule().strokeBorder(isSelected ? AnyShapeStyle(DroverColor.accentHi) : AnyShapeStyle(DroverColor.accent.opacity(0.3)), lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("overview-acc-cell-\(projectName)-\(host.id)")
    }
}
