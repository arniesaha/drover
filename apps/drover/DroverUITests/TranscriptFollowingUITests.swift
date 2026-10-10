import XCTest

final class TranscriptFollowingUITests: XCTestCase {
    @MainActor
    func testDetachedReaderSurvivesEventsKeyboardRotationAndBackgroundThenResumes() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment["DROVER_UI_TEST_SCENARIO"] = "core-journey"
        app.launchEnvironment["DROVER_UI_TEST_RUN_ID"] = UUID().uuidString
        app.launchEnvironment["DROVER_UI_TEST_TRANSCRIPT_STREAM"] = "1"
        app.launch()
        let session = app.buttons["fixture-session"]
        XCTAssertTrue(session.waitForExistence(timeout: 5))
        session.tap()
        let transcript = app.scrollViews.firstMatch
        XCTAssertTrue(transcript.waitForExistence(timeout: 5))
        let latestAtOpen = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Reading marker 40.")).firstMatch
        XCTAssertTrue(latestAtOpen.waitForExistence(timeout: 5))
        transcript.swipeDown()
        transcript.swipeDown()
        let jump = app.buttons["chat-scroll-to-bottom"]
        XCTAssertTrue(jump.waitForExistence(timeout: 3))
        let readingTop = app.navigationBars.firstMatch.frame.maxY + 20
        let visible = app.staticTexts.allElementsBoundByIndex.filter {
            $0.label.hasPrefix("Reading marker") && $0.isHittable
                && $0.frame.minY > readingTop
        }
        XCTAssertFalse(visible.isEmpty)
        guard let markerLabel = visible.first?.label else { return }
        // Use a stable label query. Keyboard elements change the global text indexes.
        let marker = app.staticTexts.matching(NSPredicate(format: "label == %@", markerLabel)).firstMatch
        let initialY = marker.frame.minY
        // Wait for a real unread event, then confirm it did not move the reader.
        let unread = NSPredicate { _, _ in
            guard let value = jump.value as? String else { return false }
            return value.contains("new events")
        }
        expectation(for: unread, evaluatedWith: jump)
        waitForExpectations(timeout: 5)
        XCTAssertEqual(marker.frame.minY, initialY, accuracy: 2)
        XCTAssertEqual(jump.label, "Jump to latest")

        let composer = app.textFields["composer-input"]
        composer.tap()
        XCTAssertTrue(app.buttons["keyboard-dismiss"].waitForExistence(timeout: 3))
        XCTAssertTrue(jump.exists)
        XCTAssertEqual(marker.frame.minY, initialY, accuracy: 2)
        // Rotate and restore while the keyboard is visible.
        XCUIDevice.shared.orientation = .landscapeLeft
        XCTAssertTrue(jump.waitForExistence(timeout: 3))
        XCUIDevice.shared.orientation = .portrait
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(jump.waitForExistence(timeout: 5))
        XCTAssertEqual(marker.frame.minY, initialY, accuracy: 2)
        app.buttons["keyboard-dismiss"].tap()
        XCTAssertEqual(marker.frame.minY, initialY, accuracy: 2)
        jump.tap()
        XCTAssertTrue(jump.waitForNonExistence(timeout: 5))

        // A manual return to the bottom also clears detachment.
        let start = transcript.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.4))
        let end = transcript.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.55))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0.2)
        XCTAssertTrue(jump.waitForExistence(timeout: 3))
        for _ in 0..<4 where jump.exists { transcript.swipeUp() }
        XCTAssertTrue(jump.waitForNonExistence(timeout: 5))
    }
}
