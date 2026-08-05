# Glass Composer UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shared glass prompt surface for Drover's chat and launch inputs.

**Architecture:** Extract the visual input dock into a shared SwiftUI view used by `Composer` and `LaunchView`. Keep networking and model state unchanged; only relocate existing controls and attachment UI into the new surface.

**Tech Stack:** SwiftUI, PhotosUI, NexusKit, XcodeGen-generated iOS project.

## Global Constraints

- Use existing `TurnAttachment`, `ImageDownscaler`, `HarnessPreferenceControls`, model bindings, and thinking bindings.
- Do not add voice input behavior.
- Keep all user-visible controls inside the glass surface.
- Verify with `swift test` and `xcodebuild`.

---

### Task 1: Shared Glass Prompt Surface

**Files:**
- Create: `apps/drover/Drover/Screens/Shared/GlassPromptSurface.swift`
- Modify: `apps/drover/Drover/Screens/Shared/HarnessPreferenceControls.swift`

**Interfaces:**
- Consumes: `TurnAttachment`, model/thinking bindings, and PhotosUI attachment button supplied by callers.
- Produces: `GlassPromptSurface`, a reusable SwiftUI view for chat and launch prompt input.

- [ ] **Step 1: Implement `GlassPromptSurface`**

Create a view with `@Binding var text`, `@Binding var attachments`, `@Binding var selectedModel`, `@Binding var thinkingEffort`, `let harness`, `let placeholder`, `let sendSystemImage`, `let isSending`, `let canSend`, `let showsSendButton`, `let attachmentButton`, and `let onSend`.

- [ ] **Step 2: Restyle preference chips**

Keep the menus but make chips compact enough for the bottom dock: icon plus short label, plain button style, capsule material background.

### Task 2: Chat Composer Dock

**Files:**
- Modify: `apps/drover/Drover/Screens/Chat/Composer.swift`

**Interfaces:**
- Consumes: `GlassPromptSurface`.
- Produces: a floating chat composer with image picker, model picker, thinking picker, text input, and send button in one surface.

- [ ] **Step 1: Replace the current row layout**

Move the PhotosPicker label into the shared surface attachment slot. Keep image loading and send behavior unchanged.

- [ ] **Step 2: Preserve accessibility identifiers**

Keep `composer-attach`, `composer-send`, and `composer-attachment`.

### Task 3: Launch Starting Prompt

**Files:**
- Modify: `apps/drover/Drover/Screens/Launch/LaunchView.swift`

**Interfaces:**
- Consumes: `GlassPromptSurface`.
- Produces: launch prompt section with matching glass input, attachment controls, model picker, and thinking picker.

- [ ] **Step 1: Replace separate controls/text editor/attach button**

Use `GlassPromptSurface` inside the structured `Starting prompt` section. Keep launch button separate.

- [ ] **Step 2: Preserve accessibility identifiers**

Keep `launch-attach` and `launch-attachment`.

### Task 4: Verification

**Files:**
- Test: `apps/drover/NexusKit`
- Test: `apps/drover/Drover.xcodeproj`

- [ ] **Step 1: Regenerate the Xcode project**

Run: `cd apps/drover && xcodegen generate`

- [ ] **Step 2: Run Swift package tests**

Run: `cd apps/drover/NexusKit && swift test`

- [ ] **Step 3: Build the iOS app**

Run: `cd apps/drover && xcodebuild -project Drover.xcodeproj -scheme Drover -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build -quiet`
