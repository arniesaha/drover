import CryptoKit
import Foundation

public struct ChatRecoveryLimits: Sendable, Equatable {
    public let maximumDraftBytes: Int
    public let maximumCompositionAttachmentBytes: Int
    public let maximumTotalBytes: Int
    public let maximumUnresolvedRecords: Int
    public let maximumDraftAge: TimeInterval

    public init(
        maximumDraftBytes: Int,
        maximumCompositionAttachmentBytes: Int,
        maximumTotalBytes: Int,
        maximumUnresolvedRecords: Int,
        maximumDraftAge: TimeInterval
    ) {
        self.maximumDraftBytes = maximumDraftBytes
        self.maximumCompositionAttachmentBytes = maximumCompositionAttachmentBytes
        self.maximumTotalBytes = maximumTotalBytes
        self.maximumUnresolvedRecords = maximumUnresolvedRecords
        self.maximumDraftAge = maximumDraftAge
    }

    public static let `default` = ChatRecoveryLimits(
        maximumDraftBytes: 64 * 1024,
        maximumCompositionAttachmentBytes: 6 * 1024 * 1024,
        maximumTotalBytes: 24 * 1024 * 1024,
        maximumUnresolvedRecords: 3,
        maximumDraftAge: 7 * 24 * 60 * 60
    )
}

public enum ChatRecoveryError: Error, Equatable, Sendable {
    case quotaExceeded
    case storageUnavailable
    case invalidRecord
}

/// Internal deterministic fault injection for the recovery store's focused
/// tests. It is deliberately unavailable through the public initializer.
final class ChatRecoveryStoreFaults: @unchecked Sendable {
    private let lock = NSLock()
    private var temporaryWriteFailures = 0
    private var indexWriteFailures = 0
    private var postIndexWriteFailures = 0
    private var postRecordCommitFailures = 0
    private var eraseAllFailures = 0
    private var recoveryFileRemovalFailures = 0

    func failNextAfterTemporaryWrite() {
        lock.withLock { temporaryWriteFailures += 1 }
    }

    func failNextIndexWrite() {
        lock.withLock { indexWriteFailures += 1 }
    }

    func failNextAfterIndexWrite() {
        lock.withLock { postIndexWriteFailures += 1 }
    }

    func failNextAfterRecordCommit() {
        lock.withLock { postRecordCommitFailures += 1 }
    }

    func failNextEraseAll() {
        lock.withLock { eraseAllFailures += 1 }
    }

    func failNextRecoveryFileRemoval() {
        lock.withLock { recoveryFileRemovalFailures += 1 }
    }

    fileprivate func consumeTemporaryWriteFailure() -> Bool {
        lock.withLock {
            guard temporaryWriteFailures > 0 else { return false }
            temporaryWriteFailures -= 1
            return true
        }
    }

    fileprivate func consumeIndexWriteFailure() -> Bool {
        lock.withLock {
            guard indexWriteFailures > 0 else { return false }
            indexWriteFailures -= 1
            return true
        }
    }

    fileprivate func consumePostIndexWriteFailure() -> Bool {
        lock.withLock {
            guard postIndexWriteFailures > 0 else { return false }
            postIndexWriteFailures -= 1
            return true
        }
    }

    fileprivate func consumePostRecordCommitFailure() -> Bool {
        lock.withLock {
            guard postRecordCommitFailures > 0 else { return false }
            postRecordCommitFailures -= 1
            return true
        }
    }

    fileprivate func consumeEraseAllFailure() -> Bool {
        lock.withLock {
            guard eraseAllFailures > 0 else { return false }
            eraseAllFailures -= 1
            return true
        }
    }

    fileprivate func consumeRecoveryFileRemovalFailure() -> Bool {
        lock.withLock {
            guard recoveryFileRemovalFailures > 0 else { return false }
            recoveryFileRemovalFailures -= 1
            return true
        }
    }
}

