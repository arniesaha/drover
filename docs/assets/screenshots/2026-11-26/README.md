# Native iOS screenshots: 2026-11-26 (UTC)

Actual simulator screenshots exported from retained Xcode UI-test attachments.
All three use **synthetic fixtures**. No images were generated, composited or
retouched. Reviewed full-size for layout, captions and accidental private data.
The images do not establish production AI availability, physical-device UAT,
animation quality from a still frame, or accepted autonomous outcomes.

## Captions and source

- **Home idle fixture** (`ios-home-idle-fixture.png`): one synthetic Fixture Mac,
  zero active sessions, current idle artwork. Source `acf179ce86cb33ae977905cb2c3aad06227e9cf3`,
  `AccessibilityJourneyUITests/testIdleTerminalMotionAtDefaultTextSize()`,
  captured 2026-11-26 01:14 UTC. Original attachment
  `B74A800D-B265-42EE-9E5B-9D74373FF9C7.png`.
- **Configuration findings fixture** (`ios-configuration-findings-fixture.png`):
  model judgment is labelled as a suggestion, not a confirmed fault. Source
  `f997dab593615ca9b9e89494d98602aa36b79113`,
  `ConfigurationReviewUITests/testAIReviewDetailBackAndRootTabReturn()`,
  captured 2026-11-26 00:35 UTC. Original attachment
  `67A9F858-C4C4-49D1-AC30-5500BB7ABD2A.png`.
- **Configuration setup fixture** (`ios-configuration-setup-fixture.png`):
  optional AI review; selected files and model availability Not verified,
  last review Not reported. Same configuration source and capture session.
  Original attachment `AB634664-149A-46EC-A3B8-8E828041C82C.png`.

These are the latest retained, source-attributable captures selected for this
refresh, not a claim that they were recaptured from a new build today.
`git diff acf179ce..7133442d -- apps/drover/Drover` is empty. Between configuration
source `f997dab5` and baseline `7133442d`, the only app-source change is
`Screens/Sessions/SessionsView.swift` (idle artwork animation). Configuration
review source therefore matches the baseline too. Test-only changes after the
captures do not make these a new acceptance run.

Simulator: iOS 27.0, device `3E61FFDF-A39E-4C59-AE7E-ECE8A22E3CCC`.
[Machine-readable provenance and SHA-256 hashes](provenance.json) permit reuse
without losing attribution. Keep the fixture caption adjacent to reused images.
The earlier unversioned screenshot set remains historical; the README uses
this dated set so it cannot silently imply the old fleet layout is current.
