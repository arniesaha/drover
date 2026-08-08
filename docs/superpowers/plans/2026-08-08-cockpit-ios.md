# Cockpit iOS Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing fleet inbox into the approved analytics-first Home with provider capacity, project activity, Insights navigation, lifecycle actions, and content-analysis privacy controls.

**Architecture:** `DroverKit` owns wire models, networking, polling state, and deterministic presentation formatting. The app target owns small SwiftUI sections and drilldown screens inside the existing `NavigationStack`; it does not add a tab bar or replace session navigation. Each API section renders independently so stale or unavailable providers cannot blank fleet activity.

**Tech Stack:** Swift 6, SwiftUI, Observation, URLSession, Swift Testing, XcodeGen, iOS 18+.

## Global Constraints

- Preserve the existing single `NavigationStack`, session list, launch action, and settings gear.
- Home order is attention, provider capacity, recent activity, popular projects, configuration insights, then sessions.
- Clearly distinguish provider-reported capacity from Drover-observed analytics.
- Never show negative reset countdowns; expired windows render stale.
- Show metric name and coverage when popular projects fall back from tokens to sessions.
- Model findings must visibly identify model judgment and uncertainty.
- Check Again only reruns analysis; no client action applies configuration changes.
- Content analysis is disabled by default and cloud consent has explicit disclosure.

---

## File Structure

- `apps/drover/DroverKit/Sources/DroverKit/CockpitModels.swift`: Home/Analytics/Insights wire types.
- `apps/drover/DroverKit/Sources/DroverKit/CockpitStore.swift`: polling, partial failure, filters, and lifecycle actions.
- `apps/drover/DroverKit/Sources/DroverKit/CockpitPresentation.swift`: countdown, freshness, coverage, severity, and confidence formatting.
- `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift`: cockpit and Insights requests.
- `apps/drover/Drover/Screens/Cockpit/`: Home sections and Analytics/Insights/detail views.
- `apps/drover/Drover/Screens/Sessions/SessionsView.swift`: embed cockpit sections above existing sessions.
- `apps/drover/Drover/Screens/Settings/SettingsView.swift`: consent, revocation, and excerpt purge controls.
- `apps/drover/DroverKit/Tests/DroverKitTests/CockpitModelsTests.swift`: tolerant decoding.
- `apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift`: formatting and hierarchy.
- `apps/drover/DroverKit/Tests/DroverKitTests/CockpitStoreTests.swift`: partial failure and actions.
- `apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift`: paths, auth, query encoding, and bodies.

### Task 1: Wire models and authenticated client methods

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/CockpitModels.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/CockpitModelsTests.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift`

**Interfaces:**
- Produces: `CockpitOverview`, `ProviderAccount`, `ProviderWindow`, `ActivitySummary`, `PopularProject`, `AnalyticsSnapshot`, `InsightSummary`, `InsightDetail`, `InsightPage`, and new `DroverClient` methods.

- [ ] **Step 1: Write failing tolerant-decoding tests**

```swift
@Test func overviewDecodesPartialProviderFailureWithoutDroppingActivity() throws {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    let overview = try decoder.decode(CockpitOverview.self, from: fixture)
    #expect(overview.providerCapacity.status == .stale)
    #expect(overview.activity.status == .ok)
    #expect(overview.popularProjects.first?.metric == .sessions)
}

@Test func providerWindowPreservesMultipleResetWindows() throws {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    let account = try decoder.decode(ProviderAccount.self, from: fixture)
    #expect(account.windows.map(\.kind) == ["primary", "secondary"])
}
```

- [ ] **Step 2: Write failing client request tests**

Assert `cockpitOverview(days: 7)` calls `/cockpit/overview?days=7`, Analytics encodes allowlisted filters, Insights cursor pagination encodes once, dismiss sends `{"reason":"..."}`, and Check Again sends no mutation payload.

- [ ] **Step 3: Run tests and confirm missing model/client failures**

Run: `cd apps/drover/DroverKit && swift test --filter 'CockpitModelsTests|ClientTests'`

Expected: FAIL because cockpit types and client methods are absent.

- [ ] **Step 4: Implement wire types with explicit coding keys and partial envelopes**

```swift
public struct SectionEnvelope<Value: Decodable & Sendable>: Decodable, Sendable {
    public let status: DataStatus
    public let observedAt: Date?
    public let coverage: Coverage?
    public let data: Value
}

