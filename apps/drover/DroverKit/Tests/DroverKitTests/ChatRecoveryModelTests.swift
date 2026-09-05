import Foundation
import Testing
@testable import DroverKit

extension MockNetworkTests {
@Suite(.serialized)
struct ChatRecoveryModelTests {
    @Test @MainActor func sendingCheckpointsOriginalIDBeforeThePost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        MockURLProtocol.handler = { _ in (202, Data(#"{"turn_id": "accepted"}"#.utf8)) }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "preserve me"

        await model.sendTurn()

        let snapshot = try #require(await recovery.lastSavedSnapshot)
        let savedID = try #require(snapshot.pendingTurn?.clientTurnID.uuidString)
        #expect(MockURLProtocol.sentClientTurnIDs == [savedID])
    }

    @Test @MainActor func storageFailureLeavesTheComposerUntouchedAndPreventsThePost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        await recovery.failNextSave()
        MockURLProtocol.handler = { _ in (202, Data(#"{"turn_id": "accepted"}"#.utf8)) }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "do not lose this"

        await model.sendTurn()

        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        #expect(model.composerText == "do not lose this")
        #expect(model.pendingTurn == nil)
        #expect(model.canSendTurn == false)
    }

    @Test @MainActor func inFlightSaveFailureParksAnOlderTurnAndRetriesTheCurrentSnapshotWithoutAPost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore()
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0x91, 0x92]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0x81, 0x82]))
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older A"
        model.pendingAttachments = [olderImage]

        let sending = Task { @MainActor in
            await model.sendTurn()
        }
        await recovery.waitUntilSaveStarted()
        let originalID = try #require(model.pendingTurn?.clientTurnID)
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        await recovery.failSave()
        await sending.value

        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        #expect(model.composerText == "newer B")
        #expect(model.pendingAttachments == [newerImage])
        #expect(model.pendingTurn?.clientTurnID == originalID)
        #expect(model.pendingTurn?.text == "older A")
        #expect(model.pendingTurn?.attachments == [olderImage])
        #expect(model.pendingTurn?.deliveryState == .needsManualReview)
        #expect(model.recoveryStatusMessage == "Chat recovery could not protect this draft. Check local storage in Settings.")
        #expect(model.canRetryRecoverySave)

        await model.retryRecoverySave()

        let saved = try #require(await recovery.lastSavedSnapshot)
        #expect(saved.draftText == "newer B")
        #expect(saved.draftAttachments.map(\.data) == [newerImage.data])
        #expect(saved.pendingTurn?.clientTurnID.uuidString == originalID)
        #expect(saved.pendingTurn?.text == "older A")
        #expect(saved.pendingTurn?.attachments.map(\.data) == [olderImage.data])
        #expect(model.recoveryStatusMessage == nil)
        #expect(model.canRetryRecoverySave == false)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func failedSaveCanDurablyCommitAnIntentionalClearWithoutPosting() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "clear this draft"

        let saving = Task { @MainActor in
            await model.flushRecoveryCheckpoint()
        }
        await recovery.waitUntilSaveStarted()
        await recovery.failSave()
        await saving.value
        model.composerText = ""

        #expect(model.canRetryRecoverySave)
        await model.retryRecoverySave()

        #expect(await recovery.removeCount == 1)
        #expect(model.recoveryStatusMessage == nil)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func retrySavingSchedulesAnEditMadeWhileTheRetryIsInFlight() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailThenBlockRetryRecoveryStore()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older draft"

        await model.flushRecoveryCheckpoint()
        #expect(model.canRetryRecoverySave)

        let retry = Task { @MainActor in
            await model.retryRecoverySave()
        }
        await recovery.waitUntilRetrySaveStarted()
        model.composerText = "newer draft"
        await recovery.releaseRetrySave()
        await retry.value

        try await waitForRecoveryRecord {
            try await recovery.load(for: recoveryKey(binding: binding))?.draftText == "newer draft"
        }
        #expect(model.recoveryStatusMessage == nil)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func retrySavingPreservesAnEditMadeWhileTheRetryIsInFlightAtImmediateDeparture() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailThenBlockRetryRecoveryStore()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older draft"

        await model.flushRecoveryCheckpoint()
        let retry = Task { @MainActor in
            await model.retryRecoverySave()
        }
        await recovery.waitUntilRetrySaveStarted()
        model.composerText = "newer draft"
        await recovery.releaseRetrySave()
        await retry.value
        await model.prepareForDeparture()

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "newer draft")
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func departingBeforeASuspendedRetryReturnsStillRecoversTheNewestEdit() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = SavedThenFailThenBlockRetryRecoveryStore()
        var model: ChatModel? = recoveryModel(binding: binding, recoveryStore: recovery)
        model?.composerText = "older A"
        await model?.flushRecoveryCheckpoint()
        await model?.flushRecoveryCheckpoint()
        #expect(model?.canRetryRecoverySave == true)

