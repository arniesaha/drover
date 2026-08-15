import SwiftUI
import Testing
import UIKit
@testable import Drover

/// The loading row under the cwd field, rendered for real.
///
/// It shipped as a non-`@ViewBuilder` computed property whose two `if` blocks
/// were plain statements and whose only `return` was `EmptyView()` — it type
/// checked, it read like it drew something, and it drew nothing. Nothing short
/// of a real layout and draw tells those two apart, and a height assertion
/// alone does not: a hosted `EmptyView` reports whatever height it was
/// offered. So these count the pixels the row actually puts on screen.
@MainActor
struct CwdSuggestionsStatusTests {
    private static let rowWidth: CGFloat = 320
    private static let offeredHeight: CGFloat = 200

    /// Non-transparent pixels in a render of the row over nothing. An
    /// `EmptyView` draws none.
    private func drawnPixels(isFetching: Bool, hasSuggestions: Bool) -> Int {
        let renderer = ImageRenderer(
            content: CwdSuggestionsStatus(isFetching: isFetching,
                                          hasSuggestions: hasSuggestions)
                .frame(width: Self.rowWidth)
                .environment(\.colorScheme, .dark)
        )
        renderer.scale = 1
        guard let cgImage = renderer.uiImage?.cgImage,
              cgImage.width > 0, cgImage.height > 0 else { return 0 }

        let width = cgImage.width, height = cgImage.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        guard let context = CGContext(
            data: &bytes, width: width, height: height,
            bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return 0 }
        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        var drawn = 0
        for alpha in stride(from: 3, to: bytes.count, by: 4) where bytes[alpha] > 0 {
            drawn += 1
        }
        return drawn
    }

    private func height(isFetching: Bool, hasSuggestions: Bool) -> CGFloat {
        let host = UIHostingController(
            rootView: CwdSuggestionsStatus(isFetching: isFetching,
                                           hasSuggestions: hasSuggestions))
        host.view.frame = CGRect(x: 0, y: 0, width: Self.rowWidth, height: Self.offeredHeight)
        host.view.layoutIfNeeded()
        return host.sizeThatFits(
            in: CGSize(width: Self.rowWidth, height: Self.offeredHeight)).height
    }

    /// The whole point of the row: while the fleet snapshot is on the wire the
    /// sheet says so, in words, rather than sitting there looking broken.
    @Test func aFirstFetchDrawsItsLabelledIndicator() {
        #expect(drawnPixels(isFetching: true, hasSuggestions: false) > 0)
    }

    /// Refreshing over paths already on screen still draws the spinner.
    @Test func aRefreshOverExistingSuggestionsDrawsItsIndicator() {
        #expect(drawnPixels(isFetching: true, hasSuggestions: true) > 0)
    }

    /// And it stays a row rather than growing into the sheet: an `EmptyView`
    /// hosted here reports the full 200pt it was offered, so this also fails
    /// if the row ever quietly reverts to drawing nothing.
    @Test func theIndicatorIsARowNotASlab() {
        let laidOut = height(isFetching: true, hasSuggestions: false)
        #expect(laidOut > 0)
        #expect(laidOut < Self.offeredHeight)
    }

    /// With nothing in flight it takes no space and draws nothing at all —
    /// this is a transient row, not a permanent gap under the cwd field.
    @Test func nothingInFlightDrawsNothing() {
        #expect(drawnPixels(isFetching: false, hasSuggestions: false) == 0)
        #expect(drawnPixels(isFetching: false, hasSuggestions: true) == 0)
        #expect(height(isFetching: false, hasSuggestions: false) == 0)
    }
}
