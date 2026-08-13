import Foundation
import Testing
@testable import DroverKit

@Test(arguments: [
    ("content-consent-complete", ContentAnalysisPropagation.complete, 0, 0),
    ("content-consent-partial", ContentAnalysisPropagation.partial, 1, 1),
    ("content-consent-failed", ContentAnalysisPropagation.failed, 0, 1),
    ("content-consent-repair-failed", ContentAnalysisPropagation.failed, 1, 1),
])
func backendContentConsentFixturesPreserveFleetPropagation(
    fixtureName: String,
    expectedPropagation: ContentAnalysisPropagation,
    expectedEpoch: Int,
    expectedAffectedHosts: Int
) throws {
    let url = try #require(droverKitFixtureURL(fixtureName))
    let status = try JSONDecoder().decode(ContentAnalysisStatus.self, from: Data(contentsOf: url))

    #expect(status.consentEpoch == expectedEpoch)
    #expect(status.propagation == expectedPropagation)
    #expect(status.affectedHosts.count == expectedAffectedHosts)
}

@Test func durableRepairFailureFixturePreservesCentralIntentAndFailureDetail() throws {
    let url = try #require(droverKitFixtureURL("content-consent-repair-failed"))
    let status = try JSONDecoder().decode(
        ContentAnalysisStatus.self, from: Data(contentsOf: url)
    )

    #expect(status.enabled)
    #expect(status.propagationOutcome == .failed)
    #expect(status.affectedHosts.first?.hostID == "fleet")
    #expect(status.affectedHosts.first?.error == "durable consent repair failed")
}

@Test func legacyConsentStatusDefaultsToNoFleetOutcome() throws {
    let status = try JSONDecoder().decode(ContentAnalysisStatus.self, from: Data(
        #"{"enabled":true,"backend":"local","external_disclosure_accepted":false,"pending_model_jobs":0}"#.utf8
    ))

    #expect(status.consentEpoch == nil)
    #expect(status.propagation == nil)
    #expect(status.hosts.isEmpty)
}