        let retry = Task { @MainActor [model = try #require(model)] in
            await model.retryRecoverySave()
        }
        await recovery.waitUntilRetrySaveStarted()
        model?.composerText = "newer B"
        let departure = Task { @MainActor [model = try #require(model)] in
            await model.prepareForDeparture()
        }
        await departure.value
        model = nil
        await recovery.releaseRetrySave()
        await retry.value

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "newer B")
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func departingBeforeASuspendedRetryReturnsDurablyCommitsAnIntentionalClear() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = SavedThenFailThenBlockRetryRecoveryStore()
        var model: ChatModel? = recoveryModel(binding: binding, recoveryStore: recovery)
        model?.composerText = "older A"
        await model?.flushRecoveryCheckpoint()
        await model?.flushRecoveryCheckpoint()
        #expect(model?.canRetryRecoverySave == true)

        let retry = Task { @MainActor [model = try #require(model)] in
            await model.retryRecoverySave()
        }
        await recovery.waitUntilRetrySaveStarted()
        model?.composerText = ""
        let departure = Task { @MainActor [model = try #require(model)] in
            await model.prepareForDeparture()
        }
        await departure.value
        model = nil
        await recovery.releaseRetrySave()
        await retry.value

        #expect(try await recovery.load(for: recoveryKey(binding: binding)) == nil)
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()
        #expect(recreated.composerText.isEmpty)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func failedCopyPreservesANewerDraftAndBlocksSendUntilRetryMakesBothDurable() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore(failingSaveNumber: 2)
        let originalID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xA1, 0xA2]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xB1, 0xB2]))
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(
                    clientTurnID: originalID,
                    text: "older A",
                    attachments: [RecoveredTurnAttachment(mediaType: "image/jpeg", data: olderImage.data)]
                )
            ),
            for: recoveryKey(binding: binding)
        )
        MockURLProtocol.handler = { _ in (202, Data(#"{"turn_id": "accepted"}"#.utf8)) }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        let copying = Task { @MainActor in
            await model.copyPendingTurnToDraft()
        }
        await recovery.waitUntilSaveStarted()
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        #expect(model.canSendTurn == false)
        let attemptedSend = Task { @MainActor in
            await model.sendTurn()
        }
        await attemptedSend.value
        await recovery.failSave()
        await copying.value

        #expect(model.pendingTurn?.clientTurnID == originalID.uuidString)
        #expect(model.pendingTurn?.attachments == [olderImage])
        #expect(model.composerText == "newer B")
        #expect(model.pendingAttachments == [newerImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)

        await model.retryRecoverySave()
        await model.prepareForDeparture()
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.pendingTurn?.clientTurnID == originalID.uuidString)
        #expect(recreated.pendingTurn?.attachments == [olderImage])
        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func failedDiscardPreservesANewerDraftAndBlocksSendUntilRetryMakesBothDurable() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore(failingSaveNumber: 2)
        let originalID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xC1, 0xC2]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xD1, 0xD2]))
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(
                    clientTurnID: originalID,
                    text: "older A",
                    attachments: [RecoveredTurnAttachment(mediaType: "image/jpeg", data: olderImage.data)]
                )
            ),
            for: recoveryKey(binding: binding)
        )
        MockURLProtocol.handler = { _ in (202, Data(#"{"turn_id": "accepted"}"#.utf8)) }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        let discarding = Task { @MainActor in
            await model.discardPendingTurn()
        }
        await recovery.waitUntilSaveStarted()
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        #expect(model.canSendTurn == false)
        await recovery.failSave()
        await discarding.value

        #expect(model.pendingTurn?.clientTurnID == originalID.uuidString)
        #expect(model.pendingTurn?.attachments == [olderImage])
        #expect(model.composerText == "newer B")
        #expect(model.pendingAttachments == [newerImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)

        await model.retryRecoverySave()
        await model.prepareForDeparture()
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.pendingTurn?.clientTurnID == originalID.uuidString)
        #expect(recreated.pendingTurn?.attachments == [olderImage])
        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func copyRefusesNewAttachmentAdmissionWhileItsFirstSaveIsBlocked() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore(failingSaveNumber: 2)
        let originalID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xE1, 0xE2]))
        let candidateImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xF1, 0xF2]))
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(
                    clientTurnID: originalID,
                    text: "older A",
                    attachments: [RecoveredTurnAttachment(mediaType: "image/jpeg", data: olderImage.data)]
                )
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        let copying = Task { @MainActor in
            await model.copyPendingTurnToDraft()
        }
        await recovery.waitUntilSaveStarted()
        let admitted = await model.addAttachmentIfRecoverable(candidateImage)

        #expect(admitted == false)
        #expect(model.pendingAttachments == [olderImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        await recovery.failSave()
        await copying.value
    }

    @Test @MainActor func discardRefusesNewAttachmentAdmissionWhileItsFirstSaveIsBlocked() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore(failingSaveNumber: 2)
        let originalID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let candidateImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xF3, 0xF4]))
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: originalID, text: "older A")
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        let discarding = Task { @MainActor in
            await model.discardPendingTurn()
        }
        await recovery.waitUntilSaveStarted()
        let admitted = await model.addAttachmentIfRecoverable(candidateImage)

        #expect(admitted == false)
        #expect(model.pendingAttachments.isEmpty)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        await recovery.failSave()
        await discarding.value
    }

    @Test @MainActor func exactAcknowledgmentDuringCopyNeverResurrectsItsManualDeliveryAfterPhaseTwoFailure() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = PhaseOneThenFailPhaseTwoRecoveryStore()
        let originalID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xA3, 0xA4]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xB3, 0xB4]))
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(
                    clientTurnID: originalID,
                    text: "older A",
                    attachments: [RecoveredTurnAttachment(mediaType: "image/jpeg", data: olderImage.data)]
                )
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        let copying = Task { @MainActor in
            await model.copyPendingTurnToDraft()
        }
        await recovery.waitUntilPhaseOneStarted()
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        model.ingest(.message(.fixture(seq: 9, type: .userInput, text: "older A", turnID: originalID.uuidString)))
        #expect(model.pendingTurn == nil)
        await recovery.releasePhaseOne()
        await copying.value

        #expect(model.pendingTurn == nil)
        #expect(model.composerText == "newer B")
        #expect(model.pendingAttachments == [newerImage])
        await model.retryRecoverySave()
        await model.prepareForDeparture()
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.pendingTurn == nil)
        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func exactAcknowledgmentDuringDiscardNeverResurrectsItsManualDeliveryAfterPhaseTwoFailure() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = PhaseOneThenFailPhaseTwoRecoveryStore()
        let originalID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xC3, 0xC4]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xD3, 0xD4]))
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(
                    clientTurnID: originalID,
                    text: "older A",
                    attachments: [RecoveredTurnAttachment(mediaType: "image/jpeg", data: olderImage.data)]
                )
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        let discarding = Task { @MainActor in
            await model.discardPendingTurn()
        }
        await recovery.waitUntilPhaseOneStarted()
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        model.ingest(.message(.fixture(seq: 9, type: .userInput, text: "older A", turnID: originalID.uuidString)))
        #expect(model.pendingTurn == nil)
        await recovery.releasePhaseOne()
        await discarding.value

        #expect(model.pendingTurn == nil)
        #expect(model.composerText == "newer B")
        #expect(model.pendingAttachments == [newerImage])
        await model.retryRecoverySave()
        await model.prepareForDeparture()
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.pendingTurn == nil)
        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func failedRecoveryReadCannotRetrySavingOrReplaceTheUnreadRecord() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let original = ChatRecoverySnapshot(draftText: "unread saved draft")
        let recovery = UnreadableRecoveryStore(snapshot: original)
        let model = recoveryModel(binding: binding, recoveryStore: recovery)

        await model.restoreRecovery()
        await model.retryRecoverySave()

        #expect(model.recoveryStatusMessage == "Chat recovery could not be read. Existing saved drafts were left unchanged.")
        #expect(model.canRetryRecoverySave == false)
        #expect(await recovery.writeCount == 0)
        #expect(await recovery.unreadSnapshot == original)
    }

    @Test @MainActor func attachmentRejectedByRecoveryLeavesTheComposerUnchanged() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        await recovery.failNextSave()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        let attachment = TurnAttachment(mediaType: "image/jpeg", data: Data([0xA1]))

        let admitted = await model.addAttachmentIfRecoverable(attachment)

        #expect(admitted == false)
        #expect(model.pendingAttachments.isEmpty)
        #expect(model.canSendTurn == false)
    }

    @Test @MainActor func editedDraftAndImageSurviveModelRecreation() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        let image = TurnAttachment(mediaType: "image/jpeg", data: Data([0x01, 0x02]))
        model.composerText = "draft text"
        model.pendingAttachments = [image]

        try await waitForRecoveryRecord {
            let snapshot = try await recovery.load(for: recoveryKey(binding: binding))
            return snapshot?.draftText == "draft text"
                && snapshot?.draftAttachments.count == 1
                && snapshot?.draftAttachments.first?.mediaType == "image/jpeg"
                && snapshot?.draftAttachments.first?.data == Data([0x01, 0x02])
        }

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "draft text")
        #expect(recreated.pendingAttachments == [image])
    }

    @Test @MainActor func immediateEditThenDepartureFlushesBeforeModelRecreation() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        var model: ChatModel? = recoveryModel(binding: binding, recoveryStore: recovery)
        model?.composerText = "do not lose the last edit"

        await model?.prepareForDeparture()
        model = nil

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "do not lose the last edit")
    }

    @Test @MainActor func deferredConflictRestoresAsDraftWithoutPosting() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                deferredTurn: RecoveredDeferredTurn(text: "review this first")
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)

        await model.restoreRecovery()

        #expect(model.composerText == "review this first")
        #expect(model.pendingTurn == nil)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func liveConflictIsDurableThenRestoresAsAReviewableDraft() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "queue this safely"

        await model.sendTurn()

        try await waitForRecoveryRecord {
            try await recovery.load(for: recoveryKey(binding: binding))?.deferredTurn?.text
                == "queue this safely"
        }
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "queue this safely")
        #expect(recreated.pendingTurn == nil)
        #expect(MockURLProtocol.sentClientTurnIDs.count == 1)
    }

    @Test @MainActor func delayedConflictKeepsTheNewerEditableCompositionForManualReview() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xA1, 0xA2]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xB1, 0xB2]))
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        MockURLProtocol.responseDelay = { _ in 0.1 }
        defer {
            MockURLProtocol.handler = nil
            MockURLProtocol.responseDelay = nil
        }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older A"
        model.pendingAttachments = [olderImage]

        let sending = Task { @MainActor in
            await model.sendTurn()
        }
        try await waitForRecoveryRecord { MockURLProtocol.sentClientTurnIDs.count == 1 }
        let originalID = try #require(model.pendingTurn?.clientTurnID)
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        await sending.value
        await model.prepareForDeparture()

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(recreated.pendingTurn?.clientTurnID == originalID)
        #expect(recreated.pendingTurn?.text == "older A")
        #expect(recreated.pendingTurn?.attachments == [olderImage])
        #expect(recreated.pendingTurn?.deliveryState == .needsManualReview)
        #expect(MockURLProtocol.sentClientTurnIDs == [originalID])
    }

    @Test @MainActor func deferredConflictParksItsOriginalIDWhenANewerDraftAppears() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xC1, 0xC2]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xD1, 0xD2]))
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older A"
        model.pendingAttachments = [olderImage]

        await model.sendTurn()

        let originalID = try #require(MockURLProtocol.sentClientTurnIDs.first)
        #expect(model.queuedTurn == "older A")
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        await model.prepareForDeparture()

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(recreated.pendingTurn?.clientTurnID == originalID)
        #expect(recreated.pendingTurn?.text == "older A")
        #expect(recreated.pendingTurn?.attachments == [olderImage])
        #expect(recreated.pendingTurn?.deliveryState == .needsManualReview)
        #expect(MockURLProtocol.sentClientTurnIDs == [originalID])
    }

    @Test @MainActor func deferredConflictDurablyAdmitsAnAttachmentOnlyNewerDraft() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xC3, 0xC4]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xD3, 0xD4]))
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older A"
        model.pendingAttachments = [olderImage]

        await model.sendTurn()

        let originalID = try #require(MockURLProtocol.sentClientTurnIDs.first)
        let admitted = await model.addAttachmentIfRecoverable(newerImage)
        let durable = try #require(await recovery.load(for: recoveryKey(binding: binding)))

        #expect(admitted)
        #expect(durable.draftText.isEmpty)
        #expect(durable.draftAttachments.map(\.data) == [newerImage.data])
        #expect(durable.deferredTurn == nil)
        #expect(durable.pendingTurn?.clientTurnID.uuidString == originalID)
        #expect(durable.pendingTurn?.text == "older A")
        #expect(durable.pendingTurn?.attachments.map(\.data) == [olderImage.data])

        await model.prepareForDeparture()
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText.isEmpty)
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(recreated.pendingTurn?.clientTurnID == originalID)
        #expect(recreated.pendingTurn?.attachments == [olderImage])
        #expect(MockURLProtocol.sentClientTurnIDs == [originalID])
    }

    @Test @MainActor func queuedSendSaveFailureParksTheQueuedIDAndPreservesTheNewerDraft() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore(failingSaveNumber: 3)
        let olderImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xE3, 0xE4]))
        let newerImage = TurnAttachment(mediaType: "image/jpeg", data: Data([0xF3, 0xF4]))
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older A"
        model.pendingAttachments = [olderImage]

        await model.sendTurn()
        model.ingest(.message(.fixture(
            seq: 9,
            type: .status,
            payload: ["turn_complete": .bool(true)]
        )))
        await recovery.waitUntilSaveStarted()
        let queuedID = try #require(model.pendingTurn?.clientTurnID)
        model.composerText = "newer B"
        model.pendingAttachments = [newerImage]
        await recovery.failSave()
        try await waitForChatState {
            model.pendingTurn?.deliveryState == .needsManualReview
        }

        #expect(model.composerText == "newer B")
        #expect(model.pendingAttachments == [newerImage])
        #expect(model.pendingTurn?.clientTurnID == queuedID)
        #expect(model.pendingTurn?.text == "older A")
        #expect(model.pendingTurn?.attachments == [olderImage])
        #expect(model.canRetryRecoverySave)

        await model.retryRecoverySave()
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText == "newer B")
        #expect(recreated.pendingAttachments == [newerImage])
        #expect(recreated.pendingTurn?.clientTurnID == queuedID)
        #expect(recreated.pendingTurn?.attachments == [olderImage])
        #expect(MockURLProtocol.sentClientTurnIDs.count == 1)
    }

    @Test @MainActor func deferredOnlyQueuedSaveFailureRetainsItsIDUntilANewerDraftAppears() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = FailingBlockingRecoveryStore(failingSaveNumber: 3)
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "older A"

        await model.sendTurn()
        model.ingest(.message(.fixture(
            seq: 9,
            type: .status,
            payload: ["turn_complete": .bool(true)]
        )))
        await recovery.waitUntilSaveStarted()
        let queuedID = try #require(model.pendingTurn?.clientTurnID)
        await recovery.failSave()
        try await waitForChatState { model.queuedTurn == "older A" }

        model.composerText = "newer B"

        #expect(model.pendingTurn?.clientTurnID == queuedID)
        #expect(model.pendingTurn?.deliveryState == .needsManualReview)
        #expect(model.composerText == "newer B")
        #expect(MockURLProtocol.sentClientTurnIDs.count == 1)
    }

    @Test @MainActor func imageOnlyDeferredConflictSurvivesRecreationWithoutAnotherPost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        let image = TurnAttachment(mediaType: "image/jpeg", data: Data([0xE1, 0xE2]))
        MockURLProtocol.handler = { _ in
            (409, Data(#"{"error": "turn already in flight"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.pendingAttachments = [image]

        await model.sendTurn()
        await model.prepareForDeparture()

        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.composerText.isEmpty)
        #expect(recreated.pendingAttachments == [image])
        #expect(recreated.pendingTurn == nil)
        #expect(MockURLProtocol.sentClientTurnIDs.count == 1)
    }

    @Test @MainActor func ambiguousFailureRestoresTheOriginalIDForManualReviewWithoutAPost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        MockURLProtocol.handler = { _ in
            (503, Data(#"{"error": "temporary failure"}"#.utf8))
        }
        defer { MockURLProtocol.handler = nil }
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        model.composerText = "preserve this UUID"

        await model.sendTurn()

        let originalID = try #require(model.pendingTurn?.clientTurnID)
        try await waitForRecoveryRecord {
            try await recovery.load(for: recoveryKey(binding: binding))?.pendingTurn?.clientTurnID.uuidString
                == originalID
        }
        let recreated = recoveryModel(binding: binding, recoveryStore: recovery)
        await recreated.restoreRecovery()

        #expect(recreated.pendingTurn?.clientTurnID == originalID)
        #expect(recreated.pendingTurn?.deliveryState == .needsManualReview)
        #expect(MockURLProtocol.sentClientTurnIDs == [originalID])
    }

    @Test @MainActor func restoredPendingCopyPreservesAnExistingEditableDraft() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let turnID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let recovery = InMemoryChatRecoveryStore()
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: turnID, text: "saved delivery")
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()
        model.composerText = "keep this draft"

        await model.copyPendingTurnToDraft()

        #expect(model.composerText == "keep this draft")
        #expect(model.pendingTurn?.clientTurnID == turnID.uuidString)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func restoredPendingCopiesToAnEmptyDraftWithoutPosting() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let turnID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let recovery = InMemoryChatRecoveryStore()
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: turnID, text: "saved delivery")
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        await model.copyPendingTurnToDraft()

        #expect(model.composerText == "saved delivery")
        #expect(model.pendingTurn == nil)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        try await waitForRecoveryRecord {
            let snapshot = try await recovery.load(for: recoveryKey(binding: binding))
            return snapshot?.draftText == "saved delivery" && snapshot?.pendingTurn == nil
        }
    }

    @Test @MainActor func restoredPendingDiscardRemovesOnlyTheLocalRecord() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let turnID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let recovery = InMemoryChatRecoveryStore()
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: turnID, text: "discard me")
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()

        await model.discardPendingTurn()

        #expect(model.pendingTurn == nil)
        #expect(try await recovery.load(for: recoveryKey(binding: binding)) == nil)
    }

    @Test @MainActor func missingRecoveryDependencyPreventsAnUnprotectedPost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let client = DroverClient(
            config: ServerConfig(urlString: "http://recovery.test:7080")!,
            token: "synthetic-token",
            credentialBindingID: binding,
            session: MockURLProtocol.session()
        )
        let model = ChatModel(client: client, sessionID: "recovery-session")
        model.composerText = "cannot send unprotected"

        await model.sendTurn()

        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        #expect(model.composerText == "cannot send unprotected")
        #expect(model.canSendTurn == false)
    }

    @Test @MainActor func invalidatedRecoveryGenerationCannotWriteAnOldNamespace() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = InMemoryChatRecoveryStore()
        let gate = ChatRecoveryWriteGate()
        let model = lifecycleRecoveryModel(
            binding: binding,
            recoveryStore: recovery,
            recoveryWriteGate: gate
        )
        model.composerText = "do not recreate after sign out"
        gate.invalidate()

        try await Task.sleep(for: .milliseconds(300))

        #expect(try await recovery.load(for: recoveryKey(binding: binding)) == nil)
    }

    @Test @MainActor func inFlightOldGenerationWriteDrainsBeforeCleanupErasesIt() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = BlockingRecoveryStore()
        let gate = ChatRecoveryWriteGate()
        let model = lifecycleRecoveryModel(
            binding: binding,
            recoveryStore: recovery,
            recoveryWriteGate: gate
        )
        model.composerText = "cannot recreate after cleanup"
        await recovery.waitUntilSaveStarted()

        let retiredGeneration = gate.invalidate()
        let cleanup = Task { @MainActor in
            await gate.drain(retiredGeneration)
            try? await recovery.eraseAllAfterCredentialDeletion()
        }
        await Task.yield()
        #expect(await recovery.wasErased == false)

        await recovery.releaseSave()
        await cleanup.value

        #expect(try await recovery.load(for: recoveryKey(binding: binding)) == nil)
    }

    @Test @MainActor func invalidatedGenerationAfterPendingSavePreventsThePost() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let recovery = BlockingRecoveryStore()
        let gate = ChatRecoveryWriteGate()
        let model = lifecycleRecoveryModel(
            binding: binding,
            recoveryStore: recovery,
            recoveryWriteGate: gate
        )
        model.composerText = "do not send after sign out"

        let sending = Task { @MainActor in
            await model.sendTurn()
        }
        await recovery.waitUntilSaveStarted()
        _ = gate.invalidate()
        await recovery.releaseSave()
        await sending.value

        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
    }

    @Test @MainActor func restoredUnknownTurnNeverPostsAndRequiresReview() async throws {
        MockURLProtocol.resetRecordedRequests()
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let turnID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let recovery = InMemoryChatRecoveryStore()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: turnID, text: "ship it")
            ),
            for: recoveryKey(binding: binding)
        )

        await model.restoreRecovery()

        #expect(model.pendingTurn?.deliveryState == .needsManualReview)
        model.checkPendingDelivery()
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        model.stop()
    }

    @Test @MainActor func checkDeliveryRestartsAnActiveCatchUpWithoutPosting() async throws {
        MockURLProtocol.resetRecordedRequests()
        nonisolated(unsafe) var historyRequests = 0
        MockURLProtocol.handler = { request in
            if request.url?.path == "/harness/sessions/recovery-session/messages" {
                historyRequests += 1
                return (200, Data(#"{"messages": [], "max_seq": 0, "has_older": false, "has_newer": false}"#.utf8))
            }
            return (404, Data())
        }
        defer { MockURLProtocol.handler = nil }
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let turnID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let recovery = InMemoryChatRecoveryStore()
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: turnID, text: "check me")
            ),
            for: recoveryKey(binding: binding)
        )
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        await model.restoreRecovery()
        model.start()
        try await waitForRecoveryRecord { historyRequests >= 1 }

        model.checkPendingDelivery()

        try await waitForRecoveryRecord { historyRequests >= 2 }
        #expect(model.pendingTurn?.deliveryState == .needsManualReview)
        #expect(MockURLProtocol.sentClientTurnIDs.isEmpty)
        model.stop()
    }

    @Test @MainActor func exactHistoryTurnIDClearsOnlyThatPendingRecord() async throws {
        let binding = UUID(uuidString: "00000000-0000-4000-8000-000000000041")!
        let turnID = UUID(uuidString: "00000000-0000-4000-8000-000000000042")!
        let recovery = InMemoryChatRecoveryStore()
        let model = recoveryModel(binding: binding, recoveryStore: recovery)
        try await recovery.save(
            ChatRecoverySnapshot(
                draftText: "",
                pendingTurn: RecoveredPendingTurn(clientTurnID: turnID, text: "same words")
            ),
            for: recoveryKey(binding: binding)
        )

        await model.restoreRecovery()
        model.ingest(.history([
            .fixture(seq: 9, type: .userInput, text: "same words", turnID: turnID.uuidString),
        ], decodeIssues: []))

        #expect(model.pendingTurn == nil)
        try await waitForRecoveryRecord {
            try await recovery.load(for: recoveryKey(binding: binding))?.pendingTurn == nil
        }
    }
}
}

