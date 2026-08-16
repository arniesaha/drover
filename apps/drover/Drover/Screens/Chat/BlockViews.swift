import SwiftUI
import DroverKit
import UIKit

/// One 10px radius and one hairline for every block in an answer — table,
/// code, diff, artifact alike. That single container is what makes a long
/// answer read as one document rather than four unrelated widgets.
extension View {
    func answerBlock() -> some View {
        self
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(DroverColor.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(DroverColor.line, lineWidth: 1)
            }
    }
}

/// A markdown table, laid out by width rather than by taste.
///
/// Two or three columns stack into label/value rows — a two-column table on a
/// phone *is* a definition list, and pretending otherwise produces four
/// characters per cell. Four or more scroll horizontally with the first
/// column pinned, because past three there is no phone width where a readable
/// cell still fits. Either way the table can leave as TSV, which is the only
/// lossless way off a phone-shaped table.
struct TableBlockView: View {
    let table: TableBlock

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if table.prefersStackedLayout {
                stacked
            } else {
                scrolling
            }
            copyButton
        }
        .answerBlock()
    }

    // MARK: - Two or three columns

    private var stacked: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(table.normalizedRows.enumerated()), id: \.offset) { index, row in
                if index > 0 { Divider().overlay(DroverColor.line) }
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(row.enumerated()), id: \.offset) { column, cell in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(table.headers[safe: column] ?? "")
                                .droverText(.nested)
                            Spacer(minLength: 12)
                            Text(cell)
                                .droverText(.mono)
                                .foregroundStyle(DroverColor.text)
                                .multilineTextAlignment(.trailing)
                        }
                    }
                }
                .padding(.horizontal, 11)
                .padding(.vertical, 9)
            }
        }
    }

    // MARK: - Four or more columns

    private var scrolling: some View {
        HStack(spacing: 0) {
            // The pinned first column is what keeps a scrolled row readable —
            // without it you are looking at four numbers with no subject.
            column(at: 0, isPinned: true)
            Divider().overlay(DroverColor.line)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 0) {
                    ForEach(1..<table.columnCount, id: \.self) { index in
                        column(at: index, isPinned: false)
                    }
                }
            }
        }
    }

    private func column(at index: Int, isPinned: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            cell(table.headers[safe: index] ?? "", isHeader: true, isPinned: isPinned)
            ForEach(Array(table.normalizedRows.enumerated()), id: \.offset) { _, row in
                cell(row[safe: index] ?? "", isHeader: false, isPinned: isPinned)
            }
        }
    }

    private func cell(_ text: String, isHeader: Bool, isPinned: Bool) -> some View {
        Text(text)
            .droverText(isHeader ? .h3 : .mono)
            .foregroundStyle(isHeader ? DroverColor.muted : DroverColor.text)
            .lineLimit(1)
            .frame(minWidth: isPinned ? 84 : 62, alignment: .leading)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
    }

    private var copyButton: some View {
        HStack {
            Spacer()
            Button {
                UIPasteboard.general.string = table.tsv
            } label: {
                Label("Copy as TSV", systemImage: "doc.on.doc")
                    .droverText(.subtitle)
                    .foregroundStyle(DroverColor.accentHi)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("table-copy-tsv")
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 7)
        .overlay(alignment: .top) { Divider().overlay(DroverColor.line) }
    }
}

/// Measures the shared marker column before applying each row's indentation, so
/// nested rows cannot reduce the content width available to root rows.
private struct ListRowsLayout: Layout {
    struct Row {
        let depth: Int
        let extraTopSpacing: CGFloat
    }

    let rows: [Row]

    private let markerSpacing: CGFloat = 8
    private let rowSpacing: CGFloat = 7

    private struct RowMeasurement {
        let indent: CGFloat
        let markerProposal: ProposedViewSize
        let contentProposal: ProposedViewSize
        let markerYOffset: CGFloat
        let contentYOffset: CGFloat
        let height: CGFloat
    }

    func sizeThatFits(
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) -> CGSize {
        measuredSize(proposal: proposal, subviews: subviews)
    }

    func placeSubviews(
        in bounds: CGRect,
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) {
        guard subviews.count == rows.count * 2 else { return }

        let markerWidth = markerColumnWidth(subviews)
        var y = bounds.minY

        for index in rows.indices {
            if index > 0 { y += rowSpacing }
            y += rows[index].extraTopSpacing

            let marker = subviews[index * 2]
            let content = subviews[index * 2 + 1]
            let measurement = rowMeasurement(
                at: index,
                width: bounds.width,
                markerWidth: markerWidth,
                subviews: subviews
            )

            marker.place(
                at: CGPoint(
                    x: bounds.minX + measurement.indent,
                    y: y + measurement.markerYOffset
                ),
                anchor: .topLeading,
                proposal: measurement.markerProposal
            )
            content.place(
                at: CGPoint(
                    x: bounds.minX + measurement.indent + markerWidth + markerSpacing,
                    y: y + measurement.contentYOffset
                ),
                anchor: .topLeading,
                proposal: measurement.contentProposal
            )
            y += measurement.height
        }
    }

