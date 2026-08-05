# Glass Composer UI Design

## Goal

Make Drover's structured prompt inputs feel like a floating iOS glass dock across chat and launch screens, matching the approved Claude Code-inspired direction while keeping Drover's existing navigation and transcript rendering intact.

## Scope

- Chat composer becomes a single hovering glass surface pinned above the safe area.
- Launch starting prompt uses the same glass input surface inside the form.
- Model and thinking controls move into the dock's control row.
- Image attachment controls and thumbnails remain supported.
- No backend behavior changes.

## Design

Create a shared SwiftUI prompt surface that owns the visual treatment: rounded dark material, subtle stroke, shadow, large placeholder text, attachment strip, and compact controls. Chat uses it as a bottom dock. Launch embeds it in the structured prompt section so the new-session screen shares the same interaction language without fighting `Form` layout.

## Constraints

- Keep controls mobile-first and compact.
- Preserve existing bindings and attachment downscaling behavior.
- Keep the current model and thinking menus.
- Do not add voice input behavior unless the app already supports it.
- Verify with Swift package tests and Xcode build.
