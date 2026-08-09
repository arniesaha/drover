import Foundation
import Testing
@testable import DroverKit

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
     "cockpit_sections":["provider_capacity","activity","popular_projects"]}
    """#.utf8))

    #expect(old.cockpitAPIVersion == nil)
    #expect(old.cockpitSections == [])
    #expect(current.cockpitAPIVersion == 1)
    #expect(current.cockpitSections == ["provider_capacity", "activity", "popular_projects"])
}