/// Exact recovery scope. Only a digest of this value reaches the filesystem.
public struct ChatRecoveryKey: Sendable, Equatable, Hashable {
    public let credentialBindingID: UUID
    public let sessionID: String
    private let normalizedServerURL: String

    public init(serverURL: URL, credentialBindingID: UUID, sessionID: String) {
        self.credentialBindingID = credentialBindingID
        self.sessionID = sessionID
        normalizedServerURL = RecoveryBindingStore.normalizedServerURL(serverURL)
    }

    fileprivate var filename: String {
        let scope = "\(normalizedServerURL)\u{1F}\(credentialBindingID.uuidString)\u{1F}\(sessionID)"
        return SHA256.hash(data: Data(scope.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

/// A recovery attachment keeps its raw bytes in memory only. Codable metadata
/// contains the opaque ID and MIME type; `ChatRecoveryStore` writes the bytes
/// to a protected sibling file and rehydrates them on load.
public struct RecoveredTurnAttachment: Codable, Equatable, Sendable {
    public let id: UUID
    public let mediaType: String
    public var data: Data

    public init(id: UUID = UUID(), mediaType: String, data: Data) {
        self.id = id
        self.mediaType = mediaType
        self.data = data
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case mediaType
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        mediaType = try container.decode(String.self, forKey: .mediaType)
        data = Data()
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(mediaType, forKey: .mediaType)
    }
}

public struct RecoveredDeferredTurn: Codable, Equatable, Sendable {
    public var text: String
    public var attachments: [RecoveredTurnAttachment]

    public init(text: String, attachments: [RecoveredTurnAttachment] = []) {
        self.text = text
        self.attachments = attachments
    }
}

public struct RecoveredPendingTurn: Codable, Equatable, Sendable {
    public var clientTurnID: UUID
    public var text: String
    public var attachments: [RecoveredTurnAttachment]

    public init(clientTurnID: UUID, text: String, attachments: [RecoveredTurnAttachment] = []) {
        self.clientTurnID = clientTurnID
        self.text = text
        self.attachments = attachments
    }
}

public struct ChatRecoverySnapshot: Codable, Equatable, Sendable {
    private static let version = 1

    public var draftText: String
    public var draftAttachments: [RecoveredTurnAttachment]
    public var deferredTurn: RecoveredDeferredTurn?
    public var pendingTurn: RecoveredPendingTurn?
    public var updatedAt: Date

    public init(
        draftText: String,
        draftAttachments: [RecoveredTurnAttachment] = [],
        deferredTurn: RecoveredDeferredTurn? = nil,
        pendingTurn: RecoveredPendingTurn? = nil,
        updatedAt: Date = .now
    ) {
        self.draftText = draftText
        self.draftAttachments = draftAttachments
        self.deferredTurn = deferredTurn
        self.pendingTurn = pendingTurn
        self.updatedAt = updatedAt
    }

    private enum CodingKeys: String, CodingKey {
        case version
        case draftText
        case draftAttachments
        case deferredTurn
        case pendingTurn
        case updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        guard try container.decode(Int.self, forKey: .version) == Self.version else {
            throw ChatRecoveryError.invalidRecord
        }
        draftText = try container.decode(String.self, forKey: .draftText)
        draftAttachments = try container.decode([RecoveredTurnAttachment].self, forKey: .draftAttachments)
        deferredTurn = try container.decodeIfPresent(RecoveredDeferredTurn.self, forKey: .deferredTurn)
        pendingTurn = try container.decodeIfPresent(RecoveredPendingTurn.self, forKey: .pendingTurn)
        updatedAt = try container.decode(Date.self, forKey: .updatedAt)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(Self.version, forKey: .version)
        try container.encode(draftText, forKey: .draftText)
        try container.encode(draftAttachments, forKey: .draftAttachments)
        try container.encodeIfPresent(deferredTurn, forKey: .deferredTurn)
        try container.encodeIfPresent(pendingTurn, forKey: .pendingTurn)
        try container.encode(updatedAt, forKey: .updatedAt)
    }
}

public protocol ChatRecoveryPersisting: Sendable {
    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot?
    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws
    func remove(for key: ChatRecoveryKey) async throws
    func purge(bindingID: UUID) async throws
    func sweep(keeping bindingIDs: Set<UUID>) async throws
    /// Destructively removes the entire store only after raw credential
    /// deletion has made every recovery binding unauthorized.
    func eraseAllAfterCredentialDeletion() async throws
}

/// A bounded, protected local store. It has no server identity in filenames or
/// record bodies; a private index maps opaque record digests to a binding UUID
/// solely so disconnect and orphan cleanup can be exact.
public actor ChatRecoveryStore: ChatRecoveryPersisting {
    private static let indexFilename = "recovery-index.json"
    private static let recordExtension = "record"
    private static let attachmentExtension = "attachment"
    private static let indexVersion = 1

    private let root: URL
    private let limits: ChatRecoveryLimits
    private let fileManager = FileManager.default
    private let faults: ChatRecoveryStoreFaults?

    public init(root: URL, limits: ChatRecoveryLimits = .default) {
        self.root = root
        self.limits = limits
        faults = nil
    }

    init(
        root: URL,
        limits: ChatRecoveryLimits = .default,
        faults: ChatRecoveryStoreFaults?
    ) {
        self.root = root
        self.limits = limits
        self.faults = faults
    }

    public func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        try prepareRoot()
        let filename = key.filename
        var index = try loadIndex()
        guard let entry = index.records[filename], entry.bindingID == key.credentialBindingID else {
            return nil
        }

        do {
            let data = try Data(contentsOf: recordURL(filename))
            var snapshot = try JSONDecoder().decode(ChatRecoverySnapshot.self, from: data)
            try hydrate(&snapshot, filename: filename)
            try validate(snapshot)
            return snapshot
        } catch is DecodingError {
            try removeRecord(filename, from: &index)
            return nil
        } catch let error as ChatRecoveryError where error == .invalidRecord || error == .quotaExceeded {
            try removeRecord(filename, from: &index)
            return nil
        } catch {
            throw ChatRecoveryError.storageUnavailable
        }
    }

    public func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        try prepareRoot()
        try validate(snapshot)
        var index = try loadIndex()
        let filename = key.filename
        let encodedSnapshot: Data
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            encodedSnapshot = try encoder.encode(snapshot)
        } catch {
            throw ChatRecoveryError.invalidRecord
        }

        let expired = expiredDraftFilenames(in: index, excluding: filename)
        let pendingCount = index.records.reduce(into: 0) { count, element in
            let (existingFilename, entry) = element
            if existingFilename != filename,
               !expired.contains(existingFilename),
               entry.bindingID == key.credentialBindingID,
               recordContainsPendingTurn(existingFilename) {
                count += 1
            }
        } + (snapshot.pendingTurn == nil ? 0 : 1)
        guard pendingCount <= limits.maximumUnresolvedRecords else {
            throw ChatRecoveryError.quotaExceeded
        }

        let attachments = allAttachments(in: snapshot)
        var newlyRequiredAttachmentBytes = 0
        for attachment in attachments {
            let url = attachmentURL(filename, attachmentID: attachment.id)
            if fileManager.fileExists(atPath: url.path) {
                do {
                    guard try Data(contentsOf: url) == attachment.data else {
                        throw ChatRecoveryError.invalidRecord
                    }
                } catch let error as ChatRecoveryError {
                    throw error
                } catch {
                    throw ChatRecoveryError.storageUnavailable
                }
            } else {
                newlyRequiredAttachmentBytes += attachment.data.count
            }
        }

        var candidateIndex = index
        for staleFilename in expired {
            candidateIndex.records.removeValue(forKey: staleFilename)
        }
        candidateIndex.records[filename] = RecoveryIndexEntry(
            bindingID: key.credentialBindingID,
            updatedAt: snapshot.updatedAt,
            hasPendingTurn: snapshot.pendingTurn != nil
        )
        let encodedIndex: Data
        do {
            encodedIndex = try encodeIndex(candidateIndex)
        } catch {
            throw ChatRecoveryError.storageUnavailable
        }

        let existingBytes = try totalRecoveryBytes()
        let replacedRecordBytes = try fileBytes(recordURL(filename))
        // Do not subtract old attachments: pruning only happens after the
        // record and index commit. Expired records are also retained until
        // their index removal commits. Also reserve the temporary-file peaks:
        // record replacement holds its old destination and its new protected
        // temporary file, then index replacement holds the new record plus
        // both old index and new protected temporary index bytes.
        let bytesAfterNewAttachments = existingBytes + newlyRequiredAttachmentBytes
        let recordReplacementPeak = bytesAfterNewAttachments + encodedSnapshot.count
        let indexReplacementPeak = bytesAfterNewAttachments - replacedRecordBytes
            + encodedSnapshot.count + encodedIndex.count
        guard max(recordReplacementPeak, indexReplacementPeak) <= limits.maximumTotalBytes else {
            throw ChatRecoveryError.quotaExceeded
        }

        let hadIndexedRecord = index.records[filename] != nil
        index = candidateIndex
        var newlyWrittenAttachments: [URL] = []
        var recordWasCommitted = false
        do {
            for attachment in attachments {
                let url = attachmentURL(filename, attachmentID: attachment.id)
                if fileManager.fileExists(atPath: url.path) {
                    guard try Data(contentsOf: url) == attachment.data else {
                        throw ChatRecoveryError.invalidRecord
                    }
                } else {
                    try writeProtected(attachment.data, to: url)
                    newlyWrittenAttachments.append(url)
                }
            }
            try writeProtected(encodedSnapshot, to: recordURL(filename))
            recordWasCommitted = true
            try writeIndex(index)
            // Index removal commits before physical expiry cleanup. If the
            // latter cannot run, the unindexed draft stays protected and
            // accounted for, rather than leaving a live index entry that
            // references a missing record.
            for staleFilename in expired {
                try? removeRecordFiles(staleFilename)
            }
            // Cleanup is after both authoritative files are durable. A
            // failure here may retain stale protected bytes, but never makes
            // a complete snapshot unreadable; the reservation above accounts
            // for that conservative case.
            try? removeUnusedAttachments(filename, keeping: Set(attachments.map(\.id)))
        } catch let error as ChatRecoveryError {
            let recordIsCommitted = recordWasCommitted || recordMatches(encodedSnapshot, filename: filename)
            let indexWasCommitted = recordIsCommitted && indexMatches(encodedIndex)
            if !recordIsCommitted || (!hadIndexedRecord && !indexWasCommitted) {
                if recordIsCommitted {
                    try? fileManager.removeItem(at: recordURL(filename))
                }
                for url in newlyWrittenAttachments {
                    try? fileManager.removeItem(at: url)
                }
            }
            throw error
        } catch {
            let recordIsCommitted = recordWasCommitted || recordMatches(encodedSnapshot, filename: filename)
            let indexWasCommitted = recordIsCommitted && indexMatches(encodedIndex)
            if !recordIsCommitted || (!hadIndexedRecord && !indexWasCommitted) {
                if recordIsCommitted {
                    try? fileManager.removeItem(at: recordURL(filename))
                }
                for url in newlyWrittenAttachments {
                    try? fileManager.removeItem(at: url)
                }
            }
            throw ChatRecoveryError.storageUnavailable
        }
    }

    public func remove(for key: ChatRecoveryKey) async throws {
        try prepareRoot()
        var index = try loadIndex()
        let filename = key.filename
        index.records.removeValue(forKey: filename)
        try writeIndex(index)
        try removeRecordFiles(filename)
    }

    public func purge(bindingID: UUID) async throws {
        try prepareRoot()
        var index = try loadIndex()
        let filenames = index.records.compactMap { filename, entry in
            entry.bindingID == bindingID ? filename : nil
        }
        for filename in filenames { index.records.removeValue(forKey: filename) }
        try writeIndex(index)
        try removeUnindexedRecoveryFiles(keeping: Set(index.records.keys))
    }

    public func sweep(keeping bindingIDs: Set<UUID>) async throws {
        try prepareRoot()
        var index = try loadIndex()
        let expired = expiredDraftFilenames(in: index, excluding: nil)
        let staleBindings = index.records.compactMap { filename, entry in
            bindingIDs.contains(entry.bindingID) ? nil : filename
        }
        let filenames = Set(expired).union(staleBindings)
        for filename in filenames { index.records.removeValue(forKey: filename) }
        try writeIndex(index)
        try removeUnindexedRecoveryFiles(keeping: Set(index.records.keys))
    }

    public func eraseAllAfterCredentialDeletion() async throws {
        do {
            if faults?.consumeEraseAllFailure() == true {
                throw ChatRecoveryError.storageUnavailable
            }
            guard fileManager.fileExists(atPath: root.path) else { return }
            try fileManager.removeItem(at: root)
        } catch {
            throw ChatRecoveryError.storageUnavailable
        }
    }

    private func prepareRoot() throws {
        do {
            try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
            try protect(root)
            try removeOrphanedTemporaryFiles()
        } catch {
            throw ChatRecoveryError.storageUnavailable
        }
    }

    private func validate(_ snapshot: ChatRecoverySnapshot) throws {
        let editableIsPresent = !snapshot.draftText.isEmpty || !snapshot.draftAttachments.isEmpty
        guard !(editableIsPresent && snapshot.deferredTurn != nil) else {
            throw ChatRecoveryError.invalidRecord
        }
        try validateComposition(text: snapshot.draftText, attachments: snapshot.draftAttachments)
        if let deferredTurn = snapshot.deferredTurn {
            try validateComposition(text: deferredTurn.text, attachments: deferredTurn.attachments)
        }
        if let pendingTurn = snapshot.pendingTurn {
            try validateComposition(text: pendingTurn.text, attachments: pendingTurn.attachments)
        }
        let ids = allAttachments(in: snapshot).map(\.id)
        guard Set(ids).count == ids.count else {
            throw ChatRecoveryError.invalidRecord
        }
    }

    private func validateComposition(text: String, attachments: [RecoveredTurnAttachment]) throws {
        guard text.lengthOfBytes(using: .utf8) <= limits.maximumDraftBytes,
              attachments.count <= 4,
              attachments.reduce(0, { $0 + $1.data.count }) <= limits.maximumCompositionAttachmentBytes else {
            throw ChatRecoveryError.quotaExceeded
        }
    }

    private func hydrate(_ snapshot: inout ChatRecoverySnapshot, filename: String) throws {
        try hydrate(&snapshot.draftAttachments, filename: filename)
        if snapshot.deferredTurn != nil {
            try hydrate(&snapshot.deferredTurn!.attachments, filename: filename)
        }
        if snapshot.pendingTurn != nil {
            try hydrate(&snapshot.pendingTurn!.attachments, filename: filename)
        }
    }

    private func hydrate(_ attachments: inout [RecoveredTurnAttachment], filename: String) throws {
        for index in attachments.indices {
            let url = attachmentURL(filename, attachmentID: attachments[index].id)
            guard fileManager.fileExists(atPath: url.path) else {
                throw ChatRecoveryError.invalidRecord
            }
            do {
                attachments[index].data = try Data(contentsOf: url)
            } catch {
                throw ChatRecoveryError.storageUnavailable
            }
        }
    }

    private func allAttachments(in snapshot: ChatRecoverySnapshot) -> [RecoveredTurnAttachment] {
        snapshot.draftAttachments
            + (snapshot.deferredTurn?.attachments ?? [])
            + (snapshot.pendingTurn?.attachments ?? [])
    }

    private func expiredDraftFilenames(
        in index: RecoveryIndex,
        excluding excludedFilename: String?
    ) -> Set<String> {
        let cutoff = Date.now.addingTimeInterval(-limits.maximumDraftAge)
        return Set(index.records.compactMap { filename, entry in
            guard filename != excludedFilename,
                  !entry.hasPendingTurn,
                  entry.updatedAt < cutoff,
                  snapshotIsConfirmedDraftOnly(filename) else {
                return nil
            }
            return filename
        })
    }

    private func snapshotIsConfirmedDraftOnly(_ filename: String) -> Bool {
        do {
            let snapshot = try JSONDecoder().decode(
                ChatRecoverySnapshot.self,
                from: Data(contentsOf: recordURL(filename))
            )
            return snapshot.pendingTurn == nil
        } catch {
            // A stale index must not authorize deleting data we cannot decode
            // or read. Preserve it until a scoped recovery path can handle it.
            return false
        }
    }

    private func recordContainsPendingTurn(_ filename: String) -> Bool {
        do {
            let snapshot = try JSONDecoder().decode(
                ChatRecoverySnapshot.self,
                from: Data(contentsOf: recordURL(filename))
            )
            return snapshot.pendingTurn != nil
        } catch {
            // When a record cannot be inspected, retaining the slot is safer
            // than creating a fourth unresolved delivery candidate.
            return true
        }
    }

    private var indexURL: URL {
        root.appendingPathComponent(Self.indexFilename)
    }

    private func loadIndex() throws -> RecoveryIndex {
        guard fileManager.fileExists(atPath: indexURL.path) else {
            return RecoveryIndex(version: Self.indexVersion, records: [:])
        }
        do {
            let index = try JSONDecoder().decode(RecoveryIndex.self, from: Data(contentsOf: indexURL))
            guard index.version == Self.indexVersion else { throw ChatRecoveryError.invalidRecord }
            return index
        } catch {
            // The index is an authorization map. Its corruption or a locked
            // protected-data read must fail closed without erasing records,
            // especially unresolved delivery snapshots.
            throw ChatRecoveryError.storageUnavailable
        }
    }

    private func writeIndex(_ index: RecoveryIndex) throws {
        do {
            if faults?.consumeIndexWriteFailure() == true {
                throw ChatRecoveryError.storageUnavailable
            }
            try writeProtected(encodeIndex(index), to: indexURL)
            if faults?.consumePostIndexWriteFailure() == true {
                throw ChatRecoveryError.storageUnavailable
            }
        } catch let error as ChatRecoveryError {
            throw error
        } catch {
            throw ChatRecoveryError.storageUnavailable
        }
    }

    private func encodeIndex(_ index: RecoveryIndex) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(index)
    }

