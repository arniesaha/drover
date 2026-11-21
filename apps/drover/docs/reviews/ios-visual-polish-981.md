# iOS visual polish #981 — worker review record

Base: `909fafd60eeb6cb5928af53d7937b4b28648b216`
Dedicated simulator: `42FB9708-1580-49A6-B291-667720C0F0EE`

## Repair scope

| Surface | Verified repair |
| --- | --- |
| Onboarding welcome | Filled **Set Up Drover** is the primary action; existing-server and demo routes remain visible secondary 44pt actions. |
| Home fleet controls | Overview and host chips now have 44pt minimum target heights; selected chips preserve the teal filled/outlined distinction. |
| Accounts | Host diagnostic details use an explicit warning symbol and accessibility label, alongside the existing per-host text. |
| Root tabs | Inbox uses the same Dynamic Type-aware tab-label helper as the other four tabs. |
| Inbox attention route | Approval requests and questions now share the Inbox attention list; the title describes both states. |
| Demo journey | The UI test follows the released `Demo` label and real Home → Inbox → approval → Home, tab, and Settings routes rather than obsolete identifiers. |

## Evidence and reproducibility

The post-change Xcode result bundle, attachments, and runner log are retained outside the checkout so generated review media is not committed:

- `/Users/arnab/scratch/drover-ios-981-worker/demo-full-journey-accepted.xcresult` — 1/1 passing, nine post-change screenshots.
- `/Users/arnab/scratch/drover-ios-981-worker/after-screenshots/manifest.json` — mapping for the exported screenshots.
- `/Users/arnab/scratch/drover-ios-981-worker/demo-full-journey-accepted.mov` — 59-second simulator interaction recording.
- `/Users/arnab/scratch/drover-ios-981-worker/visual-polish-rerun.xcresult` — prior broad Dynamic Type/dark-light run; 16/17 passed before the stale demo test was repaired.

Focused execution uses the core-journey’s normal `MainTabView` for the onboarding/normal-root checks (not the observability fixture shell), with `DemoModeFullJourney` for demo navigation. `AccessibilityJourneyUITests` supplies dark and largest-text snapshots. `swift test --filter InboxAttentionPresentationTests` passes the approval/question membership rule. Fixtures are synthetic; APNs, physical hardware, production sessions, and App Store workflow are intentionally outside this worker’s scope.

## Notification follow-up (owner report, 2026-11-21)

The notification audit traces actual iOS producers rather than treating the
server fallback as proof:

| Producer / lifecycle | Before | Corrected behavior |
| --- | --- | --- |
| Session local fallback (`AttentionWatcher` → `LocalNotifier`) | The watcher formed an actionable harness/state message, then `LocalNotifier` discarded it for generic “Session / Needs your input” copy. | A new unanswered question or approval uses `Drover needs you` plus only a bounded harness label and `reply requested` or `approval required`; no cwd, repository, prompt, or transcript is placed in the notification. Its persisted seen receipt and `PushRegistration.isActive` gate prevent refresh/reconnect/app-open duplication when hub push is active. |
| Work-control local fallback | Every fresh work-attention category was eligible for the generic `Needs you` alert. | Only fresh `agent_question` and `environment_approval` records alert, with explicit action copy. Draft/PR/failure rows remain available in-app but do not interrupt. Existing per-workspace receipts are retained across foreground/background evaluation and taps. |
| Foreground / resolve lifecycle | A delivery could remain visible once its session resolved. | The existing same-visible-session foreground path remains badge-only (no banner/sound). A successful snapshot clears only delivered/pending `input` alerts whose session is no longer actionable; completion/failure alerts remain distinct. |
| Tap and stale route | A missing session target could leave a pending route stranded. | Cold/warm/cross-tab session routing still enters Home’s navigation stack; a target absent from a completed snapshot clears the route, shows an in-app recovery notice, and leaves current sessions reachable. |

The APNs source (`src/drover/server/push/apns.py`) was inspected as a trace
lead only: it admits only awaiting input/approval in actionable mode and marks
only that state Time Sensitive (digest stays passive). This worker did not
change the server writer or claim a physical APNs delivery.

### Focused evidence

Reproduction encoded before the correction: with local fallback active, an
unanswered session whose working path was private produced generic copy because
`LocalNotifier.notify(title:body:id:)` replaced the watcher’s message. The
new notifier regression asserts the after-copy and confirms it contains neither
path nor repository material. Work-attention regression covers the two admitted
kinds, rejects draft/failure interruption, preserves the `attention_ref` route,
and verifies source-receipt dedupe. Delivery regression keeps active input and
completion alerts while clearing only resolved input alerts. The normal-root UI
tour captures stale-target recovery and the existing tour covers Inbox → Home
notification routing across rapid tabs and rotation.

Post-change outputs (all simulator/synthetic-fixture evidence, not physical
APNs) are retained outside the checkout:

- `/Users/arnab/scratch/drover-ios-981-worker/notification-followup-swift-test.log`
- `/Users/arnab/scratch/drover-ios-981-worker/notification-stale-route.xcresult` — 1/1 pass.
- `/Users/arnab/scratch/drover-ios-981-worker/notification-after-screenshots/manifest.json`

The exported `notification-stale-target-recovery` attachment visibly shows the
Home recovery notice and current-session list. A combined rerun of the older
cross-tab/rotation tour was interrupted once by the tool host and once at the
simulator UI-runner bootstrap; it is not counted as new passing evidence here.
Its prior accepted root-review result remains the baseline evidence for that
unchanged route. The new stale-route test was then run alone and passed.

Remaining physical gate: on a provisioned iPhone with notification permission
and a valid sandbox APNs token, create one fixture-backed pending question and
one approval, then verify exactly one remote alert per new action, foreground
suppression for the visible session, resolution cleanup, and cold/warm tap
routing. No production session, turn, token, or TestFlight action is authorized
for that check.

### Lifecycle acceptance correction (review round 1)

This correction closes three review findings without changing the server
writer:

- `clearResolvedAwaiting` now reads both delivered and pending notification
  requests before removing resolved `input` alerts. A pending-only request is
  therefore retired; active input and finished/failed requests remain.
- Work alerts carry `attention_ref`. Successful work responses now retain their
  top-level `workspace_id` even when `items` is empty, then clear only resolved
  question/approval alerts in that workspace. Other workspaces, session input,
  and non-input categories remain untouched. The foreground store and
  background refresh both pass that scope to the shared evaluator.
- Session receipts now combine the session id, awaiting kind, and the hub
  supplied `last_activity` (falling back to hub `updated_at`). This is source
  metadata rather than a client timestamp: an unchanged action is silent over
  reconnect/app-open, while a later source action on the same session alerts.
  Legacy id-only receipts are absorbed once during upgrade to avoid replay.

Focused round-1 result: `swift test --filter
'NotifierTests|NotificationRoutingTests|WorkAttentionTests|UATRecheckTests'`
passed **68/68**. The log is retained at
`/Users/arnab/scratch/drover-ios-981-worker/notification-lifecycle-round2-swift-test.log`.
The suite includes pending-only delivery coordination, workspace-scoped
empty-result cleanup through `WorkAttentionStore`, and same-session
distinct-action/replay coverage.

Physical/APNs limits remain unchanged: simulator fixtures do not establish a
device token, APNs delivery, Focus/Time Sensitive entitlement behavior, or
lock-screen cleanup. The precise remaining authorized check is a fixture-backed
question and approval on a provisioned sandbox iPhone with notification
permission, verifying one remote delivery per source action, pending and
delivered resolution cleanup, visible-session suppression, and cold/warm tap
routing—without creating a production session or turn.
