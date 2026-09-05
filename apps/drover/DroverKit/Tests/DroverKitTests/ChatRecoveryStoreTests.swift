import Foundation
import Testing
@testable import DroverKit

@Suite(.serialized)
struct ChatRecoveryStoreTests {
    @Test func recordRoundTripsWithoutPuttingScopeInItsPath() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let key = recoveryKey(sessionID: "session-1")
        let snapshot = ChatRecoverySnapshot(
            draftText: "keep this",
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )

        try await store.save(snapshot, for: key)

        #expect(try await store.load(for: key) == snapshot)
        #expect(try filenames(in: root).allSatisfy {
            !$0.contains("private.example") && !$0.contains("session-1")
        })
    }

    @Test func attachmentsRoundTripFromProtectedSiblingFiles() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let attachment = RecoveredTurnAttachment(
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!,
            mediaType: "image/jpeg",
            data: Data([0x01, 0x02, 0x03])
        )
        let snapshot = ChatRecoverySnapshot(
            draftText: "caption",
            draftAttachments: [attachment],
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )

        try await store.save(snapshot, for: recoveryKey())

        #expect(try await store.load(for: recoveryKey()) == snapshot)
    }

    @Test func quotaRejectionLeavesNoRecordBehind() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root, limits: .fixture(totalBytes: 1))
        let key = recoveryKey()

        await #expect(throws: ChatRecoveryError.quotaExceeded) {
            try await store.save(.draft("two"), for: key)
        }

        #expect(try await store.load(for: key) == nil)
    }

    @Test func storageFailureLeavesNoRecordAtBlockedRoot() async throws {
        let parent = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: parent) }
        let blockedRoot = parent.appendingPathComponent("not-a-directory")
        try Data().write(to: blockedRoot)
        let store = ChatRecoveryStore(root: blockedRoot)

        await #expect(throws: ChatRecoveryError.storageUnavailable) {
            try await store.save(.draft("must not persist"), for: recoveryKey())
        }

        #expect(try Data(contentsOf: blockedRoot).isEmpty)
    }

    @Test func corruptRecordIsRemovedWithoutBlockingFutureRecovery() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let key = recoveryKey()
        try await store.save(.draft("recoverable"), for: key)
        let record = try #require(try recordURLs(in: root).first)
        try Data("not json".utf8).write(to: record)

        #expect(try await store.load(for: key) == nil)
        #expect(try recordURLs(in: root).isEmpty)
    }

    @Test func unsupportedSnapshotVersionIsDiscarded() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let key = recoveryKey()
        try await store.save(.draft("recoverable"), for: key)
        let record = try #require(try recordURLs(in: root).first)
        var object = try #require(
            JSONSerialization.jsonObject(with: Data(contentsOf: record)) as? [String: Any]
        )
        object["version"] = 99
        try JSONSerialization.data(withJSONObject: object).write(to: record)

        #expect(try await store.load(for: key) == nil)
        #expect(try recordURLs(in: root).isEmpty)
    }

    @Test func staleDraftsEvictButUnresolvedTurnsSurviveTheSweep() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let binding = UUID(uuidString: "00000000-0000-0000-0000-000000000010")!
        let oldDate = Date.now.addingTimeInterval(-8 * 24 * 60 * 60)
        let staleDraftKey = recoveryKey(binding: binding, sessionID: "stale-draft")
        let pendingKey = recoveryKey(binding: binding, sessionID: "pending")
        let freshKey = recoveryKey(binding: binding, sessionID: "fresh")
        let pending = RecoveredPendingTurn(
            clientTurnID: UUID(uuidString: "00000000-0000-0000-0000-000000000011")!,
            text: "awaiting echo",
            attachments: []
        )
        try await store.save(.draft("old", updatedAt: oldDate), for: staleDraftKey)
        try await store.save(ChatRecoverySnapshot(
            draftText: "",
            pendingTurn: pending,
            updatedAt: oldDate
        ), for: pendingKey)

        try await store.save(.draft("current"), for: freshKey)

        #expect(try await store.load(for: staleDraftKey) == nil)
        #expect(try await store.load(for: pendingKey)?.pendingTurn == pending)
    }

    @Test func purgeAndSweepOnlyRemoveBindingsOutsideTheKeptSet() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let kept = UUID(uuidString: "00000000-0000-0000-0000-000000000020")!
        let removed = UUID(uuidString: "00000000-0000-0000-0000-000000000021")!
        let keptKey = recoveryKey(binding: kept, sessionID: "kept")
        let removedKey = recoveryKey(binding: removed, sessionID: "removed")
        try await store.save(.draft("keep"), for: keptKey)
        try await store.save(.draft("remove"), for: removedKey)

        try await store.sweep(keeping: [kept])

        #expect(try await store.load(for: keptKey)?.draftText == "keep")
        #expect(try await store.load(for: removedKey) == nil)

        try await store.purge(bindingID: kept)
        #expect(try await store.load(for: keptKey) == nil)
    }
}

private extension ChatRecoverySnapshot {
    static func draft(_ text: String, updatedAt: Date = .now) -> Self {
        Self(draftText: text, updatedAt: updatedAt)
    }
}

private extension ChatRecoveryLimits {
    static func fixture(totalBytes: Int) -> Self {
        Self(
            maximumDraftBytes: 64 * 1024,
            maximumCompositionAttachmentBytes: 6 * 1024 * 1024,
            maximumTotalBytes: totalBytes,
            maximumUnresolvedRecords: 3,
            maximumDraftAge: 7 * 24 * 60 * 60
        )
    }
}

private func temporaryDirectory() throws -> URL {
    let base = ProcessInfo.processInfo.environment["DROVER_TEST_TMPDIR"].map {
        URL(fileURLWithPath: $0, isDirectory: true)
    } ?? FileManager.default.temporaryDirectory
    let directory = base.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    return directory
}

private func recoveryKey(
    binding: UUID = UUID(uuidString: "00000000-0000-0000-0000-000000000000")!,
    sessionID: String = "session"
) -> ChatRecoveryKey {
    ChatRecoveryKey(
        serverURL: URL(string: "http://private.example:7080")!,
        credentialBindingID: binding,
        sessionID: sessionID
    )
}

private func filenames(in root: URL) throws -> [String] {
    try FileManager.default.contentsOfDirectory(atPath: root.path)
}

private func recordURLs(in root: URL) throws -> [URL] {
    try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        .filter { $0.pathExtension == "record" }
}
