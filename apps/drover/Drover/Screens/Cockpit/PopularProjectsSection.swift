import DroverKit
import SwiftUI

/// The projects the fleet has been busiest in.
///
/// Previously a horizontal scroller of fixed-width cards, each restating
/// "Ranked by sessions · 5.8% token coverage" — text identical on every card,
/// while the card beside it was cut off mid-word at the screen edge with
/// nothing to say it continued. The ranking and coverage are true of the whole
/// section, so they are said once in the heading, and the cards wrap.
struct PopularProjectsSection: View {
    let projects: [PopularProject]
    let tokenCoveragePercent: Double?
    let onOpenAnalytics: () -> Void
    /// Scaled for the same reason the metric grid is: a fixed minimum keeps
    /// two columns at accessibility sizes and hyphenates the project names.
    @ScaledMetric(relativeTo: .title3) private var cardMinimum: CGFloat = 150

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            CockpitSectionHeading(
                title: "Busiest projects", source: "Drover observed", action: onOpenAnalytics
            )
            if let subtitle {
                Text(subtitle)
                    .droverText(.subtitle)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Two-up where there is room, one-up when there is not — nothing
            // is ever clipped by the screen edge.
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: cardMinimum), spacing: 10, alignment: .top)],
                alignment: .leading,
                spacing: 10
            ) {
                ForEach(projects, id: \.projectKey) { project in
                    ProjectCard(
                        project: project,
                        share: share(of: project),
                        tokenCoveragePercent: tokenCoveragePercent
                    )
                }
            }
        }
        .accessibilityIdentifier("popular-projects-section")
    }

    /// The ranking and coverage, said once for the whole section.
    private var subtitle: String? {
        guard let first = projects.first else { return nil }
        let value = ProjectActivityPresentation(
            project: first, tokenCoveragePercent: tokenCoveragePercent
        )
        return "\(value.metricText) · \(value.coverageText)"
    }

    /// Share of the busiest project in this section, so a bar means "relative
    /// to the biggest thing here" and implies no wider scale.
    private func share(of project: PopularProject) -> Double {
        let values = projects.map(metric)
        guard let largest = values.max(), largest > 0 else { return 0 }
        return Double(metric(project)) / Double(largest)
    }

    private func metric(_ project: PopularProject) -> Int {
        project.metric == .tokens ? project.totalTokens : project.sessionCount
    }
}

private struct ProjectCard: View {
    let project: PopularProject
    let share: Double
    let tokenCoveragePercent: Double?

    var body: some View {
        let value = ProjectActivityPresentation(
            project: project, tokenCoveragePercent: tokenCoveragePercent
        )
        // Split so a long key wraps between owner and repo rather than being
        // ellipsed through the middle, which loses the half that identifies it.
        let key = ProjectKeySplit(value.projectName)

        CockpitCard {
            VStack(alignment: .leading, spacing: 5) {
                if let owner = key.owner {
                    Text(owner)
                        .droverText(.subtitle)
                        .lineLimit(1)
                        .truncationMode(.head)
                }
                Text(key.name)
                    .droverText(.h2)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(alignment: .center, spacing: 8) {
                    CapacityBar(fraction: share, height: 4)
                    // The figure always sits beside the bar; a bar is never
                    // the only way to read a quantity.
                    Text(value.valueText)
                        .droverText(.subtitle)
                        .monospacedDigit()
                        .layoutPriority(1)
                }

                if !value.contributorsText.isEmpty {
                    // Varies per card, so it stays — the collapse targets text
                    // that repeated identically across the section.
                    Text(value.contributorsText)
                        .droverText(.mono)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            "\(value.projectName), \(value.valueText), \(value.metricText), "
            + "\(value.coverageText), contributors \(value.contributorsText)"
        )
    }
}
