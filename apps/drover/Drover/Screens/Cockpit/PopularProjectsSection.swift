import DroverKit
import SwiftUI

struct PopularProjectsSection: View {
    let projects: [PopularProject]
    let tokenCoveragePercent: Double?
    let onOpenAnalytics: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CockpitSectionHeading(
                title: "Popular projects", source: "Drover observed", action: onOpenAnalytics
            )
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(projects, id: \.projectKey) { project in
                        let value = ProjectActivityPresentation(
                            project: project, tokenCoveragePercent: tokenCoveragePercent
                        )
                        CockpitCard {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(value.projectName).droverText(.h2).lineLimit(2)
                                Text(value.valueText).droverText(.body)
                                Text("\(value.metricText) · \(value.coverageText)")
                                    .droverText(.subtitle)
                                    .fixedSize(horizontal: false, vertical: true)
                                if !value.contributorsText.isEmpty {
                                    Text(value.contributorsText)
                                        .droverText(.mono)
                                        .lineLimit(3)
                                }
                            }
                        }
                        .frame(width: 250)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel("\(value.projectName), \(value.valueText), \(value.metricText), \(value.coverageText), contributors \(value.contributorsText)")
                    }
                }
            }
            .scrollClipDisabled()
        }
        .accessibilityIdentifier("popular-projects-section")
    }
}
