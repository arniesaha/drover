import Foundation
import Testing
@testable import DroverKit

/// Two untagged favorites (one Linux-shaped, one macOS-shaped) plus one
/// tagged to "nas". The untagged pair is the bug: the server tags a favorite
/// only when the config names a host for it, so both used to be offered on
/// every host in the fleet — including the one where neither exists.
private let cwdCompletionSnapshotJSON = Data("""
{"hosts": [
  {"host_id": "work-laptop", "status": "online",
   "capabilities": {"display_name": "Work Laptop",
                    "harnesses": [{"name": "claude-code", "enabled": true}]}},
  {"host_id": "nas", "status": "online",
   "capabilities": {"display_name": "NAS",
                    "harnesses": [{"name": "claude-code", "enabled": true}]}}],
 "sessions": [],
 "cwd_suggestions": [
  {"path": "/home/arnab/dev/drover", "source": "favorite"},
  {"path": "/Users/arnabmac/jenny", "source": "favorite"},
  {"path": "/home/arnab/nas-only", "source": "recent session", "host_id": "nas"}]}
""".utf8)

/// Thread-safe: `MockURLProtocol.handler` runs off the main actor.
private final class RequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [URLRequest] = []

    func record(_ request: URLRequest) {
        lock.lock(); requests.append(request); lock.unlock()
    }

    var all: [URLRequest] {
        lock.lock(); defer { lock.unlock() }; return requests
    }

    var count: Int { all.count }
}

private func completionBody(parent: String, paths: [String]) -> Data {
    let entries = paths.map { path -> [String: String] in
        ["name": (path as NSString).lastPathComponent, "path": path]
    }
    let object: [String: Any] = [
        "parent": parent, "entries": entries, "truncated": false,
    ]
    return try! JSONSerialization.data(withJSONObject: object)
}