    private func indexMatches(_ data: Data) -> Bool {
        (try? Data(contentsOf: indexURL)) == data
    }

    private func recordMatches(_ data: Data, filename: String) -> Bool {
        (try? Data(contentsOf: recordURL(filename))) == data
    }

    private func writeProtected(_ data: Data, to url: URL) throws {
        let temporaryURL = root.appendingPathComponent(".\(UUID().uuidString).tmp")
        do {
            var options: Data.WritingOptions = [.withoutOverwriting]
#if os(iOS)
            // This applies NSFileProtectionComplete as the temporary file is
            // created, before the payload bytes are written.
            options.insert(.completeFileProtection)
#endif
            try data.write(to: temporaryURL, options: options)
            if faults?.consumeTemporaryWriteFailure() == true {
                throw ChatRecoveryError.storageUnavailable
            }
            try protect(temporaryURL)
            if fileManager.fileExists(atPath: url.path) {
                _ = try fileManager.replaceItemAt(url, withItemAt: temporaryURL)
            } else {
                try fileManager.moveItem(at: temporaryURL, to: url)
            }
            if url.pathExtension == Self.recordExtension,
               faults?.consumePostRecordCommitFailure() == true {
                throw ChatRecoveryError.storageUnavailable
            }
            try protect(url)
        } catch {
            try? fileManager.removeItem(at: temporaryURL)
            throw ChatRecoveryError.storageUnavailable
        }
    }

