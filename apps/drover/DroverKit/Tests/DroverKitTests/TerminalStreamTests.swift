import Foundation
import Testing
@testable import DroverKit

// MARK: - Fake terminal connector

/// Scripted fake mirroring `FakeConnector` in StreamTests: each `connect()`
/// pops the next scenario and records every outgoing frame per connection.
final class FakeTerminalConnector: TerminalConnecting, @unchecked Sendable {
    enum Scenario { case frames([String], thenError: Bool) }
    var scenarios: [Scenario]
    /// Outgoing frames, one array per connect() call, in order.
    var sent: [[String]] = []
    private(set) var connectCount = 0

    init(_ scenarios: [Scenario]) { self.scenarios = scenarios }

    func connect(_ request: URLRequest) -> TerminalConnection {
        let scenario = scenarios.isEmpty ? .frames([], thenError: false) : scenarios.removeFirst()
        let index = connectCount
        connectCount += 1
        sent.append([])
        let frames = AsyncThrowingStream<String, Error> { continuation in
            guard case let .frames(frames, thenError) = scenario else { return }
            for frame in frames { continuation.yield(frame) }
            if thenError {
                continuation.finish(throwing: URLError(.networkConnectionLost))
            }
            // no finish otherwise: socket stays open
        }
        return TerminalConnection(frames: frames) { [weak self] frame in
            self?.sent[index].append(frame)
        }
    }
}

private let attachedFrame = #"{"type": "attached", "session_id": "s1"}"#
private func outputFrame(_ text: String) -> String {
    #"{"type": "output", "data": "\#(text)"}"#
}
private let exitFrame = #"{"type": "exit"}"#