/// `.serialized`: these tests install the process-global
/// `MockURLProtocol.handler` — see `Fixtures.swift`.
extension MockNetworkTests {
@Suite(.serialized)
struct LaunchCwdCompletionTests {

private func testStore() -> HarnessModelCatalogStore {
    HarnessModelCatalogStore(
        defaults: UserDefaults(suiteName: "launch-cwd-test-\(UUID().uuidString)")!
    )
}

/// Short enough that a test does not spend the shipping 250 ms per
/// keystroke, long enough that three synchronous assignments still coalesce.
@MainActor private func model(debounce: Duration = .milliseconds(30)) throws -> LaunchModel {
    LaunchModel(
        client: client(),
        snapshot: try HarnessSnapshot.decode(from: cwdCompletionSnapshotJSON),
        store: testStore(),
        completionDebounce: debounce
    )
}

// MARK: - Host scoping

/// The reported bug: `/Users/arnabmac/jenny` was offered on a Linux laptop
/// that has no `/Users` at all, because an untagged favorite used to pass on
/// every host unconditionally.
@Test @MainActor func anUntaggedSuggestionTheHostLacksIsDropped() async throws {
    let model = try model()
    #expect(model.hostID == "work-laptop")
    #expect(model.cwdSuggestions == ["/home/arnab/dev/drover", "/Users/arnabmac/jenny"])

    let log = RequestLog()
    MockURLProtocol.handler = { request in
        log.record(request)
        return (200, Data("""
        {"exists": {"/home/arnab/dev/drover": true, "/Users/arnabmac/jenny": false}}
        """.utf8))
    }

    await model.verifyCuratedSuggestions()

    #expect(model.cwdSuggestions == ["/home/arnab/dev/drover"])
    // One batched round trip, not one per favorite, and only the ambiguous
    // (untagged) paths are asked about.
    #expect(log.count == 1)
    let request = try #require(log.all.first)
    #expect(request.url?.path == "/harness/hosts/work-laptop/fs/exists")
    #expect(request.httpMethod == "POST")
    let body = try JSONSerialization.jsonObject(with: request.bodyStreamData()) as? [String: Any]
    #expect(body?["paths"] as? [String] == ["/home/arnab/dev/drover", "/Users/arnabmac/jenny"])
}

/// Hiding every untagged favorite because the network blinked costs more
/// than showing one that turns out not to exist: the first breaks the field
/// for everyone, the second costs one failed launch.
@Test @MainActor func aFailedExistsCheckKeepsShowingUntaggedSuggestions() async throws {
    let model = try model()
    MockURLProtocol.handler = { _ in (502, Data(#"{"error": "host unreachable"}"#.utf8)) }

    await model.verifyCuratedSuggestions()

    #expect(model.cwdSuggestions == ["/home/arnab/dev/drover", "/Users/arnabmac/jenny"])
}

/// Regression guard for the half that always worked: a suggestion the server
/// tagged to another host must stay hidden, verified or not.
@Test @MainActor func aSuggestionTaggedToAnotherHostStaysFilteredOut() async throws {
    let model = try model()
    MockURLProtocol.handler = { _ in
        (200, Data(#"{"exists": {"/home/arnab/dev/drover": true, "/Users/arnabmac/jenny": true}}"#.utf8))
    }
    await model.verifyCuratedSuggestions()

    #expect(model.cwdSuggestions.contains("/home/arnab/nas-only") == false)

    // On its own host it is offered, and the previous host's verdicts do not
    // carry over to it.
    model.hostID = "nas"
    #expect(model.cwdSuggestions == [
        "/home/arnab/dev/drover", "/Users/arnabmac/jenny", "/home/arnab/nas-only",
    ])
}

// MARK: - Debounced live completion

/// One request for a burst of keystrokes. Per-keystroke fetches turned a
/// typed path into a dozen round trips over cellular.
@Test @MainActor func typingCoalescesIntoASingleFetch() async throws {
    let log = RequestLog()
    MockURLProtocol.handler = { request in
        log.record(request)
        return (200, completionBody(parent: "/home/arnab", paths: ["/home/arnab/dev"]))
    }
    let model = try model()

    model.cwd = "/h"
    model.cwd = "/ho"
    model.cwd = "/home/arnab/d"
    await model.settleCompletion()

    #expect(log.count == 1)
    let request = try #require(log.all.first)
    #expect(request.url?.path == "/harness/hosts/work-laptop/fs/complete")
    // The one request carries the newest text, not the first keystroke.
    #expect(request.url?.query == "path=%2Fhome%2Farnab%2Fd")
    #expect(model.liveCompletions == ["/home/arnab/dev"])
}

/// A slow answer is for text the user has already moved past, so it must not
/// land on top of the newer one — whether it loses the race to the newer
/// keystroke's cancellation or to the generation guard behind it.
///
/// The first request is left genuinely in flight (delivery held for 300 ms)
/// while the second is issued, typed, and answered.
@Test @MainActor func aSupersededResponseNeverOverwritesNewerResults() async throws {
    let slowStarted = MockFlag()
    MockURLProtocol.responseDelay = { request in
        guard request.url?.query?.contains("slow") == true else { return nil }
        slowStarted.raise()
        return 0.3
    }
    defer { MockURLProtocol.responseDelay = nil }
    MockURLProtocol.handler = { request in
        request.url?.query?.contains("slow") == true
            ? (200, completionBody(parent: "/slow", paths: ["/slow/entry"]))
            : (200, completionBody(parent: "/fast", paths: ["/fast/entry"]))
    }
    let model = try model(debounce: .milliseconds(10))

    model.cwd = "/slow"
    let deadline = Date().addingTimeInterval(2)
    while !slowStarted.isRaised, Date() < deadline {
        try await Task.sleep(for: .milliseconds(5))
    }
    #expect(slowStarted.isRaised, "the first completion request never reached the mock")

    model.cwd = "/fast"
    await model.settleCompletion()

    #expect(model.liveCompletions == ["/fast/entry"])
    // A request the app itself tore down is not a failure to report.
    #expect(model.isCompletionHostUnreachable == false)
    #expect(model.cwdSuggestionsHint == nil)

    // Outlive the held answer: if it could still overwrite the newer one,
    // this is where it would.
    try await Task.sleep(for: .milliseconds(400))
    #expect(model.liveCompletions == ["/fast/entry"])
}

/// An empty field has nothing to complete, and the curated list is already
/// the whole answer.
@Test @MainActor func anEmptyFieldShowsCuratedEntriesAndFetchesNothing() async throws {
    let log = RequestLog()
    MockURLProtocol.handler = { request in
        log.record(request)
        return (200, completionBody(parent: "/", paths: ["/should-never-be-asked-for"]))
    }
    let model = try model()

    model.cwd = "/x"
    model.cwd = ""
    await model.settleCompletion()
    try await Task.sleep(for: .milliseconds(80))

    #expect(log.count == 0)
    #expect(model.liveCompletions.isEmpty)
    #expect(model.cwdSuggestions == ["/home/arnab/dev/drover", "/Users/arnabmac/jenny"])
}

// MARK: - Merged ranking

/// Curated paths lead because they are the directories this fleet works in;
/// a sibling on disk that happens to sort earlier is not a better guess. A
/// directory that is both appears once, in the curated position.
@Test @MainActor func curatedEntriesRankAboveLiveOnesAndDuplicatesCollapse() async throws {
    MockURLProtocol.handler = { _ in
        (200, completionBody(parent: "/home/arnab", paths: [
            "/home/arnab/data", "/home/arnab/dev/drover",
        ]))
    }
    let model = try model()

    model.cwd = "/home/arnab/d"
    await model.settleCompletion()

    #expect(model.cwdSuggestions == [
        "/home/arnab/dev/drover",  // curated + on disk, once, first
        "/home/arnab/data",
    ])
    // The macOS favorite does not match the typed prefix and drops out.
    #expect(model.cwdSuggestions.contains("/Users/arnabmac/jenny") == false)
}

// MARK: - Offline hint

/// Silence reads as "no such directory". An unreachable host has to say so,
/// or the user retypes a path that was never the problem.
@Test @MainActor func aFailedCompletionSetsTheUnreachableHint() async throws {
    MockURLProtocol.handler = { _ in (504, Data(#"{"error": "host timed out"}"#.utf8)) }
    let model = try model()

    model.cwd = "/home/arnab/d"
    await model.settleCompletion()

    #expect(model.isCompletionHostUnreachable)
    #expect(model.cwdSuggestionsHint == "Can't reach the host — showing saved paths only")
    // The curated half of the list survives the outage.
    #expect(model.cwdSuggestions == ["/home/arnab/dev/drover"])
}

@Test @MainActor func aTransportFailureAlsoSetsTheHint() async throws {
    MockURLProtocol.transportError = URLError(.notConnectedToInternet)
    defer { MockURLProtocol.transportError = nil }
    let model = try model()

    model.cwd = "/home/arnab/d"
    await model.settleCompletion()

    #expect(model.isCompletionHostUnreachable)
}

/// A host that answered "nothing here" is not unreachable — it answered.
@Test @MainActor func aSuccessfulEmptyResultLeavesTheHintUnset() async throws {
    MockURLProtocol.handler = { _ in
        (200, Data(#"{"parent": "/home/arnab", "entries": [], "truncated": false}"#.utf8))
    }
    let model = try model()

    model.cwd = "/home/arnab/zz"
    await model.settleCompletion()

    #expect(model.liveCompletions.isEmpty)
    #expect(model.isCompletionHostUnreachable == false)
    #expect(model.cwdSuggestionsHint == nil)
}

/// A parent that does not exist is the normal state of a path being typed:
/// the server says so in-band with a 200, and the field must not accuse the
/// host of being down over it.
@Test @MainActor func aNotFoundParentIsNotTreatedAsAnOutage() async throws {
    MockURLProtocol.handler = { _ in
        (200, Data(#"{"parent": "/nope", "entries": [], "error": "not_found"}"#.utf8))
    }
    let model = try model()

    model.cwd = "/nope/x"
    await model.settleCompletion()

    #expect(model.isCompletionHostUnreachable == false)
}

/// The hint is a report on the last answer, not a sticky banner.
@Test @MainActor func aSucceedingRetryClearsTheHint() async throws {
    MockURLProtocol.handler = { _ in (502, Data()) }
    let model = try model()
    model.cwd = "/home/arnab/d"
    await model.settleCompletion()
    #expect(model.isCompletionHostUnreachable)

    MockURLProtocol.handler = { _ in
        (200, completionBody(parent: "/home/arnab", paths: ["/home/arnab/dev"]))
    }
    model.cwd = "/home/arnab/de"
    await model.settleCompletion()

    #expect(model.isCompletionHostUnreachable == false)
    #expect(model.liveCompletions == ["/home/arnab/dev"])
}

/// Switching hosts invalidates the previous host's listing immediately —
/// showing `/home/...` from the NAS while the Mac is selected is the same
/// class of wrongness this whole change is about.
@Test @MainActor func changingHostDropsTheOtherHostsLiveEntries() async throws {
    MockURLProtocol.handler = { _ in
        (200, completionBody(parent: "/home/arnab", paths: ["/home/arnab/dev"]))
    }
    let model = try model()
    model.cwd = "/home/arnab/d"
    await model.settleCompletion()
    #expect(model.liveCompletions == ["/home/arnab/dev"])

    model.hostID = "nas"
    #expect(model.liveCompletions.isEmpty)
}

}

}  // extension MockNetworkTests