private actor InMemoryChatRecoveryStore: ChatRecoveryPersisting {
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]
    private var saves: [ChatRecoverySnapshot] = []
    private var saveFailures = 0

    var lastSavedSnapshot: ChatRecoverySnapshot? { saves.last }

    func failNextSave() {
        saveFailures += 1
    }

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        guard saveFailures == 0 else {
            saveFailures -= 1
            throw ChatRecoveryError.storageUnavailable
        }
        snapshots[key] = snapshot
        saves.append(snapshot)
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }

    func purge(bindingID: UUID) async throws {
        snapshots = snapshots.filter { $0.key.credentialBindingID != bindingID }
    }

    func sweep(keeping bindingIDs: Set<UUID>) async throws {
        snapshots = snapshots.filter { bindingIDs.contains($0.key.credentialBindingID) }
    }

    func eraseAllAfterCredentialDeletion() async throws {
        snapshots.removeAll()
    }
}

private actor BlockingRecoveryStore: ChatRecoveryPersisting {
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]
    private var saveStarted = false
    private var saveStartWaiters: [CheckedContinuation<Void, Never>] = []
    private var saveRelease: CheckedContinuation<Void, Never>?
    private(set) var wasErased = false

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        saveStarted = true
        let waiters = saveStartWaiters
        saveStartWaiters.removeAll()
        waiters.forEach { $0.resume() }
        await withCheckedContinuation { continuation in
            saveRelease = continuation
        }
        snapshots[key] = snapshot
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }

    func purge(bindingID: UUID) async throws {
        snapshots = snapshots.filter { $0.key.credentialBindingID != bindingID }
    }

    func sweep(keeping bindingIDs: Set<UUID>) async throws {
        snapshots = snapshots.filter { bindingIDs.contains($0.key.credentialBindingID) }
    }

    func eraseAllAfterCredentialDeletion() async throws {
        wasErased = true
        snapshots.removeAll()
    }

    func waitUntilSaveStarted() async {
        guard !saveStarted else { return }
        await withCheckedContinuation { continuation in
            saveStartWaiters.append(continuation)
        }
    }

    func releaseSave() {
        let continuation = saveRelease
        saveRelease = nil
        continuation?.resume()
    }
}