    private func protect(_ url: URL) throws {
        var protectedURL = url
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try protectedURL.setResourceValues(values)
#if os(iOS)
        try fileManager.setAttributes(
            [.protectionKey: FileProtectionType.complete],
            ofItemAtPath: url.path
        )
#endif
    }

    private func removeRecord(_ filename: String, from index: inout RecoveryIndex) throws {
        index.records.removeValue(forKey: filename)
        try writeIndex(index)
        try removeRecordFiles(filename)
    }

    private func removeRecordFiles(_ filename: String) throws {
        let record = recordURL(filename)
        if fileManager.fileExists(atPath: record.path) {
            try removeRecoveryFile(record)
        }
        let attachments = try fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix("\(filename)-") && $0.pathExtension == Self.attachmentExtension }
        for attachment in attachments {
            try removeRecoveryFile(attachment)
        }
    }

    private func removeUnusedAttachments(_ filename: String, keeping attachmentIDs: Set<UUID>) throws {
        let attachments = try fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix("\(filename)-") && $0.pathExtension == Self.attachmentExtension }
        for attachment in attachments {
            let idString = attachment.deletingPathExtension().lastPathComponent
                .replacingOccurrences(of: "\(filename)-", with: "")
            if UUID(uuidString: idString).map({ attachmentIDs.contains($0) }) != true {
                try fileManager.removeItem(at: attachment)
            }
        }
    }

    private func removeUnindexedRecoveryFiles(keeping filenames: Set<String>) throws {
        let files = try fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        for file in files where file.pathExtension == Self.recordExtension || file.pathExtension == Self.attachmentExtension {
            let candidate = file.pathExtension == Self.recordExtension
                ? file.deletingPathExtension().lastPathComponent
                : String(file.lastPathComponent.prefix(64))
            if !filenames.contains(candidate) {
                try removeRecoveryFile(file)
            }
        }
    }

    private func removeOrphanedTemporaryFiles() throws {
        let files = try fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        for file in files where file.lastPathComponent.hasPrefix(".") && file.pathExtension == "tmp" {
            try fileManager.removeItem(at: file)
        }
    }

    private func removeRecoveryFile(_ url: URL) throws {
        if faults?.consumeRecoveryFileRemovalFailure() == true {
            throw ChatRecoveryError.storageUnavailable
        }
        try fileManager.removeItem(at: url)
    }

    private func totalRecoveryBytes() throws -> Int {
        try fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: [.fileSizeKey])
            .reduce(0) { partial, url in
                partial + (try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0)
            }
    }

    func onDiskByteCount() throws -> Int {
        try totalRecoveryBytes()
    }

    private func fileBytes(_ url: URL) throws -> Int {
        guard fileManager.fileExists(atPath: url.path) else { return 0 }
        return try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
    }

    private func recordURL(_ filename: String) -> URL {
        root.appendingPathComponent(filename).appendingPathExtension(Self.recordExtension)
    }

    private func attachmentURL(_ filename: String, attachmentID: UUID) -> URL {
        root.appendingPathComponent("\(filename)-\(attachmentID.uuidString)")
            .appendingPathExtension(Self.attachmentExtension)
    }
}

private struct RecoveryIndex: Codable, Sendable {
    let version: Int
    var records: [String: RecoveryIndexEntry]
}

private struct RecoveryIndexEntry: Codable, Sendable {
    let bindingID: UUID
    let updatedAt: Date
    let hasPendingTurn: Bool
}