    private func measuredSize(proposal: ProposedViewSize, subviews: Subviews) -> CGSize {
        guard subviews.count == rows.count * 2 else { return .zero }

        let markerWidth = markerColumnWidth(subviews)
        let width = proposal.width ?? naturalWidth(
            markerWidth: markerWidth,
            subviews: subviews
        )
        var height: CGFloat = 0

        for index in rows.indices {
            if index > 0 { height += rowSpacing }
            height += rows[index].extraTopSpacing
            height += rowMeasurement(
                at: index,
                width: width,
                markerWidth: markerWidth,
                subviews: subviews
            ).height
        }

        return CGSize(width: width, height: height)
    }

    private func rowMeasurement(
        at index: Int,
        width: CGFloat,
        markerWidth: CGFloat,
        subviews: Subviews
    ) -> RowMeasurement {
        let indent = CGFloat(rows[index].depth) * 14
        let markerProposal = ProposedViewSize(width: markerWidth, height: nil)
        let contentProposal = ProposedViewSize(
            width: max(0, width - indent - markerWidth - markerSpacing),
            height: nil
        )
        let markerDimensions = subviews[index * 2].dimensions(in: markerProposal)
        let contentDimensions = subviews[index * 2 + 1].dimensions(in: contentProposal)
        let markerBaseline = markerDimensions[VerticalAlignment.firstTextBaseline]
        let contentBaseline = contentDimensions[VerticalAlignment.firstTextBaseline]
        let baseline = max(markerBaseline, contentBaseline)
        let markerYOffset = baseline - markerBaseline
        let contentYOffset = baseline - contentBaseline

        return RowMeasurement(
            indent: indent,
            markerProposal: markerProposal,
            contentProposal: contentProposal,
            markerYOffset: markerYOffset,
            contentYOffset: contentYOffset,
            height: max(
                markerYOffset + markerDimensions.height,
                contentYOffset + contentDimensions.height
            )
        )
    }

    private func markerColumnWidth(_ subviews: Subviews) -> CGFloat {
        rows.indices.reduce(CGFloat.zero) { width, index in
            max(width, subviews[index * 2].sizeThatFits(.unspecified).width)
        }
    }

    private func naturalWidth(markerWidth: CGFloat, subviews: Subviews) -> CGFloat {
        rows.indices.reduce(CGFloat.zero) { width, index in
            let indent = CGFloat(rows[index].depth) * 14
            let contentWidth = subviews[index * 2 + 1].sizeThatFits(.unspecified).width
            return max(width, indent + markerWidth + markerSpacing + contentWidth)
        }
    }
}

/// Bullets and ordered items with drawn markers: a filled accent dot at depth
/// 1, a hollow ring at depth 2 with clean indentation, tabular mono numerals
/// with punctuation when ordered.
struct ListBlockView: View {
    let list: ListBlock

    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ListRowsLayout(rows: layoutRows) {
            ForEach(Array(list.items.enumerated()), id: \.offset) { _, item in
                marker(for: item)
                Text(item.content.droverLinks(on: .surface, in: colorScheme))
                    .droverText(item.depth > 0 ? .nested : .body)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var layoutRows: [ListRowsLayout.Row] {
        list.items.enumerated().map { index, item in
            ListRowsLayout.Row(
                depth: item.depth,
                extraTopSpacing: item.depth == 0
                    && index > 0
                    && list.items[index - 1].depth > 0 ? 4 : 0
            )
        }
    }

    @ViewBuilder
    private func marker(for item: ListBlock.Item) -> some View {
        if let ordinal = item.ordinal {
            Text("\(ordinal).")
                .droverText(.marker)
        } else if item.depth == 0 {
            Circle()
                .fill(DroverColor.accent)
                .frame(width: 5, height: 5)
                .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + 3 }
                .frame(width: 14, alignment: .leading)
        } else {
            Circle()
                .strokeBorder(DroverColor.accentHi, lineWidth: 1)
                .frame(width: 5, height: 5)
                .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + 3 }
                .frame(width: 14, alignment: .leading)
        }
    }
}

/// A 2px accent bar plus italic muted body — quiet, and clearly someone
/// else's words.
struct QuoteBlockView: View {
    let content: AttributedString

    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 1)
                .fill(DroverColor.accent)
                .frame(width: 2)
            Text(content.droverLinks(on: .surface, in: colorScheme))
                .droverText(.nested)
                .italic()
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
