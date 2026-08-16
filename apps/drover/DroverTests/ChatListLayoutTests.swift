import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

/// Ordered markers are one column, not three independently-sized prefixes.
///
/// These tests lay the real list view out. Giving every row the widest marker
/// width means identical content wraps identically whether the ordinal is 9,
/// 10, or 100. The old per-row HStacks made the mixed list shorter whenever
/// the narrower markers happened to save a line.
@MainActor
struct ChatListLayoutTests {
    private static let content = "A wrapped list item keeps its text aligned with every other item."

    private func height(
        ordinals: [Int],
        typeSize: DynamicTypeSize,
        width: CGFloat
    ) -> CGFloat {
        let list = ListBlock(items: ordinals.map {
            ListBlock.Item(depth: 0, ordinal: $0, content: AttributedString(Self.content))
        })
        let view = ListBlockView(list: list)
            .frame(width: width)
            .dynamicTypeSize(typeSize)
            .droverTint()
        let host = UIHostingController(rootView: view)
        host.view.frame = CGRect(x: 0, y: 0, width: width, height: 800)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(
            in: CGSize(width: width, height: .greatestFiniteMagnitude)
        ).height
    }

    @Test func programmaticBottomTargetCannotReuseTheFinalRowIdentity() throws {
        let message = HarnessMessage(
            id: "answer", seq: 1, type: .assistantOutput, role: "assistant", text: "Done"
        )
        let items = TranscriptItem.group([message])
        let destination = try #require(
            ChatTranscriptScrollTarget.bottomDestination(for: items)
        )

        #expect(destination == AnyHashable(ChatTranscriptScrollTarget.visualTail))
        #expect(destination != AnyHashable(try #require(items.last?.id)))
        #expect(ChatTranscriptScrollTarget.bottomDestination(for: []) == nil)
    }

    @Test func nineTenAndOneHundredShareTheWidestMarkerColumn() {
        for width: CGFloat in [148, 164, 180, 196, 212] {
            let mixed = height(ordinals: [9, 10, 100], typeSize: .large, width: width)
            let allWidest = height(
                ordinals: [100, 100, 100], typeSize: .large, width: width
            )

            #expect(
                mixed == allWidest,
                "at \(width)pt, mixed markers measured \(mixed)pt vs \(allWidest)pt"
            )
        }
    }

    @Test func sharedMarkerColumnSurvivesAccessibilityType() {
        let width: CGFloat = 188
        let mixed = height(
            ordinals: [9, 10, 100], typeSize: .accessibility3, width: width
        )
        let allWidest = height(
            ordinals: [100, 100, 100], typeSize: .accessibility3, width: width
        )

        #expect(mixed == allWidest, "mixed markers measured \(mixed)pt vs \(allWidest)pt")
    }
}
