# iOS chat navigation & attention notifications — design

Date: 2026-07-22
Scope: Drover iOS app (`apps/drover`), three UX problems reported from phone use.

## Problems

1. **No way to jump to the newest message.** Opening a working or needs-you
   session drops you into the transcript with no scroll-to-bottom affordance.
2. **Scrolling mid-stream hangs/lags.** The coalesced auto-scroll in
   `ChatView` fires unconditionally on every appended message, so while the
   user is reading earlier messages the list keeps getting yanked back to the
   bottom. Each unanimated jump de/re-materializes LazyVStack rows under the
   finger — perceived as hang/lag. Thinking turns also render as a stack of
   identical stock `DisclosureGroup("Thinking…")` rows, one per thinking
   block, which is noisy and visually flat.
3. **Attention notifications only appear with the app open.** The full
   background path exists (`BGAppRefreshTask` → `AttentionWatcher` →
   `LocalNotifier`, Info.plist keys declared) but delivery when the app is
   closed is unreliable in practice.

## Design

### 1 + 2a. Pinned auto-scroll with a scroll-to-bottom button (`ChatView`)

Track whether the user is *pinned* to the bottom using iOS 18's
`onScrollGeometryChange` (deployment target is already 18.0): pinned means
`contentOffset.y + containerSize.height >= contentSize.height - 80pt`.

- **Auto-scroll only while pinned.** The existing 120 ms coalesced,
  unanimated scroll behavior is kept, but gated on `isPinnedToBottom`. This
  is the lag fix: the stream never fights the user's finger, and rows the
  user is reading stay put.
- **Floating scroll-to-bottom button** appears (bottom-trailing overlay over
  the transcript) whenever the user is not pinned. Tapping it scrolls to the
  newest message and re-engages pinning. A single user-initiated scroll may
  animate — the animation-storm problem only applies to per-message scrolls.
- Opening a session lands pinned (initial state true), so a fresh open
  follows the stream from the start, matching today's behavior.

Alternatives considered: (a) a toolbar button — rejected, too far from the
thumb and invisible when needed; (b) `defaultScrollAnchor(.bottom)` —
insufficient alone, doesn't solve "resume following after scrolling up".

### 2b. Thinking-turn rendering

- **Group consecutive thinking messages into one block.** A pure
  `TranscriptItem.group(_:)` helper in NexusKit folds runs of consecutive
  `assistant_output` messages whose payload has `thinking == true` into one
  item; everything else passes through 1:1. Unit-tested (identity stability,
  interleaving with tool calls, run boundaries).
- **Calmer visual.** One collapsed row per thinking run: brain SF Symbol +
  "Thought for a bit" caption in secondary italic, chevron disclosure.
  Expanded: the run's text in italic secondary `callout` behind a 2 pt
  leading accent bar, visually receding relative to real output bubbles.
- Row identity for the grouped item is the run's **first** message id, so an
  in-progress run keeps a stable SwiftUI identity as later chunks join it
  (no row teardown mid-stream — part of the lag fix).

### 3. Background attention notifications

Everything client-side that can improve delivery, plus an honest ceiling:

- **Time-sensitive interruption level** on attention notifications, with the
  matching `com.apple.developer.usernotifications.time-sensitive`
  entitlement — breaks through Focus/lock-screen deferral, a common reason
  "no notification until I open the app".
- **Always keep a refresh request pending:** schedule at launch as well as on
  background transition (today only the latter), so a cold relaunch that
  never backgrounds still has one queued.
- Unchanged: the BGTask/foreground paths share one persisted seen-set, so no
  double alerts.

**Ceiling (documented, not built now):** `BGAppRefreshTask` is opportunistic
— iOS decides when (often 15 min to hours) and never runs it for a
force-quit app. Guaranteed instant delivery with the app closed requires
server-side push (APNs key + device-token registry + push on attention
transitions in the drover server). That is a separate server feature;
follow-up candidate.

## Testing

- NexusKit unit tests for `TranscriptItem.group(_:)` and notifier
  time-sensitive content (SpyNotifier already exists).
- iOS build via xcodebuild; behavior verified on simulator/device per the
  usual E2E flow (see ios-app-validated memory).
