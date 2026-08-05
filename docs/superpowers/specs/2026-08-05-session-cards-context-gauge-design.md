# Session Cards And Context Gauge Design

## Goal

Make the sessions screen distinguish concurrent agent sessions at a glance and stop showing misleading Codex context usage.

## Findings

- The current sessions snapshot exposes only session id, host, harness, cwd, status, awaiting state, and last activity. The iOS app therefore falls back to cwd as the title.
- Live snapshot timestamps are emitted as naive local-time strings, while iOS parses naive server timestamps as UTC. That is why a current Mac Mini session can render as roughly 7 hours old.
- Codex `turn.completed` usage is cumulative. In the live session it climbed from 7.9M to 18.1M input tokens and carried no context-window value, so it should not be rendered as current context occupancy.

## Design

The server snapshot will expose richer, safe session metadata:

- `started_at`: serialized with a timezone offset when available.
- `updated_at`, `last_activity`, `ended_at`, and host `last_seen_at`: serialized consistently with a timezone offset.
- `preview`: derived from the newest user or assistant event `content_preview`, with status/tool noise ignored.

The iOS data model will decode these fields while preserving compatibility with older servers.

The sessions screen will move from host-section rows to a flat active-session card feed inspired by CloudGuard:

- Active cards show a preview/title, trailing activity age, harness, status, host, and cwd.
- Finished sessions stay visually separate and collapsed by default.
- Host identity moves into each card rather than being the primary grouping.
- The new-session action becomes a floating glass button at bottom-left and opens the existing launch flow in a modal sheet.

The context gauge will only render values that are known to represent per-call prompt context. Codex cumulative `turn.completed` usage without a context window will produce no gauge rather than `ctx 18.1M`.

## Testing

- Python tests cover timezone-aware harness snapshot serialization and preview derivation.
- Swift model tests cover decoding the new fields.
- Swift context tests cover hiding cumulative Codex usage while keeping Claude per-call usage behavior.
- App build verification covers the redesigned SwiftUI screen.