private actor FailingBlockingRecoveryStore: ChatRecoveryPersisting {
    private var saveStarted = false
    private var saveStartWaiters: [CheckedContinuation<Void, Never>] = []
    private var saveFailure: CheckedContinuation<Void, Never>?
    private var failureRequested = false
    private let failingSaveNumber: Int
    private var saveCount = 0
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]
    private(set) var removeCount = 0

    init(failingSaveNumber: Int = 1) {
        self.failingSaveNumber = failingSaveNumber
    }

    var lastSavedSnapshot: ChatRecoverySnapshot? { snapshots.values.first }

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        saveCount += 1
        guard saveCount == failingSaveNumber else {
            snapshots[key] = snapshot
            return
        }
        saveStarted = true
        let waiters = saveStartWaiters
        saveStartWaiters.removeAll()
        waiters.forEach { $0.resume() }
        await withCheckedContinuation { continuation in
            if failureRequested {
                failureRequested = false
                continuation.resume()
            } else {
                saveFailure = continuation
            }
        }
        throw ChatRecoveryError.storageUnavailable
    }

    func remove(for key: ChatRecoveryKey) async throws {
        removeCount += 1
        snapshots.removeValue(forKey: key)
    }
    func purge(bindingID: UUID) async throws {}
    func sweep(keeping bindingIDs: Set<UUID>) async throws {}
    func eraseAllAfterCredentialDeletion() async throws {}

    func waitUntilSaveStarted() async {
        guard !saveStarted else { return }
        await withCheckedContinuation { continuation in
            saveStartWaiters.append(continuation)
        }
    }

    func failSave() {
        if let saveFailure {
            self.saveFailure = nil
            saveFailure.resume()
        } else {
            failureRequested = true
        }
    }
}

