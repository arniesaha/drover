# Work queue phase 3

The existing iOS inbox now includes one Needs you list for environment gates,
draft/review PRs, pending agent questions, and failed/blocked work. Rows display
their source repository and refresh age, retain their last observation after an
outage, and expose GitHub and available canonical session links. The list scrolls
with the inbox at every text size. Rows use wrapping system text, vertical
controls, and no height cap through accessibility size 5.

`drover_work_list(view="attention")`, also available as
`GET /work/list?view=attention`, projects live provider observations and scoped
`decision_events` across configured goals. An optional `goal_id` restricts the
read. Each row has `workspace_id`, the authenticated `actor_principal`, a stable
source identity, and refresh metadata. No provider body, question text, local
path, or new persisted queue is introduced. A later execution supersedes an old
failed session. The bounded response reports truncation. Poll intervals and
freshness thresholds come from each goal's versioned policy, with the shortest
configured interval used for the combined list.

Only actions offered by the current control service appear. The current GitHub
adapter supports navigation for environment and PR operations, so these rows
open GitHub. Pending questions can request revision through the existing safe
reject operation. Supported future adapter actions use the same decision path.
A confirmation names the action, then iOS refreshes the projection and checks
that the same workspace/request still offers it before calling the authenticated
`/work/decide` operation behind `drover_work_decide`. The control service rechecks
provider state and serializes the event audit. Resolved requests do not execute
again. Recorded uncertain intents offer inspection without actions; a lost
response disables decisions until refreshed. The client never retries a POST.

Existing notification routing accepts an `attention_ref` containing
`workspace_id` and `id` for APNs or local payloads. Taps focus the same inbox
list, including cold launch; a resolved item returns to the list. The existing
foreground/background refresh paths produce generic local alerts, sharing
source receipts with delivered attention pushes. Existing session notification
routing and session destinations remain available. This phase does not add a
new server APNs producer or change GitHub capabilities.

Focused coverage checks the four source categories, scoped identities, policy
poll/freshness values, safe action filtering, recovery, two-device serialization,
preflight resolution, offline/uncertain behavior, client authentication,
notification routing/deduplication, and largest Dynamic Type row growth.

Validation on the phase 3 branch:

From the repository root, 22 tests passed:

```sh
uv run --extra dev python -m pytest tests/test_work_queue_attention.py tests/test_work_queue_transport.py tests/test_work_queue_execution.py -q
```

From `apps/drover`, generate the local project with `xcodegen generate`, then run
only the three attention suites and the 13 existing notification routing tests.
The following exact command passed 24 tests on the local iOS simulator. The
simulator identifier is a disposable test destination.

```sh
xcodebuild test \
  -project Drover.xcodeproj \
  -scheme Drover \
  -destination 'platform=iOS Simulator,id=B149C6A0-62BA-4D8D-9AA1-6B5514F883E8' \
  -derivedDataPath .derivedData-work-p3 \
  -only-testing:DroverTests/WorkAttentionTests \
  -only-testing:DroverTests/MockNetworkTests/WorkAttentionClientTests \
  -only-testing:DroverTests/WorkAttentionLayoutTests \
  '-only-testing:DroverTests/theSessionIdKeyMatchesTheServerPayload()' \
  '-only-testing:DroverTests/aPushPayloadResolvesToItsSession()' \
  '-only-testing:DroverTests/aLocalNotificationFallsBackToItsRequestIdentifier()' \
  '-only-testing:DroverTests/anEmptyPayloadValueDoesNotBeatTheIdentifier()' \
  '-only-testing:DroverTests/aNotificationWithNothingUsableResolvesToNil()' \
  '-only-testing:DroverTests/aNonStringPayloadValueIsIgnored()' \
  '-only-testing:DroverTests/aTapIsHeldUntilSomethingCanNavigate()' \
  '-only-testing:DroverTests/consumingATapClearsIt()' \
  '-only-testing:DroverTests/anEmptyIdIsNotAPendingTap()' \
  '-only-testing:DroverTests/aSecondTapSupersedesAnUnconsumedFirst()' \
  '-only-testing:DroverTests/notificationModeDefaultsAndPersistence()' \
  '-only-testing:DroverTests/summaryPayloadRoutesOnlyToItsSessions()' \
  '-only-testing:DroverTests/summaryRouteSurvivesUntilConsumedAndReplacesSingleRoute()' \
  CODE_SIGNING_ALLOWED=NO
```

`git diff --check` passed. Scanning added lines and new files for U+2014 found
no matches. No full test suite was run.
