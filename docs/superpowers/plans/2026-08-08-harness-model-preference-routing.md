# Harness Model Preference Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route model and thinking preferences exactly once for every structured harness and lock those controls after a Claude Code session starts.

**Architecture:** The daemon owns startup flags only for persistent Claude Code. Codex and Gemini retain clean base commands and let their per-turn drivers add preferences. DroverKit exposes whether an existing session can change preferences; both request construction and the chat controls consume that capability.

**Tech Stack:** Python 3.11+, pytest, Swift 6, Swift Testing, SwiftUI

## Global Constraints

- New-session preference controls remain editable for Claude Code, Codex, and Gemini.
- Existing Claude Code sessions cannot change or submit model and thinking overrides.
- Existing Codex and Gemini sessions keep per-turn model controls; Codex also keeps per-turn thinking effort.
- Do not change model suggestions, authentication, persistence, or native CLI error handling.

---

### Task 1: Route Native CLI Preferences by Process Lifecycle

**Files:**
- Modify: `tests/test_harness_daemon.py`
- Modify: `tests/test_structured_codex.py`
- Modify: `tests/test_structured_gemini.py`
- Modify: `src/drover/server/harness/daemon.py`

**Interfaces:**
- Consumes: `apply_structured_preferences(command, harness, model, thinking_effort)` and driver `_argv_for(...)` methods.
- Produces: Claude-only startup preference routing and exactly-once Codex/Gemini turn argv.

- [ ] **Step 1: Write failing daemon routing expectations**

Change `test_structured_command_preferences_map_to_cli_flags` so Claude still expects `--model` and `--effort`, while Codex and Gemini expect their original clean commands:

```python
assert apply_structured_preferences(
    ["codex"], harness="codex", model="gpt-5.6-sol", thinking_effort="high"
) == ["codex"]
assert apply_structured_preferences(
    ["gemini"], harness="gemini", model="gemini-2.5-pro", thinking_effort="high"
) == ["gemini"]
```

- [ ] **Step 2: Write failing per-turn argv regressions**

Add literal assertions that a Codex initial and resumed argv each contain exactly one `--model` and one model-reasoning config, and a Gemini argv contains exactly one `--model`:

```python
assert argv.count("--model") == 1
assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
assert argv.count('model_reasoning_effort="high"') == 1
```

Use the analogous literal `gemini-2.5-pro` assertion for Gemini.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
uv run --extra dev pytest -q \
  tests/test_harness_daemon.py::test_structured_command_preferences_map_to_cli_flags \
  tests/test_structured_codex.py \
  tests/test_structured_gemini.py
```

Expected: daemon routing assertions fail because Codex and Gemini commands still receive preferences.

- [ ] **Step 4: Implement lifecycle routing**

Restrict `apply_structured_preferences` to Claude Code startup flags:

```python
if harness != "claude-code":
    return preferred
if model:
    preferred.extend(["--model", model])
if thinking_effort:
    preferred.extend(["--effort", thinking_effort])
```

Keep Codex and Gemini driver argv construction unchanged; after the daemon correction, each driver becomes the single preference owner for every spawned turn.

- [ ] **Step 5: Run focused Python tests and verify GREEN**

Run the command from Step 3. Expected: all selected tests pass.

- [ ] **Step 6: Commit the server fix**

```bash
git add src/drover/server/harness/daemon.py tests/test_harness_daemon.py \
  tests/test_structured_codex.py tests/test_structured_gemini.py
git commit -m "fix(harness): route model preferences once"
```

---

### Task 2: Lock Existing Claude Session Preferences

**Files:**
- Create: `apps/drover/DroverKit/Tests/DroverKitTests/HarnessRunPreferencesTests.swift`
- Modify: `apps/drover/DroverKit/Tests/DroverKitTests/ChatModelTests.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/HarnessRunPreferences.swift`
- Modify: `apps/drover/DroverKit/Sources/DroverKit/ChatModel.swift`
- Modify: `apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift`
- Modify: `apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift`
- Modify: `apps/drover/Drover/Screens/Chat/Composer.swift`

**Interfaces:**
- Produces: `HarnessRunPreferences.canChangeInExistingSession(_ harness: String) -> Bool`.
- Consumes: the capability in `ChatModel` request construction and the chat composer UI.

- [ ] **Step 1: Write failing capability and request tests**

Add literal capability expectations:

```swift
#expect(HarnessRunPreferences.canChangeInExistingSession("claude-code") == false)
#expect(HarnessRunPreferences.canChangeInExistingSession("codex") == true)
#expect(HarnessRunPreferences.canChangeInExistingSession("gemini") == true)
```

Add a `ChatModelTests` request-capture test that selects a Claude model and effort, sends a turn, and asserts the JSON body contains neither `model` nor `thinking_effort`. Retain the existing Codex test proving both keys are sent.

- [ ] **Step 2: Run focused Swift tests and verify RED**

Run:

```bash
swift test --package-path apps/drover/DroverKit --filter HarnessRunPreferencesTests
swift test --package-path apps/drover/DroverKit --filter sendTurnOmitsLockedClaudePreferences
```

Expected: compilation or assertion failure because the capability and omission do not exist.

- [ ] **Step 3: Implement the capability and request lock**

Add:

```swift
public static func canChangeInExistingSession(_ harness: String) -> Bool {
    harness != "claude-code"
}
```

In both normal and queued turn sends, derive model and effort only when this method returns true. This ensures disabled UI cannot be bypassed by stale bindings.

- [ ] **Step 4: Disable existing Claude session controls**

Add an `isEditable` input to `HarnessPreferenceControls` and apply `.disabled(!isEditable)` plus a locked visual/accessibility state. Thread an `arePreferencesEditable` value through `GlassPromptSurface`, defaulting to `true` for the launch sheet. In `Composer`, pass `HarnessRunPreferences.canChangeInExistingSession(harness)`.

- [ ] **Step 5: Run focused Swift tests and verify GREEN**

Run the commands from Step 2. Expected: both pass.

- [ ] **Step 6: Commit the app fix**

```bash
git add apps/drover/DroverKit apps/drover/Drover/Screens
git commit -m "fix(ios): lock active Claude run preferences"
```

---

### Task 3: Verify the Cross-Harness Fix

**Files:**
- Verify only; no planned production changes.

**Interfaces:**
- Consumes: the server routing and app lock delivered by Tasks 1 and 2.
- Produces: fresh test and build evidence.

- [ ] **Step 1: Run all relevant Python tests**

```bash
uv run --extra dev pytest -q \
  tests/test_harness_daemon.py \
  tests/test_structured_claude.py \
  tests/test_structured_codex.py \
  tests/test_structured_gemini.py \
  tests/test_structured_manager.py
```

- [ ] **Step 2: Run the full DroverKit suite**

```bash
swift test --package-path apps/drover/DroverKit
```

- [ ] **Step 3: Build the iOS app**

Regenerate the Xcode project if required by repository state, then run a simulator build with the checked-in project and scheme.

- [ ] **Step 4: Validate actual argv shapes**

Use the repository environment to print constructed Claude, Codex, and Gemini argv. Confirm Claude contains one startup preference set, Codex contains one per-turn set, and Gemini contains one per-turn model.

- [ ] **Step 5: Check the patch**

```bash
git diff --check
git status --short
git log -3 --oneline
```

Expected: no whitespace errors; only planned commits are ahead of the starting branch.