private actor FailThenBlockRetryRecoveryStore: ChatRecoveryPersisting {
    private var saveCount = 0
    private var retrySaveStarted = false
    private var retrySaveWaiters: [CheckedContinuation<Void, Never>] = []
    private var retrySaveRelease: CheckedContinuation<Void, Never>?
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        saveCount += 1
        switch saveCount {
        case 1:
            throw ChatRecoveryError.storageUnavailable
        case 2:
            retrySaveStarted = true
            let waiters = retrySaveWaiters
            retrySaveWaiters.removeAll()
            waiters.forEach { $0.resume() }
            await withCheckedContinuation { continuation in
                retrySaveRelease = continuation
            }
            snapshots[key] = snapshot
        default:
            snapshots[key] = snapshot
        }
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }
    func purge(bindingID: UUID) async throws {}
    func sweep(keeping bindingIDs: Set<UUID>) async throws {}
    func eraseAllAfterCredentialDeletion() async throws {}

    func waitUntilRetrySaveStarted() async {
        guard !retrySaveStarted else { return }
        await withCheckedContinuation { continuation in
            retrySaveWaiters.append(continuation)
        }
    }

    func releaseRetrySave() {
        let continuation = retrySaveRelease
        retrySaveRelease = nil
        continuation?.resume()
    }
}

