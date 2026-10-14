import SwiftUI
import DroverKit
import UIKit

/// Inline artifact pane, also used in the floating chip's sheet.
/// Machine strings truncate in the middle and retain their Open or Copy action.
/// The bounded list scrolls when more than three artifacts are present.
struct ArtifactRows: View {
    let artifacts: [SessionArtifact]

    /// The transcript starts collapsed; the sheet opens the list immediately.
    @State private var isExpanded: Bool

    init(artifacts: [SessionArtifact], initiallyExpanded: Bool = false) {
        self.artifacts = artifacts
        _isExpanded = State(initialValue: initiallyExpanded)
    }

    @Environment(\.openURL) private var openURL

    /// Up to three rows are shown whole; past that the list scrolls.
    private static let maxVisibleRows = 3

    /// Roughly three and a half rows at default type: enough that the pane
    /// reads as a list, short enough that the transcript stays the larger
    /// surface, and the half row is the affordance that says there is more
    /// below. Scaled, so the same three-and-a-half rows survive at
    /// accessibility sizes rather than becoming one and a half.
    @ScaledMetric(relativeTo: .caption) private var listHeight: CGFloat = 156

    var body: some View {
        VStack(spacing: 0) {
            header
            if isExpanded {
                Divider().overlay(DroverColor.line)
                list
            }
        }
        .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(DroverColor.line, lineWidth: 1)
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
    }

    private var header: some View {
        Button {
            withAnimation(.snappy(duration: 0.22)) { isExpanded.toggle() }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "shippingbox")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DroverColor.accentHi)
                    .frame(width: 16)
                Text(title)
                    .droverText(.h3)
                Spacer(minLength: 8)
                Image(systemName: "chevron.down")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DroverColor.muted)
                    .rotationEffect(.degrees(isExpanded ? 0 : -90))
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 9)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("artifact-pane-toggle")
        .accessibilityLabel(title)
        .accessibilityHint(isExpanded ? "Collapse" : "Expand")
    }

    /// Two artifacts get the height of two artifacts; nine get `listHeight`
    /// and a scroll. The row count is what decides, deliberately: the two
    /// ways SwiftUI offers to say "as tall as the content, up to a cap" both
    /// fail here. A geometry reader inside the scroll view driving that
    /// scroll view's own frame is a layout cycle and resolves to a 1pt
    /// sliver; `ViewThatFits` picks the scrolling fallback even for a single
    /// row. A count and a scaled height are boring and they hold.
    @ViewBuilder
    private var list: some View {
        if artifacts.count <= Self.maxVisibleRows {
            rowStack.accessibilityIdentifier("artifact-list")
        } else {
            ScrollView {
                rowStack
            }
            .frame(height: listHeight)
            // The newest artifact is the one the session just produced.
            .defaultScrollAnchor(.bottom)
            .scrollIndicators(.visible)
            .accessibilityIdentifier("artifact-list")
        }
    }

    private var rowStack: some View {
        VStack(spacing: 0) {
            ForEach(Array(artifacts.enumerated()), id: \.element.id) { index, artifact in
                if index > 0 { Divider().overlay(DroverColor.line) }
                row(artifact)
            }
        }
    }

    private var title: String {
        artifacts.count == 1 ? "1 artifact" : "\(artifacts.count) artifacts"
    }

    private func row(_ artifact: SessionArtifact) -> some View {
        HStack(spacing: 10) {
            Image(systemName: artifact.kind == .branch ? "arrow.triangle.branch" : "arrow.triangle.pull")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(DroverColor.accentHi)
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 1) {
                Text(artifact.kind.rawValue)
                    .droverText(.h3)
                Text(artifact.value)
                    .droverText(.mono)
                    .foregroundStyle(DroverColor.text)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            Button {
                if let url = artifact.url {
                    openURL(url)
                } else {
                    UIPasteboard.general.string = artifact.value
                }
            } label: {
                Text(artifact.action)
                    .font(.system(.caption, design: .default, weight: .medium))
                    .foregroundStyle(DroverColor.accentHi)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .frame(minWidth: 44, minHeight: 44)
                    .overlay { Capsule().strokeBorder(DroverColor.accent, lineWidth: 1) }
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("artifact-action")
            .accessibilityLabel("\(artifact.action) \(artifact.kind.rawValue)")
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
    }
}
