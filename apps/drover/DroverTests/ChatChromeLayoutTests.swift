import DroverKit
import SwiftUI
import Testing
import UIKit
@testable import Drover

@MainActor
@Suite(.serialized)
struct ChatChromeLayoutTests {
    private func size<V: View>(_ view: V, width: CGFloat, type: DynamicTypeSize) -> CGSize {
        let host = UIHostingController(rootView: view.environment(\.dynamicTypeSize, type))
        host.view.frame = CGRect(x: 0, y: 0, width: width, height: 852)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(in: CGSize(width: width, height: UIView.layoutFittingCompressedSize.height))
    }

    @Test(arguments: [CGFloat(320), 375, 393])
    func jumpDoesNotBecomeAMultilineCard(width: CGFloat) {
        for count in [0, 6, 12345] {
            let measured = size(JumpToLatestLabel(newEventCount: count, hasUnseenContent: true),
                                width: width - 32, type: .accessibility5)
            #expect(measured.width <= 100)
            #expect(measured.height >= 44)
            #expect(measured.height <= 70)
        }
        let standard = size(JumpToLatestLabel(newEventCount: 6, hasUnseenContent: false),
                            width: width - 32, type: .large)
        #expect(standard.height == 44)
        #expect(standard.width > 120)
        #expect(standard.width < width - 32)
    }

    @Test(arguments: [CGFloat(320), 375, 393])
    func statusTelemetryDoesNotExpandTheDock(width: CGFloat) {
        var activity = SessionActivityPresentation(messages: [])
        activity.phase = .working
        activity.title = "Working"
        activity.startedAt = Date.now.addingTimeInterval(-126)
        activity.updatedAt = Date.now.addingTimeInterval(-4)
        activity.completedSteps = 123
        let summary = size(SessionActivityView(activity: activity), width: width - 32, type: .accessibility5)
        activity.detail = String(repeating: "synthetic command argument ", count: 20)
        let detailed = size(SessionActivityView(activity: activity), width: width - 32, type: .accessibility5)
        #expect(summary == detailed)
        #expect(detailed.width <= width - 32)
        #expect(detailed.height <= 80)
        let standard = size(SessionActivityView(activity: activity), width: width - 32, type: .large)
        #expect(standard.height <= 100)
    }

    @Test(arguments: [CGFloat(320), 375, 393])
    func longStatusTitlesReflowWithinTheScreen(width: CGFloat) {
        var activity = SessionActivityPresentation(messages: [])
        activity.phase = .failed
        activity.title = "Run ended with an error"
        let measured = size(SessionActivityView(activity: activity), width: width - 32, type: .accessibility5)
        #expect(measured.width <= width - 32)
        #expect(measured.height <= 180)
        #expect(measured.height > 44)
    }
}
