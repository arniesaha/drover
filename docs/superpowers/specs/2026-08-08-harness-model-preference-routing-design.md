# Harness Model Preference Routing Design

## Problem

Structured-session creation currently applies model and thinking preferences twice for per-turn harnesses. The daemon adds the preferences to the base command, then the Codex and Gemini drivers append them again when they spawn the initial turn. Codex rejects the duplicate `--model` argument and Gemini silently tolerates it.

Claude Code has a different lifecycle: one persistent process serves the whole session, so its model and effort must be selected when that process starts and cannot be changed by a later turn.

## Behavior

- New-session controls remain editable for Claude Code, Codex, and Gemini.
- Claude Code applies model and thinking effort once when its persistent process starts.
- An existing Claude Code session displays its selected model and effort as disabled controls. Turns do not submit model or effort overrides.
- Codex applies model and thinking effort once to every per-turn process, including the initial turn and resumed turns.
- Gemini applies model once to every per-turn process, including the initial turn. Gemini continues to omit thinking effort because its headless CLI has no corresponding option.
- Existing Codex and Gemini sessions keep editable preference controls so users can change the next turn's selection.

## Architecture

Preference ownership follows each harness process lifecycle. `apply_structured_preferences` is the startup-command boundary and applies preferences only for persistent Claude Code. Codex and Gemini receive clean base commands; their drivers add the preferences when building each turn's argv.

`HarnessRunPreferences` exposes whether an existing session supports preference changes. The chat composer uses that capability to disable Claude Code preference menus and to omit Claude Code overrides from turn requests. The launch surface remains unaffected.

## Error Handling

No new fallback is introduced. Invalid model names continue to be reported by the native CLI. The change only guarantees that Drover emits each supported preference at most once at the correct lifecycle boundary.

## Verification

- Regression tests assert that daemon startup preference routing changes only Claude Code commands.
- Codex driver tests assert one `--model` and one reasoning-effort override on initial and resumed turns.
- Gemini driver tests assert one `--model` on its turn argv.
- DroverKit tests assert Claude Code cannot change preferences in an existing session while Codex and Gemini can.
- Chat model tests assert Claude Code turn requests omit locked preferences.
- Focused Python and Swift suites run before the full relevant test suites.

## Scope

This change does not restart or fork Claude Code sessions to change models, change model suggestion lists, or alter authentication and session persistence behavior.
