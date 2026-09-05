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

    @Test func replacementRejectsTemporaryPeakAndPreservesPreviousSnapshot() async throws {
        let root = try temporaryDirectory()
        let comparisonRoot = try temporaryDirectory()
        defer {
            try? FileManager.default.removeItem(at: root)
            try? FileManager.default.removeItem(at: comparisonRoot)
        }
        let key = recoveryKey()
        let old = ChatRecoverySnapshot(
            draftText: String(repeating: "a", count: 1_024),
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )
        let replacement = ChatRecoverySnapshot(
            draftText: String(repeating: "b", count: 1_024),
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )
        let durableStore = ChatRecoveryStore(root: root)
        let comparisonStore = ChatRecoveryStore(root: comparisonRoot)
        try await durableStore.save(old, for: key)
        try await comparisonStore.save(replacement, for: key)
        let finalBytes = try await comparisonStore.onDiskByteCount()
        let constrainedStore = ChatRecoveryStore(
            root: root,
            limits: .fixture(totalBytes: finalBytes)
        )

        await #expect(throws: ChatRecoveryError.quotaExceeded) {
            try await constrainedStore.save(replacement, for: key)
        }

        #expect(try await durableStore.load(for: key) == old)
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

    @Test func unreadableIndexPreservesPendingRecoveryBytes() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let key = recoveryKey()
        let pending = RecoveredPendingTurn(clientTurnID: UUID(), text: "synthetic pending")
        try await store.save(ChatRecoverySnapshot(draftText: "", pendingTurn: pending), for: key)
        let record = try #require(try recordURLs(in: root).first)
        let index = root.appendingPathComponent("recovery-index.json")
        try FileManager.default.removeItem(at: index)
        try FileManager.default.createDirectory(at: index, withIntermediateDirectories: false)

        await #expect(throws: ChatRecoveryError.storageUnavailable) {
            try await store.load(for: key)
        }
        #expect(FileManager.default.fileExists(atPath: record.path))
    }

    @Test func corruptIndexPreservesPendingRecoveryBytes() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let key = recoveryKey()
        let pending = RecoveredPendingTurn(clientTurnID: UUID(), text: "synthetic pending")
        try await store.save(ChatRecoverySnapshot(draftText: "", pendingTurn: pending), for: key)
        let record = try #require(try recordURLs(in: root).first)
        let index = root.appendingPathComponent("recovery-index.json")
        try Data("corrupt index".utf8).write(to: index)

        await #expect(throws: ChatRecoveryError.storageUnavailable) {
            try await store.load(for: key)
        }
        #expect(FileManager.default.fileExists(atPath: record.path))
    }

    @Test func indexWriteFailureAfterRecordCommitKeepsCompleteReplacement() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let faults = ChatRecoveryStoreFaults()
        let store = ChatRecoveryStore(root: root, faults: faults)
        let key = recoveryKey()
        let old = ChatRecoverySnapshot(
            draftText: "old",
            draftAttachments: [.init(mediaType: "image/jpeg", data: Data([0x01]))]
        )
        let replacement = ChatRecoverySnapshot(
            draftText: "replacement",
            draftAttachments: [.init(mediaType: "image/jpeg", data: Data([0x02, 0x03]))]
        )
        try await store.save(old, for: key)
        faults.failNextAfterIndexWrite()

        await #expect(throws: ChatRecoveryError.storageUnavailable) {
            try await store.save(replacement, for: key)
        }

        #expect(try await store.load(for: key) == replacement)
        #expect(try attachmentURLs(in: root).contains { url in
            try Data(contentsOf: url) == Data([0x02, 0x03])
        })
    }

    @Test func recordCommitFailureKeepsCompleteReplacement() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let faults = ChatRecoveryStoreFaults()
        let store = ChatRecoveryStore(root: root, faults: faults)
        let key = recoveryKey()
        let old = ChatRecoverySnapshot(
            draftText: "old",
            draftAttachments: [.init(mediaType: "image/jpeg", data: Data([0x01]))]
        )
        let replacement = ChatRecoverySnapshot(
            draftText: "replacement",
            draftAttachments: [.init(mediaType: "image/jpeg", data: Data([0x02, 0x03]))]
        )
        try await store.save(old, for: key)
        faults.failNextAfterRecordCommit()

        await #expect(throws: ChatRecoveryError.storageUnavailable) {
            try await store.save(replacement, for: key)
        }

        #expect(try await store.load(for: key) == replacement)
        #expect(try attachmentURLs(in: root).contains { url in
            try Data(contentsOf: url) == Data([0x02, 0x03])
        })
    }

    @Test func postTemporaryWriteFailureRemovesTemporaryPayload() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let faults = ChatRecoveryStoreFaults()
        let store = ChatRecoveryStore(root: root, faults: faults)
        faults.failNextAfterTemporaryWrite()

        await #expect(throws: ChatRecoveryError.storageUnavailable) {
            try await store.save(.draft("must not persist"), for: recoveryKey())
        }

        #expect(try filenames(in: root).allSatisfy { !$0.hasSuffix(".tmp") })
        #expect(try await store.load(for: recoveryKey()) == nil)
    }

    @Test func orphanedTemporaryBytesAreAccountedAndSwept() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        try await store.save(.draft("seed"), for: recoveryKey())
        let temporary = root.appendingPathComponent(".orphan.tmp")
        let orphanData = Data(repeating: 0xA5, count: 1_024)
        try orphanData.write(to: temporary)

        #expect(try await store.onDiskByteCount() >= orphanData.count)
        try await store.sweep(keeping: [])
        #expect(!FileManager.default.fileExists(atPath: temporary.path))
    }

    @Test func missingAttachmentIsDiscardedAsCorruptRecovery() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let snapshot = ChatRecoverySnapshot(
            draftText: "caption",
            draftAttachments: [.init(mediaType: "image/jpeg", data: Data([0x01]))]
        )
        try await store.save(snapshot, for: recoveryKey())
        let attachment = try #require(try attachmentURLs(in: root).first)
        try FileManager.default.removeItem(at: attachment)

        #expect(try await store.load(for: recoveryKey()) == nil)
        #expect(try recordURLs(in: root).isEmpty)
        #expect(try attachmentURLs(in: root).isEmpty)
    }

    @Test func oversizedAttachmentIsDiscardedAsCorruptRecovery() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let snapshot = ChatRecoverySnapshot(
            draftText: "caption",
            draftAttachments: [.init(mediaType: "image/jpeg", data: Data([0x01]))]
        )
        try await store.save(snapshot, for: recoveryKey())
        let attachment = try #require(try attachmentURLs(in: root).first)
        try Data(repeating: 0xEF, count: 6 * 1024 * 1024 + 1).write(to: attachment)

        #expect(try await store.load(for: recoveryKey()) == nil)
        #expect(try recordURLs(in: root).isEmpty)
        #expect(try attachmentURLs(in: root).isEmpty)
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

    @Test func upgradingADraftCannotCreateAFourthPendingTurn() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let binding = UUID()
        let draftKey = recoveryKey(binding: binding, sessionID: "draft-to-upgrade")
        try await store.save(.draft("draft"), for: draftKey)
        for sessionID in ["pending-one", "pending-two", "pending-three"] {
            try await store.save(pendingSnapshot(), for: recoveryKey(binding: binding, sessionID: sessionID))
        }

        await #expect(throws: ChatRecoveryError.quotaExceeded) {
            try await store.save(pendingSnapshot(), for: draftKey)
        }
        #expect(try await store.load(for: draftKey)?.pendingTurn == nil)
    }

    @Test func draftOnlyRecordsDoNotConsumeTheFirstPendingSlot() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let store = ChatRecoveryStore(root: root)
        let binding = UUID()
        for sessionID in ["draft-one", "draft-two", "draft-three"] {
            try await store.save(.draft(sessionID), for: recoveryKey(binding: binding, sessionID: sessionID))
        }
        let pendingKey = recoveryKey(binding: binding, sessionID: "first-pending")

        try await store.save(pendingSnapshot(), for: pendingKey)

        #expect(try await store.load(for: pendingKey)?.pendingTurn != nil)
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

private func pendingSnapshot() -> ChatRecoverySnapshot {
    ChatRecoverySnapshot(
        draftText: "",
        pendingTurn: RecoveredPendingTurn(clientTurnID: UUID(), text: "synthetic pending")
    )
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

private func attachmentURLs(in root: URL) throws -> [URL] {
    try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        .filter { $0.pathExtension == "attachment" }
}
