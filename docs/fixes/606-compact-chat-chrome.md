# Compact chat chrome at Accessibility XXXL (#606)

Base: `b17bde691b9b8fe724d85dd47da4c57b6ce22eaf` (GitHub main).
Regression: `b73d4abfb3081aed5e1b7f642326b8c24053a35f` (#601).

## Evidence and root cause

Read issue #606 through `gh issue view 606 -R arniesaha/drover`.
Inspected the Studio's r6 screenshots in `~/.drover/uat/2026-10-13-r6`:

- `r6-40-jump-pill-default.png`: content-sized translucent jump and status
  treatments at standard text size.
- `r6-41-ax-largest-scrolled.png`: a two-line jump capsule and large status
  metadata consume the transcript; the composer occupies a large bottom area.
- `r6-42-ax-largest-bottom.png`: the same three-line status expansion remains
  when following the transcript. Send remains reachable.

The referenced `artifacts/drover-v2/uat-r6-report.md` was unavailable in this
checkout, the UAT checkout, the primary checkout, and GitHub main. The issue
and original screenshots supplied the reproduction evidence. No original UAT
screenshots or private session data are copied into this commit.

The jump label used an unrestricted text HStack at every Dynamic Type size.
The status retained its timer, detail and wrapping FlowLayout metadata in the
pinned bottom inset. Intrinsic sizing therefore made these surfaces grow with
XXXL fonts. The composer already protected Send outside its input scroll view,
but allowed a 300-point surface. #601 tests checked visibility, width and
reachability without imposing height budgets or exercising timestamped status
telemetry alongside detached reading and preferences.

## Change

Standard text keeps the translucent label/count capsule, status telemetry and
one-row model/effort choices. Accessibility sizes use an arrow and unread dot
for jump, retaining the full spoken label and event count. The status shows a
wrapping phase title; tapping it opens scrollable details, and VoiceOver also
receives the telemetry as its value. Decorative status imagery no longer
competes with large titles for width. The accessibility composer surface is
bounded to 200 points, with reflowing preferences and drafts inside its scroll
view and attachment/Send actions outside it. Existing text-field focus and
send wiring are preserved.

## Validation

The new hosted SwiftUI layout tests cover 320-, 375- and 393-point widths,
large unread counts, timestamped activity with steps and long detail, long
error titles, and standard-size jump chrome. Substituting main's original
status and extracted original jump label makes all three test functions fail
with 16 assertions; the corrected views pass all nine parameter cases.

UI coverage includes detached running chat with preferences at XXXL in light
and dark appearance, height budgets, non-overlap, activity details, jumping
back to the tail, preference reflow, default one-row preferences and compact
status, and focused multiline typing with Send above the keyboard (#577).

Results on the dedicated iPhone 17 / iOS 27 simulator:

- `DroverTests/ChatChromeLayoutTests`: 3 test functions, 9 parameter cases,
  0 failures. Swift Testing reports function and parameter-case counts
  separately from XCTest's empty wrapper suite.
- Scoped `AccessibilityJourneyUITests` selection (8 methods) plus
  `TranscriptFollowingUITests` (1 method): 9 tests, 0 failures.
- After guarding completed turns against an empty details sheet, the
  accessibility active/completed status UI method passed again.
- Explicit `xcodebuild -scheme Drover -destination
  'generic/platform=iOS Simulator' build`: succeeded.
- Inspected the synthetic UI screenshots for detached XXXL light/dark and
  standard-size one-row preferences. No server behavior changed, so server
  tests were not needed. No full test suite was run.

Local validation logs: `/tmp/drover606-baseline.log`,
`/tmp/drover606-unit-final.log`, `/tmp/drover606-ui-final.log`,
`/tmp/drover606-status-final.log`, and `/tmp/drover606-build.log`.
