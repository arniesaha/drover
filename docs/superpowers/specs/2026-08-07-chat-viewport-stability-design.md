# Chat Viewport Stability

## Problem

Long structured sessions render their newest 200 raw messages as a much
smaller set of folded transcript rows. The current chat screen still uses a
`LazyVStack` and auto-scrolls to the row containing the newest raw event.
Together those choices make the viewport visibly reset:

- A tool result can arrive after status rows but fold into its earlier tool
  action. Auto-scroll then targets that earlier card, jumps upward, and jumps
  down again when the next event creates or updates the visual tail.
- Sending clears a multi-line composer while the keyboard remains open. The
  resulting safe-area and row-height changes can make `LazyVStack`
  de-materialize visible rows briefly, so the transcript appears blank before
  rebuilding.
- Reopening a session performs the same initial lazy layout and can exhibit
  the same temporary blank state even though history loaded successfully.

The Mac Mini simulator reproduced the Send transition against session
`harness-327154f5-92a2-40e6-8e24-ee25d37a4204`. Its live event tail also
contained the decisive ordering: a tool action, intervening status rows, then
the tool result that folds back into the earlier action card.

## Goals

- Sending a reply must not blank the transcript or move the pinned viewport
  upward.
- Updates to earlier folded rows must occur in place without becoming an
  auto-scroll destination.
- A pinned conversation follows the visual bottom as new rows arrive.
- A user who scrolls upward remains in control and is not pulled to the tail.
- Returning to a chat paints its bounded newest page without a transient empty
  transcript.
- Explicit older-page loading retains its stable-anchor behavior.

## Design

### Eagerly render the bounded tail

Replace the transcript's `LazyVStack` with `VStack`. Cold open is already
bounded to 200 raw messages, which fold to far fewer displayed rows (29 in the
simulator reproduction). Eagerly materializing that bounded set avoids lazy
row eviction during composer, keyboard, and navigation layout transitions.

Users may explicitly load older pages. That can grow the eager stack, but the
growth is deliberate and incremental; initial and routine live-session cost
remains bounded. This trade-off favors a stable active conversation over
virtualization that is not needed at the normal row count.

### Follow the visual tail

Add a model-level visual-tail row identifier derived from the last grouped
`TranscriptItem`. Chat auto-scroll and the down-arrow target this identifier.
Do not use the row containing the newest raw event: a result may update an
earlier folded step while later status rows remain at the visual bottom.

The existing raw-event-to-row resolver remains for explicit older-page anchor
restoration, where following a particular reading row is intentional.

### Preserve interaction rules

- Keep the keyboard open after Send for rapid follow-up turns.
- Keep auto-scroll unanimated and coalesced.
- Keep the pinned/unpinned gesture rules and cancellation generations.
- Composer clearing may change its height, but the eager transcript remains
  materialized and the pinned target remains the visual tail.
- No server, pagination, database, or message-folding behavior changes.

## Failure Handling

Network and send errors keep their existing behavior. A failed send retains
composer text; a successful send clears it. This change only alters rendering
and scroll targeting, so it introduces no new retry or persistence state.

## Verification

### Unit regression

Build a transcript where a tool action is followed by a status row and then
its tool result. Assert:

- the newest raw event resolves to the earlier tool row;
- the visual-tail row remains the later status row;
- adding a genuinely new row advances the visual-tail identifier.

The test must fail under the current latest-event target before production code
changes.

### Simulator regression

Use the existing Mac Mini iPhone simulator and live-server debug override to:

1. Open the known long structured session.
2. Confirm transcript rows are visible.
3. Enter a wrapping reply and press Send.
4. Sample screenshots and visible transcript elements through the composer
   collapse and incoming tool/status sequence.
5. Navigate back and reopen the same session.

Acceptance requires no empty transcript frame, no upward jump when a tool
result attaches to an earlier card, and a visible transcript after reopen.

### Release gates

- Full `DroverKit` test suite.
- Targeted UI regression on the simulator.
- Signed Release build for the physical iPhone.
- On-device confirmation in the same long session before merging locally.

## Non-goals

- Loading the entire session automatically.
- Changing transcript folding or tool/status presentation.
- Redesigning the composer or dismissing the keyboard after Send.
- Server-side deployment or database changes.
