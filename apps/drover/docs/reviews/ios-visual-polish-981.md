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