private actor SavedThenFailThenBlockRetryRecoveryStore: ChatRecoveryPersisting {
    private var saveCount = 0
    private var retrySaveStarted = false
    private var retrySaveWaiters: [CheckedContinuation<Void, Never>] = []
    private var retrySaveRelease: CheckedContinuation<Void, Never>?
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        saveCount += 1
        switch saveCount {
        case 1:
            snapshots[key] = snapshot
        case 2:
            throw ChatRecoveryError.storageUnavailable
        case 3:
            retrySaveStarted = true
            let waiters = retrySaveWaiters
            retrySaveWaiters.removeAll()
            waiters.forEach { $0.resume() }
            await withCheckedContinuation { continuation in
                retrySaveRelease = continuation
            }
            snapshots[key] = snapshot
        default:
            snapshots[key] = snapshot
        }
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }
    func purge(bindingID: UUID) async throws {}
    func sweep(keeping bindingIDs: Set<UUID>) async throws {}
    func eraseAllAfterCredentialDeletion() async throws {}

    func waitUntilRetrySaveStarted() async {
        guard !retrySaveStarted else { return }
        await withCheckedContinuation { continuation in
            retrySaveWaiters.append(continuation)
        }
    }

    func releaseRetrySave() {
        let continuation = retrySaveRelease
        retrySaveRelease = nil
        continuation?.resume()
    }
}

