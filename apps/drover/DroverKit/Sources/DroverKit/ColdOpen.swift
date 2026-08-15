import Foundation

/// What a session screen shows while its stream has never attached.
///
/// The cold open used to have two states, not three: "not connected yet" and
/// "connected". With the fleet unreachable the first one never ended, so chat
/// held a spinner forever and terminal held a black screen, neither of them
/// saying anything — the reported symptom was read as a crash rather than as
/// an outage (#170).
public enum ColdOpenState: Sendable, Equatable {
    /// Nothing drawn. Either the wait is still short enough that a local open
    /// would have finished inside it, or the session has already attached.
    case quiet
    /// The spinner: still trying, and it has taken long enough to be worth
    /// acknowledging.
    case connecting
    /// Given up: the reason, and a retry. `detail` is a whole sentence, ready
    /// to render.
    case unreachable(detail: String)
}

/// Counts failed cold-open attempts and decides when a screen stops
/// pretending it is merely slow.
///
/// Shared by chat and terminal because they fail identically and should say
/// the same thing when they do; the counting lives here rather than on
/// `ChatModel` so the terminal screen, which has no model, can hold one in
/// `@State`.
public struct ColdOpenTracker: Sendable, Equatable {
    /// Nothing is drawn before this. The delay is the part that carries its
    /// weight: a loopback open lands in tens of milliseconds, and a spinner
    /// flashing on every one of those reads as jank rather than reassurance.
    public static let appearAfter: TimeInterval = 0.25

    /// How many failures it takes before the screen stops pretending it is
    /// merely slow. One blip is normal — a phone handing off between Wi-Fi
    /// and cellular loses a request and gets it back on the retry — so this
    /// holds the same line `SessionStore.connectingDetail` holds for the
    /// inbox (#85): quiet until the second failure.
    public static let failureLimit = 2

    /// The whole decision, in one place, so the two screens cannot drift.
    /// A session that has attached is done with all three states: from then
    /// on a dropped socket belongs to `ReconnectingPill`, which sits over a
    /// transcript the user can already read.
    public static func state(
        hasConnectedOnce: Bool, failure: String?, elapsed: TimeInterval
    ) -> ColdOpenState {
        guard !hasConnectedOnce else { return .quiet }
        if let failure { return .unreachable(detail: failure) }
        return elapsed >= appearAfter ? .connecting : .quiet
    }

    /// Consecutive failed attempts. Reset by a connection and by an explicit
    /// retry, so it only ever counts an unbroken run.
    public private(set) var failedAttempts = 0

    /// The newest attempt's reason, in the client's own vocabulary. Held from
    /// the first failure even though nothing shows it until the second.
    private var reason: String?

    public init() {}

    /// The line the screen shows once the failures have earned an
    /// explanation, or nil while a slow link still explains them on its own.
    public var detail: String? {
        guard failedAttempts >= Self.failureLimit, let reason else { return nil }
        return "\(failedAttempts) attempts · \(reason)"
    }

    /// The newest reason wins: a link that goes from unreachable to a 500 has
    /// stopped being a network problem, and the count beside it is what says
    /// "this is not a blip".
    public mutating func noteFailure(_ reason: String) {
        failedAttempts += 1
        self.reason = reason
    }

    public mutating func reset() {
        failedAttempts = 0
        reason = nil
    }
}
