import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Drover
@testable import DroverKit

@MainActor
struct WorkAttentionLayoutTests {
    @Test func hiddenSectionOccupiesNoHeight() {
        let store = WorkAttentionStore(service: LayoutAttentionService(response: nil))
        let host = UIHostingController(rootView: WorkAttentionList(store: store) { _ in })
        #expect(host.sizeThatFits(in: CGSize(width: 320, height: 0)).height == 0)
    }

    @Test func visibleSectionSupportsLargestDynamicType() async throws {
        let response = try attentionResponse([attentionItemJSON()])
        let store = WorkAttentionStore(service: LayoutAttentionService(response: response))
        await store.refresh()
        var heights: [CGFloat] = []
        for size in [DynamicTypeSize.large, .accessibility5] {
            let host = UIHostingController(rootView: WorkAttentionList(store: store) { _ in }
                .environment(\.dynamicTypeSize, size))
            let measured = host.sizeThatFits(in: CGSize(width: 320, height: 0))
            #expect(measured.width <= 320)
            #expect(measured.height.isFinite)
            heights.append(measured.height)
        }
        #expect(heights[1] > heights[0] * 1.5)
    }

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

private struct LayoutAttentionService: WorkAttentionServing {
    let response: WorkAttentionResponse?
    func workAttention() async throws -> WorkAttentionResponse {
        guard let response else { throw DroverError.transport("offline") }
        return response
    }
    func workDecide(item: WorkAttentionItem, action: String) async throws -> WorkDecisionResponse {
        throw DroverError.transport("unused")
    }
}
