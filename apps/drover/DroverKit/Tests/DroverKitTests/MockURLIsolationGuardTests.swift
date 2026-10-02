import Foundation
import Testing
@testable import DroverKit

private final class GuardBarrier: @unchecked Sendable {
    static let shared = GuardBarrier()
    private let lock = NSLock()
    private var suiteAStarted = false
    private var suiteBStarted = false

    func signalAStarted() {
        lock.lock()
        suiteAStarted = true
        lock.unlock()
    }

    func signalBStarted() {
        lock.lock()
        suiteBStarted = true
        lock.unlock()
    }

    var bothStarted: Bool {
        lock.lock()
        defer { lock.unlock() }
        return suiteAStarted && suiteBStarted
    }
}

/// Guard Suite A: runs concurrently with Guard Suite B.
/// Exercises separate `MockNetwork` instances simultaneously to verify
/// that requests from Suite A never land in Suite B's handler.
@Suite struct MockURLIsolationGuardSuiteA {
    let mock = MockNetwork()

    @Test func suiteARequestsNeverHitSuiteB() async throws {
        GuardBarrier.shared.signalAStarted()
        mock.responseDelay = { _ in 0.05 }
        mock.handler = { request in
            #expect(request.url?.path == "/suite-a")
            #expect(request.url?.path.contains("suite-b") == false)
            return (200, Data("response-suite-a".utf8))
        }

        try await withThrowingTaskGroup(of: String.self) { group in
            for i in 0..<10 {
                group.addTask {
                    let url = URL(string: "http://test.local:7080/suite-a?req=\(i)")!
                    let (data, _) = try await self.mock.session().data(from: url)
                    return String(decoding: data, as: UTF8.self)
                }
            }
            for try await result in group {
                #expect(result == "response-suite-a")
            }
        }
    }
}

/// Guard Suite B: runs concurrently with Guard Suite A.
/// Exercises separate `MockNetwork` instances simultaneously to verify
/// that requests from Suite B never land in Suite A's handler.
@Suite struct MockURLIsolationGuardSuiteB {
    let mock = MockNetwork()

    @Test func suiteBRequestsNeverHitSuiteA() async throws {
        GuardBarrier.shared.signalBStarted()
        mock.responseDelay = { _ in 0.05 }
        mock.handler = { request in
            #expect(request.url?.path == "/suite-b")
            #expect(request.url?.path.contains("suite-a") == false)
            return (200, Data("response-suite-b".utf8))
        }

        try await withThrowingTaskGroup(of: String.self) { group in
            for i in 0..<10 {
                group.addTask {
                    let url = URL(string: "http://test.local:7080/suite-b?req=\(i)")!
                    let (data, _) = try await self.mock.session().data(from: url)
                    return String(decoding: data, as: UTF8.self)
                }
            }
            for try await result in group {
                #expect(result == "response-suite-b")
            }
        }
    }
}

/// Direct concurrent request interleaving across distinct `MockNetwork` instances.
@Suite struct MockURLConcurrentMocksGuardTests {
    @Test func concurrentMocksNeverCrossDeliver() async throws {
        let mockA = MockNetwork()
        let mockB = MockNetwork()

        mockA.responseDelay = { _ in 0.02 }
        mockB.responseDelay = { _ in 0.02 }

        mockA.handler = { req in
            #expect(req.url?.query?.contains("caller=A") == true)
            #expect(req.url?.query?.contains("caller=B") == false)
            return (200, Data("response-A".utf8))
        }
        mockB.handler = { req in
            #expect(req.url?.query?.contains("caller=B") == true)
            #expect(req.url?.query?.contains("caller=A") == false)
            return (200, Data("response-B".utf8))
        }

        try await withThrowingTaskGroup(of: Void.self) { group in
            for i in 0..<20 {
                group.addTask {
                    let urlA = URL(string: "http://test.local:7080/path?caller=A&i=\(i)")!
                    let (dataA, _) = try await mockA.session().data(from: urlA)
                    #expect(String(decoding: dataA, as: UTF8.self) == "response-A")
                }
                group.addTask {
                    let urlB = URL(string: "http://test.local:7080/path?caller=B&i=\(i)")!
                    let (dataB, _) = try await mockB.session().data(from: urlB)
                    #expect(String(decoding: dataB, as: UTF8.self) == "response-B")
                }
            }
            try await group.waitForAll()
        }
    }
}