private actor PhaseOneThenFailPhaseTwoRecoveryStore: ChatRecoveryPersisting {
    private var saveCount = 0
    private var phaseOneStarted = false
    private var phaseOneWaiters: [CheckedContinuation<Void, Never>] = []
    private var phaseOneRelease: CheckedContinuation<Void, Never>?
    private var releaseRequested = false
    private var snapshots: [ChatRecoveryKey: ChatRecoverySnapshot] = [:]

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        snapshots[key]
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        saveCount += 1
        switch saveCount {
        case 1:
            snapshots[key] = snapshot
        case 2:
            phaseOneStarted = true
            let waiters = phaseOneWaiters
            phaseOneWaiters.removeAll()
            waiters.forEach { $0.resume() }
            await withCheckedContinuation { continuation in
                if releaseRequested {
                    releaseRequested = false
                    continuation.resume()
                } else {
                    phaseOneRelease = continuation
                }
            }
            snapshots[key] = snapshot
        case 3:
            snapshots[key] = snapshot
        case 4:
            throw ChatRecoveryError.storageUnavailable
        default:
            snapshots[key] = snapshot
        }
    }

    func remove(for key: ChatRecoveryKey) async throws {
        snapshots.removeValue(forKey: key)
    }
    func purge(bindingID: UUID) async throws {}
    func sweep(keeping bindingIDs: Set<UUID>) async throws {}
    func eraseAllAfterCredentialDeletion() async throws {}

    func waitUntilPhaseOneStarted() async {
        guard !phaseOneStarted else { return }
        await withCheckedContinuation { continuation in
            phaseOneWaiters.append(continuation)
        }
    }

    func releasePhaseOne() {
        if let phaseOneRelease {
            self.phaseOneRelease = nil
            phaseOneRelease.resume()
        } else {
            releaseRequested = true
        }
    }
}