struct TerminalStreamTests {

@Test func outputFramesFeedThrough() async throws {
    let connector = FakeTerminalConnector([
        .frames([attachedFrame, outputFrame("hello")], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    var outputs: [String] = []
    var cameUp = false
    for await event in await stream.events() {
        switch event {
        case .connection(true): cameUp = true
        case .output(let text): outputs.append(text)
        default: break
        }
        if !outputs.isEmpty { break }
    }
    #expect(cameUp)
    #expect(outputs == ["hello"])
}

@Test func exitStopsStreamWithoutReconnect() async throws {
    let connector = FakeTerminalConnector([
        .frames([attachedFrame, outputFrame("bye"), exitFrame], thenError: false),
        .frames([attachedFrame], thenError: false),   // must never be reached
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    var events: [TerminalStreamEvent] = []
    // No break: the stream must finish on its own after `exit`.
    for await event in await stream.events() {
        events.append(event)
    }
    #expect(events.contains(.exited))
    #expect(connector.connectCount == 1)
}

@Test func socketDropReconnectsAndComesBackUp() async throws {
    let connector = FakeTerminalConnector([
        .frames([attachedFrame, outputFrame("one")], thenError: true),
        .frames([attachedFrame, outputFrame("two")], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    var outputs: [String] = []
    var sawDown = false
    var sawBackUp = false
    for await event in await stream.events() {
        switch event {
        case .output(let text): outputs.append(text)
        case .connection(let up):
            if !up { sawDown = true }
            else if sawDown { sawBackUp = true }
        default: break
        }
        if outputs.count == 2 { break }
    }
    #expect(outputs == ["one", "two"])
    #expect(sawDown)
    #expect(sawBackUp)
}

/// The terminal's half of #170. A PTY that never attaches shows a black
/// screen, which is even quieter than chat's spinner — nothing on it changes
/// to suggest anything is being attempted.
@Test func aConnectionThatNeverAttachedSaysWhy() async throws {
    let connector = FakeTerminalConnector([
        .frames([], thenError: true),                                 // never attached
        .frames([attachedFrame, outputFrame("up")], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    var failures: [String] = []
    for await event in await stream.events() {
        if case let .connectFailed(reason) = event { failures.append(reason) }
        if case .output = event { break }
    }
    #expect(failures == ["Can't reach the hub"])
}

/// The daemon keeps the PTY alive across client drops, so a socket that dies
/// after the terminal was live is a reconnect, not a cold open that failed.
/// Only an attempt that never saw a frame counts.
@Test func aDropAfterAttachingIsNotAColdOpenFailure() async throws {
    let connector = FakeTerminalConnector([
        .frames([attachedFrame, outputFrame("one")], thenError: true),
        .frames([attachedFrame, outputFrame("two")], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    var outputs: [String] = []
    var failures: [String] = []
    for await event in await stream.events() {
        switch event {
        case .output(let text): outputs.append(text)
        case .connectFailed(let reason): failures.append(reason)
        default: break
        }
        if outputs.count == 2 { break }
    }
    #expect(failures.isEmpty)
}

@Test func lastResizeIsResentOnReconnect() async throws {
    let connector = FakeTerminalConnector([
        .frames([attachedFrame], thenError: true),
        .frames([attachedFrame, outputFrame("back")], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    stream.sendResize(rows: 40, cols: 120)
    for await event in await stream.events() {
        if case .output = event { break }   // reached the second connection
    }
    let expected = TerminalWire.resizeFrame(rows: 40, cols: 120)
    #expect(connector.sent.count == 2)
    #expect(connector.sent[1].contains(expected))
}

@Test func sendInputGoesToCurrentConnection() async throws {
    let connector = FakeTerminalConnector([
        .frames([attachedFrame], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .milliseconds(10))
    var sentWhileUp = false
    // Send while the loop (and therefore the connection) is still live —
    // breaking out of `events()` tears the stream down by design.
    for await event in await stream.events() {
        if case .connection(true) = event {
            stream.send(TerminalWire.inputFrame("ls\n"))
            sentWhileUp = true
            break
        }
    }
    #expect(sentWhileUp)
    #expect(connector.sent[0].contains(TerminalWire.inputFrame("ls\n")))
}

// -- Retry while an attempt is already running (#170 follow-up) -------------
//
// `nudge()` cancelled the backoff sleeper and nothing else, so it only had an
// effect while the pump happened to be sleeping. Pressed during an attempt
// there was no sleeper to cancel, and the Retry button the cold-open failure
// state offers did nothing at all: the user got the spinner back and the
// same wait they already had.

@Test func retryDuringAnAttemptSkipsTheNextBackoff() async throws {
    let connector = FakeTerminalConnector([
        .frames([], thenError: true),        // fails without ever attaching
        .frames([attachedFrame], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .seconds(30))

    await stream.nudge()

    // Elapsed time is the whole assertion. "It reconnected eventually" is
    // true with or without the fix -- the first version of this test passed
    // in 31.6 seconds, having waited out the very backoff it meant to prove
    // was skipped.
    let started = ContinuousClock.now
    var connected = false
    for await event in await stream.events() {
        if case .connection(true) = event { connected = true; break }
    }
    let elapsed = ContinuousClock.now - started

    #expect(connected)
    #expect(connector.connectCount == 2)
    #expect(elapsed < .seconds(5), "waited \(elapsed) for a retry that should not have slept")
}

@Test func retryIsSpentOnceRatherThanDisablingBackoffForGood() async throws {
    // Skipping every later backoff would turn one tap into a reconnect spin
    // against a fleet that is still down.
    let connector = FakeTerminalConnector([
        .frames([], thenError: true),
        .frames([], thenError: true),
        .frames([attachedFrame], thenError: false),
    ])
    let stream = TerminalStream(request: URLRequest(url: URL(string: "ws://test.local/t")!),
                                connector: connector,
                                reconnectBaseDelay: .seconds(30))

    await stream.nudge()

    let started = ContinuousClock.now
    var failures = 0
    for await event in await stream.events() {
        if case .connectFailed = event {
            failures += 1
            if failures == 2 { break }
        }
    }
    let elapsed = ContinuousClock.now - started

    // Two failures arrive quickly because the retry skipped the first
    // backoff. The third attempt is still sleeping when this returns, which
    // is the point: the request was spent, not stored.
    #expect(failures == 2)
    #expect(connector.connectCount == 2)
    #expect(elapsed < .seconds(5))
}

}