public struct CockpitOverview: Decodable, Sendable {
    public let providerCapacity: SectionEnvelope<[ProviderAccount]>
    public let activity: SectionEnvelope<ActivitySummary>
    public let popularProjects: [PopularProject]
    public let insightCounts: InsightCounts
}
```

Extend `HarnessSnapshot` with optional `cockpitAPIVersion` and
`cockpitSections`. Missing fields decode to no cockpit capability, preserving
compatibility with older servers.

Use lenient defaults only for additive arrays and optional fields; malformed required identity fields remain decoding errors.

- [ ] **Step 5: Implement client methods through the existing authenticated request helper**

Add `cockpitOverview`, `analytics`, `insights`, `insightDetail`, `acknowledgeInsight`, `dismissInsight`, `checkInsight`, `contentAnalysisStatus`, `setContentAnalysisConsent`, `revokeContentAnalysis`, and `purgeContentExcerpts`.

- [ ] **Step 6: Run focused tests**

Run: `cd apps/drover/DroverKit && swift test --filter 'CockpitModelsTests|ClientTests'`

Expected: PASS.

- [ ] **Step 7: Commit models and client**

```bash
git add apps/drover/DroverKit/Sources/DroverKit/CockpitModels.swift apps/drover/DroverKit/Sources/DroverKit/DroverClient.swift apps/drover/DroverKit/Tests/DroverKitTests/CockpitModelsTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift
git commit -m "feat(ios): add cockpit API models"
```

### Task 2: Cockpit state and presentation logic

**Files:**
- Create: `apps/drover/DroverKit/Sources/DroverKit/CockpitStore.swift`
- Create: `apps/drover/DroverKit/Sources/DroverKit/CockpitPresentation.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/CockpitStoreTests.swift`
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift`

**Interfaces:**
- Consumes: Task 1 client and models.
- Produces: `CockpitStore`, `ProviderCapacityPresentation`, `ProjectActivityPresentation`, and `InsightPresentation`.

- [ ] **Step 1: Write failing countdown and coverage tests**

```swift
@Test func expiredResetNeverShowsNegativeCountdown() {
    let value = ProviderCapacityPresentation(window: expiredWindow, now: now)
    #expect(value.resetText == "Stale")
    #expect(value.isStale)
}

@Test func projectFallbackNamesSessionMetricAndCoverage() {
    let value = ProjectActivityPresentation(project: sessionRankedProject)
    #expect(value.valueText == "12 sessions")
    #expect(value.coverageText == "42% token coverage")
}
```

- [ ] **Step 2: Write failing partial-refresh and action tests**

```swift
@Test func failedProviderRefreshKeepsLastGoodActivity() async {
    let store = CockpitStore(client: clientWithProviderFailure)
    await store.refresh()
    #expect(store.overview?.activity.data.totalSessions == 18)
    #expect(store.providerError != nil)
}
```

Also test optimistic acknowledge rollback on HTTP failure, required dismissal reason, cursor append, and cancellation suppression.

- [ ] **Step 3: Run tests and confirm missing store/presentation failures**

Run: `cd apps/drover/DroverKit && swift test --filter 'CockpitStoreTests|CockpitPresentationTests'`

Expected: FAIL.

- [ ] **Step 4: Implement deterministic formatting**

Format used/remaining labels from provider units, reset timestamps relative to an injected clock, stale age, source class, severity, confidence, and analytics metric/coverage. Keep formatting out of SwiftUI views.

- [ ] **Step 5: Implement observable store with independent section state**

`CockpitStore` polls overview on foreground cadence, retains last successful data, exposes section-specific errors, loads Analytics/Insights on demand, and serializes lifecycle actions. It never clears good content when a refresh is cancelled or one section fails.

Only request cockpit endpoints when `HarnessSnapshot.cockpitAPIVersion >= 1`;
otherwise preserve the current fleet inbox without showing an error.

- [ ] **Step 6: Run DroverKit tests**

Run: `cd apps/drover/DroverKit && swift test`

Expected: PASS.

- [ ] **Step 7: Commit state and presentation**

```bash
git add apps/drover/DroverKit/Sources/DroverKit/CockpitStore.swift apps/drover/DroverKit/Sources/DroverKit/CockpitPresentation.swift apps/drover/DroverKit/Tests/DroverKitTests/CockpitStoreTests.swift apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift
git commit -m "feat(ios): model cockpit state and formatting"
```

### Task 3: Analytics-first Home and drilldown screens

**Files:**
- Create: `apps/drover/Drover/Screens/Cockpit/ProviderCapacitySection.swift`
- Create: `apps/drover/Drover/Screens/Cockpit/ActivitySummarySection.swift`
- Create: `apps/drover/Drover/Screens/Cockpit/PopularProjectsSection.swift`
- Create: `apps/drover/Drover/Screens/Cockpit/InsightsSummaryRow.swift`
- Create: `apps/drover/Drover/Screens/Cockpit/AnalyticsView.swift`
- Create: `apps/drover/Drover/Screens/Cockpit/InsightsView.swift`
- Create: `apps/drover/Drover/Screens/Cockpit/InsightDetailView.swift`
- Modify: `apps/drover/Drover/Screens/Sessions/SessionsView.swift`
- Modify: `apps/drover/Drover/DroverApp.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift`

**Interfaces:**
- Consumes: `CockpitStore` and presentation values from Task 2.
- Produces: approved Home hierarchy plus Analytics and Insights navigation.