@Test func unknownConsentPropagationAndHostStateFailSafe() throws {
    let status = try JSONDecoder().decode(ContentAnalysisStatus.self, from: Data(#"""
    {"enabled":false,"backend":"local","external_disclosure_accepted":false,
     "pending_model_jobs":0,"consent_epoch":2,"propagation":"future-result",
     "hosts":[{"host_id":"future-host","status":"new-server-state"}]}
    """#.utf8))

    #expect(status.hosts.last?.state == .unknown)
    #expect(status.propagationOutcome == .failed)
    #expect(status.affectedHosts.map(\.hostID) == ["future-host"])
}

@Test func analyticsDecodesPaginationAndObservedAggregateMetadata() throws {
    let snapshot = try JSONDecoder().decode(AnalyticsSnapshot.self, from: Data(#"""
    {"cockpit_api_version":1,"filters":{"days":7,"limit":2},
     "provider_capacity":{"status":"unavailable","data":[]},
     "activity":{"status":"ok","observed_at":"2026-08-08T18:00:00Z",
      "coverage":{"token_percent":80},"data":{
       "totals":{"session_count":2,"total_tokens":20,"cost_usd":0,
        "cache_read_tokens":0,"cache_write_tokens":0,"total_latency_ms":20,
        "average_latency_ms":10,"metadata":{"source":"drover_observed",
         "observed_at":"2026-08-08T18:00:00Z","freshness":"fresh",
         "coverage":{"token_percent":80}}},
       "projects":[],"harnesses":[],"hosts":[],"models":[],
       "project_metric":"tokens","coverage":{"token_percent":80},
       "metadata":{"source":"drover_observed","observed_at":"2026-08-08T18:00:00Z",
        "freshness":"fresh","coverage":{"token_percent":80}},
       "pagination":{"projects":{"limit":2,"next_cursor":"next-project"},
        "harnesses":{"limit":2,"next_cursor":null},
        "hosts":{"limit":2,"next_cursor":"next-host"},
        "models":{"limit":2,"next_cursor":null}}}}}
    """#.utf8))

    #expect(snapshot.activity.data?.metadata?.source == "drover_observed")
    #expect(snapshot.activity.data?.metadata?.freshness == .fresh)
    #expect(snapshot.activity.data?.totals.metadata?.coverage.tokenPercent == 80)
    #expect(snapshot.activity.data?.pagination.projects.nextCursor == "next-project")
    #expect(snapshot.activity.data?.pagination.hosts.nextCursor == "next-host")
}

@Test func analyticsDecodesProjectContributorAttributionCounts() throws {
    let project = try JSONDecoder().decode(ProjectActivity.self, from: Data(#"""
    {"project_key":"arniesaha/drover","session_count":3,"total_tokens":0,
     "cost_usd":0,"cache_read_tokens":0,"cache_write_tokens":0,
     "total_latency_ms":0,"harnesses":["claude-code"],
     "harness_attributed_session_count":1,"hosts":[],
     "host_attributed_session_count":0}
    """#.utf8))

    #expect(project.harnessAttributedSessionCount == 1)
    #expect(project.hostAttributedSessionCount == 0)
}

@Test func overviewDecodesPartialProviderFailureWithoutDroppingActivity() throws {
    let fixture = Data(#"""
    {
      "cockpit_api_version": 1,
      "provider_capacity": {
        "status": "stale",
        "observed_at": "2026-08-08T18:00:00+00:00",
        "coverage": {"source": "provider_reported", "account_count": 1},
        "data": [{
          "snapshot_id": "snapshot-1", "dedup_key": "codex:personal",
          "provider": "openai", "account_label": "Personal", "plan_label": "Plus",
          "host_id": "mac-mini", "status": "stale",
          "observed_at": "2026-08-08T18:00:00+00:00", "windows": [],
          "source": "codex_app_server", "error_category": null
        }]
      },
      "activity": {
        "status": "ok",
        "observed_at": "2026-08-08T18:01:00+00:00",
        "coverage": {
          "attributable_session_percent": 100.0, "token_percent": 42.0,
          "cost_percent": 0.0, "cache_percent": 0.0, "latency_percent": 50.0
        },
        "data": {
          "totals": {
            "session_count": 12, "total_tokens": 1000, "cost_usd": 0.0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_latency_ms": 500.0, "average_latency_ms": 250.0
          },
          "projects": [], "harnesses": [], "hosts": [], "models": [],
          "project_metric": "sessions",
          "coverage": {
            "attributable_session_percent": 100.0, "token_percent": 42.0,
            "cost_percent": 0.0, "cache_percent": 0.0, "latency_percent": 50.0
          }
        }
      },
      "popular_projects": [{
        "project_key": "arniesaha/drover", "session_count": 12,
        "total_tokens": 1000, "cost_usd": 0.0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "total_latency_ms": 500.0,
        "average_latency_ms": 250.0, "harnesses": ["codex"],
        "hosts": ["mac-mini"], "metric": "sessions"
      }]
    }
    """#.utf8)

    let overview = try JSONDecoder().decode(CockpitOverview.self, from: fixture)

    #expect(overview.providerCapacity.status == .stale)
    #expect(overview.activity.status == .ok)
    #expect(overview.activity.data?.totals.sessionCount == 12)
    #expect(overview.popularProjects.first?.metric == .sessions)
    #expect(overview.insightCounts == nil)
}

@Test func errorEnvelopeWithNullDataDoesNotDropOtherSections() throws {
    let fixture = Data(#"""
    {
      "cockpit_api_version": 1,
      "provider_capacity": {
        "status": "unavailable", "observed_at": null,
        "coverage": null, "data": []
      },
      "activity": {
        "status": "error", "observed_at": null,
        "coverage": null, "data": null
      },
      "popular_projects": []
    }
    """#.utf8)

    let overview = try JSONDecoder().decode(CockpitOverview.self, from: fixture)

    #expect(overview.providerCapacity.data == [])
    #expect(overview.activity.status == .error)
    #expect(overview.activity.data == nil)
}

@Test func providerWindowPreservesMultipleResetWindows() throws {
    let fixture = Data(#"""
    {
      "snapshot_id": "snapshot-1", "dedup_key": "codex:personal",
      "provider": "openai", "account_label": "Personal", "plan_label": "Plus",
      "host_id": "mac-mini", "status": "ok",
      "observed_at": "2026-08-08T18:00:00+00:00", "source": "codex_app_server",
      "windows": [
        {"kind":"primary","used_percent":20.0,"window_minutes":300,
         "resets_at":"2026-08-08T20:00:00+00:00"},
        {"kind":"secondary","used_percent":40.0,"window_minutes":10080,
         "resets_at":"2026-08-15T18:00:00+00:00"}
      ]
    }
    """#.utf8)

    let account = try JSONDecoder().decode(ProviderAccount.self, from: fixture)

    #expect(account.windows.map(\.kind) == ["primary", "secondary"])
    #expect(account.windows[1].windowMinutes == 10_080)
}

@Test func providerAccountDefaultsOnlyAdditiveWindows() throws {
    let fixture = Data(#"""
    {
      "snapshot_id":"snapshot-1", "dedup_key":"codex:personal",
      "provider":"openai", "account_label":"Personal", "plan_label":null,
      "host_id":"mac-mini", "status":"usage_unavailable",
      "observed_at":"2026-08-08T18:00:00+00:00", "source":"harness_inventory"
    }
    """#.utf8)

    let account = try JSONDecoder().decode(ProviderAccount.self, from: fixture)

    #expect(account.windows.isEmpty)
    #expect(account.errorCategory == nil)
}

@Test func malformedPresentProviderWindowDoesNotBecomeAnEmptyArray() {
    let fixture = Data(#"""
    {
      "snapshot_id":"snapshot-1", "dedup_key":"codex:personal",
      "provider":"openai", "account_label":"Personal", "host_id":"mac-mini", "status":"ok",
      "observed_at":"2026-08-08T18:00:00+00:00", "source":"codex_app_server",
      "windows":[{"used_percent":20.0}]
    }
    """#.utf8)

    #expect(throws: DecodingError.self) {
        try JSONDecoder().decode(ProviderAccount.self, from: fixture)
    }
}

@Test func malformedRequiredProviderIdentityFailsDecoding() {
    let fixture = Data(#"""
    {
      "snapshot_id":"snapshot-1", "dedup_key":"codex:personal",
      "account_label":"Personal", "host_id":"mac-mini", "status":"ok",
      "observed_at":"2026-08-08T18:00:00+00:00", "source":"codex_app_server",
      "windows":[]
    }
    """#.utf8)

    #expect(throws: DecodingError.self) {
        try JSONDecoder().decode(ProviderAccount.self, from: fixture)
    }
}

@Test func emptyRequiredProviderIdentityFailsDecoding() {
    let fixture = Data(#"""
    {
      "snapshot_id":"snapshot-1", "dedup_key":"codex:personal",
      "provider":"", "account_label":"Personal", "host_id":"mac-mini", "status":"ok",
      "observed_at":"2026-08-08T18:00:00+00:00", "source":"codex_app_server",
      "windows":[]
    }
    """#.utf8)

    #expect(throws: DecodingError.self) {
        try JSONDecoder().decode(ProviderAccount.self, from: fixture)
    }
}

@Test func harnessSnapshotCockpitCapabilitiesAreBackwardCompatible() throws {
    let old = try HarnessSnapshot.decode(from: Data(#"{"hosts":[],"sessions":[]}"#.utf8))
    let current = try HarnessSnapshot.decode(from: Data(#"""
    {"hosts":[],"sessions":[],"cockpit_api_version":1,
     "cockpit_sections":["provider_capacity","activity","popular_projects","insights"]}
    """#.utf8))

    #expect(old.cockpitAPIVersion == nil)
    #expect(old.cockpitSections == [])
    #expect(current.cockpitAPIVersion == 1)
    #expect(current.cockpitSections == ["provider_capacity", "activity", "popular_projects", "insights"])
}

@Test func backendCockpitOverviewFixtureDecodesInsightCounts() throws {
    let fixtureURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .appendingPathComponent("Fixtures/cockpit-overview-with-insights.json")

    let overview = try JSONDecoder().decode(
        CockpitOverview.self,
        from: Data(contentsOf: fixtureURL)
    )

    #expect(overview.insightCounts?.critical == 0)
    #expect(overview.insightCounts?.high == 0)
    #expect(overview.insightCounts?.medium == 0)
    #expect(overview.insightCounts?.low == 0)
}

@Test func insightDetailDecodesTruthfulCheckAgainAvailability() throws {
    let fixture = Data(#"""
    {
      "finding": {
        "finding_id":"finding-one","analyzer_id":"deterministic.hook_validity",
        "rule_id":"hook.missing","target_type":"hook",
        "target_id":"mac-mini/codex/pre-tool","analyzer_class":"deterministic",
        "severity":"high","confidence":"confirmed","title":"Hook is missing",
        "impact":"The hook cannot run.","remediation":["Restore it."],"state":"open",
        "first_seen_at":"2026-08-08T18:00:00Z","last_seen_at":"2026-08-08T18:01:00Z"
      },
      "evidence": [],
      "actions": {
        "check_again": {
          "available": false,
          "reason": "Scoped reanalysis is not available for this analyzer."
        }
      }
    }
    """#.utf8)

    let detail = try JSONDecoder().decode(InsightDetail.self, from: fixture)

    #expect(detail.actions.checkAgain.available == false)
    #expect(detail.actions.checkAgain.reason == "Scoped reanalysis is not available for this analyzer.")
}

@Test func legacyInsightDetailDoesNotInventCheckAgainAvailability() throws {
    let fixture = Data(#"""
    {
      "finding": {
        "finding_id":"finding-one","analyzer_id":"deterministic.hook_validity",
        "rule_id":"hook.missing","target_type":"hook",
        "target_id":"mac-mini/codex/pre-tool","analyzer_class":"deterministic",
        "severity":"high","confidence":"confirmed","title":"Hook is missing",
        "impact":"The hook cannot run.","remediation":["Restore it."],"state":"open",
        "first_seen_at":"2026-08-08T18:00:00Z","last_seen_at":"2026-08-08T18:01:00Z"
      },
      "evidence": []
    }
    """#.utf8)

    let detail = try JSONDecoder().decode(InsightDetail.self, from: fixture)

    #expect(detail.actions.checkAgain.available == false)
}
