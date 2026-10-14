import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

@MainActor
struct WorkAttentionLayoutTests {
    private func height(_ size: DynamicTypeSize, kind: String) throws -> CGFloat {
        let item = try #require(attentionResponse([attentionItemJSON(kind: kind)]).items.first)
        let view = WorkAttentionRow(item: item, stale: true, focused: true,
                                    canDecide: { _ in true }, decide: { _ in }, openSession: { _ in })
            .environment(\.dynamicTypeSize, size)
        let host = UIHostingController(rootView: view)
        host.view.frame = CGRect(x: 0, y: 0, width: 320, height: 2400)
        host.view.layoutIfNeeded()
        let measured = host.sizeThatFits(in: CGSize(width: 320, height: 0))
        #expect(measured.width <= 320)
        return measured.height
    }

    @Test func everyKindGrowsAtLargestDynamicTypeWithoutAHeightCap() throws {
        for kind in ["environment_approval", "draft_pr", "agent_question", "blocked", "failed_session"] {
            let normal = try height(.large, kind: kind)
            let largest = try height(.accessibility5, kind: kind)
            #expect(largest > normal * 1.5)
            #expect(largest.isFinite)
        }
    }
}