- [ ] **Step 1: Add presentation ordering tests before view code**

```swift
@Test func homeSectionsFollowApprovedHierarchy() {
    #expect(HomeSection.visible(for: populatedOverview) == [
        .attention, .providerCapacity, .activity, .popularProjects, .insights, .sessions
    ])
}
```

- [ ] **Step 2: Run the focused test and confirm missing hierarchy type**

Run: `cd apps/drover/DroverKit && swift test --filter CockpitPresentationTests`

Expected: FAIL because `HomeSection` is absent.

- [ ] **Step 3: Implement small Home sections and preserve the session inbox**

Insert provider, activity, project, and insight sections after `FleetHeader` and before session rows. Each section owns only layout; it receives presentation values and navigation closures. Hide empty healthy sections, but render stale/unavailable provider cards with explicit copy.

- [ ] **Step 4: Implement Analytics filters and source labeling**

Provide window, host, harness, provider, model, and project filters. Label provider subscription rows `Provider reported` and operational charts `Drover observed`. Display project harness/host contributors and token coverage.

- [ ] **Step 5: Implement Insights feed and detail actions**

Rank and filter findings, distinguish deterministic versus model badges, show evidence/remediation, require a dismissal reason sheet, and label Check Again as reanalysis. There is no Apply or Fix button.

- [ ] **Step 6: Generate the Xcode project and run builds/tests**

Run: `cd apps/drover && xcodegen generate`

Expected: project generation succeeds and includes new files through existing source globs.

Run: `cd apps/drover/DroverKit && swift test`

Expected: PASS.

Run: `cd apps/drover && xcodebuild -project Drover.xcodeproj -scheme Drover -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`

Expected: BUILD SUCCEEDED.

- [ ] **Step 7: Commit the cockpit UI**

```bash
git add apps/drover/Drover apps/drover/DroverKit/Tests/DroverKitTests/CockpitPresentationTests.swift apps/drover/Drover.xcodeproj
git commit -m "feat(ios): add analytics-first fleet cockpit"
```

### Task 4: Content-analysis privacy controls and end-to-end states

**Files:**
- Create: `apps/drover/Drover/Screens/Settings/ContentAnalysisSettings.swift`
- Modify: `apps/drover/Drover/Screens/Settings/SettingsView.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/CockpitStore.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/CockpitStoreTests.swift`
- Test: `apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift`

**Interfaces:**
- Consumes: content consent/status endpoints from Plan 3 and Task 1 client methods.
- Produces: local consent, external disclosure, revocation, and excerpt purge UI.

- [ ] **Step 1: Write failing consent-state tests**

```swift
@Test func cloudConsentRequiresDisclosureAcknowledgement() async {
    let store = CockpitStore(client: mockClient)
    await store.enableContentAnalysis(policy: .cloud, disclosureAccepted: false)
    #expect(store.contentConsentError == "Review and accept the external analysis disclosure.")
    #expect(mockClient.requests.isEmpty)
}
```

Also test local consent without disclosure, revocation preserving findings, and purge confirmation copy.

- [ ] **Step 2: Run tests and confirm missing privacy-state behavior**

Run: `cd apps/drover/DroverKit && swift test --filter 'CockpitStoreTests|ClientTests'`

Expected: FAIL.

- [ ] **Step 3: Implement privacy settings**

Show disabled/local/cloud states. Local is recommended. Cloud selection presents exactly what content may leave the device and requires affirmative acceptance. Revocation and excerpt purge are separate destructive confirmations with clear consequences.

- [ ] **Step 4: Add accessibility and compact-screen checks**

Give capacity cards combined labels including reset time, expose severity/confidence/source class on findings, ensure Dynamic Type does not require horizontal scrolling for finding text, and keep project/provider card strips independently scrollable.

- [ ] **Step 5: Run final iOS verification**

Run: `cd apps/drover/DroverKit && swift test`

Expected: PASS.

Run: `cd apps/drover && xcodegen generate && xcodebuild -project Drover.xcodeproj -scheme Drover -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build`

Expected: BUILD SUCCEEDED.

- [ ] **Step 6: Commit privacy controls**

```bash
git add apps/drover/Drover/Screens/Settings apps/drover/DroverKit/Sources/DroverKit/CockpitStore.swift apps/drover/DroverKit/Tests/DroverKitTests/CockpitStoreTests.swift apps/drover/DroverKit/Tests/DroverKitTests/ClientTests.swift apps/drover/Drover.xcodeproj
git commit -m "feat(ios): add advisory privacy controls"
```

## Stage Acceptance

Run the full Python suite from the repository root: `uv run pytest tests/ -q`.

Run the full DroverKit suite: `cd apps/drover/DroverKit && swift test`.

Run an iOS simulator build after `xcodegen generate`. Verify manually on a compact iPhone simulator that Home remains useful with zero providers, stale provider data, no OTLP data, and a mixture of deterministic and model findings.
