import Foundation
import Testing
@testable import DroverKit

// MARK: - Payload

@Test func theSessionIdKeyMatchesTheServerPayload() {
    // The hub writes "session_id" into the APNs payload; a rename on either
    // side makes every push tap land on the list instead of the session.
    #expect(NotificationPayloadKey.sessionID == "session_id")
}

@Test func aPushPayloadResolvesToItsSession() {
    let id = NotificationRoute.sessionID(
        userInfo: ["session_id": "harness-abc"], requestIdentifier: "ignored"
    )

    #expect(id == "harness-abc")
}

@Test func aLocalNotificationFallsBackToItsRequestIdentifier() {
    // LocalNotifier has always used the session id as the identifier, so
    // alerts scheduled before the userInfo key existed stay tappable.
    let id = NotificationRoute.sessionID(userInfo: [:], requestIdentifier: "harness-xyz")

    #expect(id == "harness-xyz")
}

@Test func anEmptyPayloadValueDoesNotBeatTheIdentifier() {
    let id = NotificationRoute.sessionID(
        userInfo: ["session_id": "   "], requestIdentifier: "harness-real"
    )

    #expect(id == "harness-real")
}

@Test func aNotificationWithNothingUsableResolvesToNil() {
    #expect(NotificationRoute.sessionID(userInfo: [:], requestIdentifier: "  ") == nil)
}

@Test func aNonStringPayloadValueIsIgnored() {
    let id = NotificationRoute.sessionID(
        userInfo: ["session_id": 42], requestIdentifier: "harness-real"
    )

    #expect(id == "harness-real")
}

// MARK: - Route

@MainActor
@Test func aTapIsHeldUntilSomethingCanNavigate() {
    let route = NotificationRoute()
    route.open(sessionID: "harness-1")

    #expect(route.pendingSessionID == "harness-1")
}

@MainActor
@Test func consumingATapClearsIt() {
    let route = NotificationRoute()
    route.open(sessionID: "harness-1")

    #expect(route.consume() == "harness-1")
    // Otherwise every list refresh would re-navigate to the same session.
    #expect(route.pendingSessionID == nil)
    #expect(route.consume() == nil)
}

@MainActor
@Test func anEmptyIdIsNotAPendingTap() {
    let route = NotificationRoute()
    route.open(sessionID: "   ")

    #expect(route.pendingSessionID == nil)
}

@MainActor
@Test func aSecondTapSupersedesAnUnconsumedFirst() {
    let route = NotificationRoute()
    route.open(sessionID: "harness-1")
    route.open(sessionID: "harness-2")

    // Two alerts tapped in a row should land on the one the user chose last.
    #expect(route.consume() == "harness-2")
}

@Test func notificationModeDefaultsAndPersistence() {
    let name = "NotificationModeTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: name)!
    defer { defaults.removePersistentDomain(forName: name) }
    #expect(NotificationMode.saved(in: defaults) == .action)
    for mode in NotificationMode.allCases {
        mode.save(in: defaults)
        #expect(NotificationMode.saved(in: UserDefaults(suiteName: name)!) == mode)
    }
    defaults.set("invalid", forKey: "drover.push.mode")
    #expect(NotificationMode.saved(in: defaults) == .action)
}

@Test func summaryPayloadRoutesOnlyToItsSessions() {
    let payload: [AnyHashable: Any] = ["session_ids": [" second ", "first", "first", " "]]
    #expect(NotificationRoute.sessionIDs(userInfo: payload) == ["first", "second"])
    #expect(NotificationRoute.sessionID(userInfo: payload, requestIdentifier: "apns-request") == nil)
    #expect(NotificationRoute.sessionIDs(userInfo: ["session_ids": [" "]]) == nil)
    #expect(NotificationRoute.sessionIDs(userInfo: ["session_ids": "wrong type"]) == nil)
}

@Test @MainActor func summaryRouteSurvivesUntilConsumedAndReplacesSingleRoute() {
    let route = NotificationRoute()
    route.open(sessionID: "first")
    route.open(sessionIDs: ["second", "first", "second"])
    #expect(route.pendingSessionID == nil)
    #expect(route.pendingSessionIDs == ["first", "second"])
    #expect(route.consumeSummary() == ["first", "second"])
    #expect(route.consumeSummary() == nil)
    route.open(sessionIDs: ["first", "second"])
    route.open(sessionID: "third")
    #expect(route.pendingSessionIDs == nil)
    #expect(route.consume() == "third")
}