private actor UnreadableRecoveryStore: ChatRecoveryPersisting {
    private let snapshot: ChatRecoverySnapshot
    private(set) var writeCount = 0

    init(snapshot: ChatRecoverySnapshot) {
        self.snapshot = snapshot
    }

    var unreadSnapshot: ChatRecoverySnapshot { snapshot }

    func load(for key: ChatRecoveryKey) async throws -> ChatRecoverySnapshot? {
        throw ChatRecoveryError.storageUnavailable
    }

    func save(_ snapshot: ChatRecoverySnapshot, for key: ChatRecoveryKey) async throws {
        writeCount += 1
    }

    func remove(for key: ChatRecoveryKey) async throws {
        writeCount += 1
    }

    func purge(bindingID: UUID) async throws {}
    func sweep(keeping bindingIDs: Set<UUID>) async throws {}
    func eraseAllAfterCredentialDeletion() async throws {}
}

@MainActor
private func recoveryModel(
    binding: UUID,
    recoveryStore: any ChatRecoveryPersisting
) -> ChatModel {
    let recoveryWriteGate = ChatRecoveryWriteGate()
    let client = DroverClient(
        config: ServerConfig(urlString: "http://recovery.test:7080")!,
        token: "synthetic-token",
        credentialBindingID: binding,
        session: MockURLProtocol.session()
    )
    return ChatModel(
        client: client,
        sessionID: "recovery-session",
        recoveryStore: recoveryStore,
        recoveryWriteGate: recoveryWriteGate,
        recoveryGeneration: recoveryWriteGate.generation
    )
}

private func recoveryKey(binding: UUID) -> ChatRecoveryKey {
    ChatRecoveryKey(
        serverURL: ServerConfig(urlString: "http://recovery.test:7080")!.baseURL,
        credentialBindingID: binding,
        sessionID: "recovery-session"
    )
}

@MainActor
private func lifecycleRecoveryModel(
    binding: UUID,
    recoveryStore: any ChatRecoveryPersisting,
    recoveryWriteGate: ChatRecoveryWriteGate
) -> ChatModel {
    let client = DroverClient(
        config: ServerConfig(urlString: "http://recovery.test:7080")!,
        token: "synthetic-token",
        credentialBindingID: binding,
        session: MockURLProtocol.session()
    )
    return ChatModel(
        client: client,
        sessionID: "recovery-session",
        recoveryStore: recoveryStore,
        recoveryWriteGate: recoveryWriteGate,
        recoveryGeneration: recoveryWriteGate.generation
    )
}

private func waitForRecoveryRecord(
    _ condition: @escaping @Sendable () async throws -> Bool
) async throws {
    let deadline = ContinuousClock.now + .seconds(1)
    while !(try await condition()) {
        guard ContinuousClock.now < deadline else {
            throw RecoveryTestTimeout()
        }
        try await Task.sleep(for: .milliseconds(10))
    }
}

@MainActor
private func waitForChatState(_ condition: () -> Bool) async throws {
    let deadline = ContinuousClock.now + .seconds(1)
    while !condition() {
        guard ContinuousClock.now < deadline else {
            throw RecoveryTestTimeout()
        }
        try await Task.sleep(for: .milliseconds(10))
    }
}

private struct RecoveryTestTimeout: Error {}
