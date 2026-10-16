import Foundation
import Testing
import UserNotifications
@testable import DroverKit

func attentionItemJSON(kind: String = "agent_question", uncertain: Bool = false,
                       refreshedAt: String? = nil, number: Int? = 1) -> String {
    """
    {"id":"attention-1","workspace_id":"workspace","actor_principal":"credential:device",
     "goal_id":"goal","repo":"example/project","kind":"\(kind)",
     "url":"https://github.com/example/project/issues/1","refreshed_at":"\(refreshedAt ?? ISO8601DateFormatter().string(from: Date()))","freshness_seconds":240,
     "stale":false,"actions":\(kind == "agent_question" ? "[\"reject\"]" : "[]"),
     "number":\(number.map(String.init) ?? "null"),"session_id":"session-1","decision_id":"decision-1","category":"revision",
     "uncertain":\(uncertain)}
    """
}

func attentionResponse(_ items: [String], truncated: Bool = false) throws -> WorkAttentionResponse {
    try JSONDecoder().decode(WorkAttentionResponse.self, from: Data("""
    {"items":[\(items.joined(separator: ","))],"stale":false,"truncated":\(truncated),"poll_interval_seconds":120}
    """.utf8))
}

private actor AttentionService: WorkAttentionServing {
    var snapshots: [WorkAttentionResponse]
    var sends = 0
    var reads = 0
    var failure: DroverError?
    var failDecision = false
    init(_ snapshots: [WorkAttentionResponse]) { self.snapshots = snapshots }
    func setFailure(_ error: DroverError = .transport("offline")) { failure = error }
    func setDecisionFailure() { failDecision = true }
    func workAttention() async throws -> WorkAttentionResponse {
        reads += 1
        if let failure { throw failure }
        if snapshots.count > 1 { return snapshots.removeFirst() }
        return snapshots[0]
    }
    func workDecide(item: WorkAttentionItem, action: String) async throws -> WorkDecisionResponse {
        sends += 1
        if failDecision { throw DroverError.transport("timeout") }
        return try JSONDecoder().decode(WorkDecisionResponse.self, from: Data(#"{"resolved":true}"#.utf8))
    }
}

@MainActor
struct WorkAttentionTests {
    @Test func initialFailureIsHidden() async throws {
        let service = AttentionService([try attentionResponse([])])
        await service.setFailure()
        let store = WorkAttentionStore(service: service)
        #expect(!store.isVisible)
        await store.refresh()
        #expect(!store.isVisible)
        #expect(store.message == nil)
        #expect(!store.isReachable)
    }

    @Test func emptySuccessIsHiddenUnlessTruncated() async throws {
        let service = AttentionService([try attentionResponse([], truncated: true), try attentionResponse([])])
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        #expect(store.isVisible)
        #expect(store.truncated)
        await store.refresh()
        #expect(!store.isVisible)
        #expect(store.isReachable)
        #expect(store.message == nil)
    }

    @Test func failedRefreshKeepsItemsVisibleAndDisablesDecisions() async throws {
        let full = try attentionResponse([attentionItemJSON()])
        let service = AttentionService([full])
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        #expect(store.canDecide(full.items[0], action: "reject"))
        await service.setFailure()
        await store.refresh()
        #expect(store.isVisible)
        #expect(store.items == full.items)
        #expect(!store.isReachable)
        #expect(!store.canDecide(full.items[0], action: "reject"))
        #expect(store.message == "Couldn't update. Pull to refresh.")
    }

    @Test func disabledWorkControlIsHiddenEvenWithCachedItems() async throws {
        for detail in ["work control unavailable", "work control disabled"] {
            let service = AttentionService([try attentionResponse([attentionItemJSON()], truncated: true)])
            let store = WorkAttentionStore(service: service)
            await store.refresh()
            #expect(store.isVisible)
            await service.setFailure(.httpStatus(503, detail))
            await store.refresh()
            #expect(!store.isVisible)
            #expect(store.items.isEmpty)
            #expect(!store.truncated)
            #expect(store.message == nil)
        }
    }

    @Test func fourKindsHaveReadableTitlesSourceAndRefreshAge() throws {
        for kind in ["environment_approval", "draft_pr", "agent_question", "blocked", "failed_session"] {
            let item = try #require(attentionResponse([attentionItemJSON(kind: kind)]).items.first)
            #expect(!item.title.isEmpty)
            #expect(item.githubURL?.host == "github.com")
            #expect(item.refreshAge(now: Date(timeIntervalSince1970: 1_792_065_660)).hasPrefix("Refreshed "))
            if kind != "agent_question" { #expect(item.actions.isEmpty) }
        }
    }

    @Test func rowLabelIncludesNumberWhenAvailable() throws {
        let item = try #require(attentionResponse([attentionItemJSON(kind: "draft_pr", number: 650)]).items.first)
        #expect(item.rowLabel == "Draft PR #650")
    }

    @Test func rowLabelOmitsNumberWhenUnavailable() throws {
        let item = try #require(attentionResponse([attentionItemJSON(kind: "agent_question", number: nil)]).items.first)
        #expect(item.rowLabel == "Agent question")
    }

    @Test func workAttentionListDefaultsToTwoRowsAndCanShowAll() throws {
        let response = try attentionResponse([
            attentionItemJSON(), attentionItemJSON(), attentionItemJSON()
        ])
        #expect(WorkAttentionListPresentation.displayedItems(response.items, isExpanded: false).count == 2)
        #expect(WorkAttentionListPresentation.displayedItems(response.items, isExpanded: true).count == 3)
        #expect(WorkAttentionListPresentation.needsExpansionControl(for: response.items))
        #expect(!WorkAttentionListPresentation.needsExpansionControl(for: Array(response.items.prefix(2))))
    }

    @Test func refreshAgeAndStalenessUseTheReturnedPolicy() throws {
        let json = attentionItemJSON(refreshedAt: "2026-10-14T12:00:00Z")
        let item = try #require(attentionResponse([json]).items.first)
        let refreshed = try #require(ISO8601DateFormatter().date(from: "2026-10-14T12:00:00Z"))
        #expect(item.refreshAge(now: refreshed.addingTimeInterval(60)) == "Refreshed 1m ago")
        #expect(!item.isStale(now: refreshed.addingTimeInterval(240)))
        #expect(item.isStale(now: refreshed.addingTimeInterval(241)))
    }

    @Test func concurrentTapsSendOneDecision() async throws {
        let full = try attentionResponse([attentionItemJSON()])
        let service = AttentionService([full, full, try attentionResponse([])])
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        async let first = store.decide(full.items[0], action: "reject")
        async let second = store.decide(full.items[0], action: "reject")
        _ = await (first, second)
        #expect(await service.sends == 1)
    }

    @Test func anotherDeviceResolvedDuringRefreshSendsNothing() async throws {
        let full = try attentionResponse([attentionItemJSON()])
        let service = AttentionService([full, try attentionResponse([])])
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        _ = await store.decide(full.items[0], action: "reject")
        #expect(await service.sends == 0)
        #expect(store.items.isEmpty)
        #expect(store.message?.contains("resolved") == true)
    }

    @Test func decisionRefreshesBeforeSendingAndAfterOutcome() async throws {
        let full = try attentionResponse([attentionItemJSON()])
        let service = AttentionService([full, full, try attentionResponse([])])
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        _ = await store.decide(full.items[0], action: "reject")
        #expect(await service.sends == 1)
        #expect(await service.reads == 3)
        #expect(store.items.isEmpty)
        _ = await store.decide(full.items[0], action: "reject")
        #expect(await service.sends == 1)
    }

    @Test func offlineAndUncertainDecisionsAreDisabled() async throws {
        let full = try attentionResponse([attentionItemJSON()])
        let service = AttentionService([full])
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        await service.setFailure()
        _ = await store.decide(full.items[0], action: "reject")
        #expect(await service.sends == 0)
        #expect(!store.canDecide(full.items[0], action: "reject"))
        #expect(!store.isReachable)
        #expect(store.isVisible)
        #expect(store.message == "Couldn't update. Pull to refresh.")
        let uncertain = try attentionResponse([attentionItemJSON(uncertain: true)])
        let second = WorkAttentionStore(service: AttentionService([uncertain]))
        await second.refresh()
        #expect(!second.canDecide(uncertain.items[0], action: "reject"))
    }

    @Test func timedOutDecisionIsNeverAutomaticallyRetried() async throws {
        let full = try attentionResponse([attentionItemJSON()])
        let service = AttentionService([full])
        await service.setDecisionFailure()
        let store = WorkAttentionStore(service: service)
        await store.refresh()
        _ = await store.decide(full.items[0], action: "reject")
        _ = await store.decide(full.items[0], action: "reject")
        #expect(await service.sends == 1)
        #expect(store.message?.contains("unknown") == true)
    }

    @Test func attentionNotificationRoutesWithoutSessionFallbackAndHasGenericText() throws {
        let ref = WorkAttentionRef(workspaceID: "workspace", id: "source-1")
        let content = LocalNotifier.content(attention: ref)
        #expect(content.title == "Needs you")
        #expect(content.body == "Open Drover to review pending work.")
        #expect(NotificationRoute.attentionRef(userInfo: content.userInfo) == ref)
        #expect(NotificationRoute.sessionID(userInfo: content.userInfo, requestIdentifier: "source-1") == nil)
        #expect(NotificationRoute.sessionIDs(userInfo: content.userInfo) == nil)
        let route = NotificationRoute()
        route.open(sessionID: "old-session")
        route.open(attentionRef: ref)
        #expect(route.pendingSessionID == nil)
        #expect(route.consumeAttention() == ref)
        #expect(route.consumeAttention() == nil)
        #expect(NotificationRoute.attentionRef(userInfo: ["attention_ref": ["id": "missing-workspace"]]) == nil)
    }

    @Test func foregroundBackgroundAndPushShareSourceDeduplication() async throws {
        let suite = "work-attention-tests-\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let notifier = AttentionSpy()
        let items = try attentionResponse([attentionItemJSON()]).items
        await WorkAttentionNotifications.evaluate(items, notifier: notifier, defaults: defaults)
        await WorkAttentionNotifications.evaluate(items, notifier: notifier, defaults: defaults)
        #expect(await notifier.count == 1)
        defaults.removeObject(forKey: "drover.work.seen.workspace")
        WorkAttentionNotifications.markSeen(WorkAttentionRef(workspaceID: "workspace", id: items[0].id), defaults: defaults)
        await WorkAttentionNotifications.evaluate(items, notifier: notifier, defaults: defaults)
        #expect(await notifier.count == 1)
    }
}

private actor AttentionSpy: Notifying {
    var count = 0
    func notify(title: String, body: String, id: String) async { count += 1 }
    func setBadge(_ count: Int) async {}
}

extension MockNetworkTests {
@Suite(.serialized)
struct WorkAttentionClientTests {
    let mock = MockNetwork()

    @Test func sharedWorkOperationsUseBearerAndNeverSendCallerIdentity() async throws {
        let json = attentionItemJSON()
        mock.handler = { request in
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
            if request.httpMethod == "GET" {
                #expect(request.url?.path == "/work/list")
                #expect(request.url?.query == "view=attention&limit=100")
                return (200, Data("{\"items\":[\(json)],\"stale\":false,\"truncated\":false,\"poll_interval_seconds\":120}".utf8))
            }
            #expect(request.url?.path == "/work/decide")
            let body = try! JSONSerialization.jsonObject(with: request.bodyStreamData()) as! [String: Any]
            #expect(Set(body.keys) == ["goal_id", "number", "decision_id", "action"])
            #expect(body["action"] as? String == "reject")
            #expect(body["decision_id"] as? String == "decision-1")
            return (200, Data(#"{"resolved":true}"#.utf8))
        }
        let client = mock.client()
        let items = try await client.workAttention().items
        #expect(try await client.workDecide(item: items[0], action: "reject").resolved)
    }
}
}
