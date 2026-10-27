# Drover 1.0 App Store submission package and review guide

Submission package prepared for Drover 1.0 App Store candidate release.
Last reviewed: 2026-10-26.

This document compiles the required App Store Connect metadata, privacy
declarations, reviewer instructions for the built-in offline demo mode, support
destinations, and screenshot verification for supported device classes.

> [!IMPORTANT]
> **Approval Gate**: In accordance with project policy and issue #733 acceptance
> criteria, binary upload to App Store Connect and final release submission
> require explicit human approval from Arnab.

---

## 1. App Store Connect metadata

### Version and identity

| Field | Value | Constraints & notes |
| --- | --- | --- |
| **App Name** | `Drover` | Primary display name (≤ 30 characters). |
| **Subtitle** | `Command & context for agents` | Functional description (28 chars, ≤ 30 limit). |
| **Primary Language** | `English (U.S.)` | Default localization. |
| **Bundle ID** | `com.arnab.drover` | Matches `project.yml` and distribution provisioning profile. |
| **SKU** | `DROVER-IOS-100` | Unique developer SKU. |
| **Primary Category** | `Developer Tools` | Matches intended user persona (engineers operating local/remote agent fleets). |
| **Secondary Category** | `Productivity` | Focus on session management and operator handoffs. |
| **Age Rating** | `4+` | Developer tool; no user-generated public content or third-party web browsing. |
| **Price Tier** | `Free` (0 USD) | Free tool; self-hosted software. |
| **Copyright** | `© 2026 Arnab Saha` | Current release year and author. |
| **Marketing Version** | `1.0.0` | Initial public App Store release. |
| **Build Number** | Pinned by release pipeline | Automated by CI candidate workflow. |

### URLs

| Destination | URL | Verification |
| --- | --- | --- |
| **Support URL** | `https://drover.fyi/support` | Live page on drover.fyi; describes setup, diagnostics, pairing, and issue reporting. |
| **Marketing URL** | `https://drover.fyi` | Live product site; architecture overview, documentation, and features. |
| **Privacy Policy URL** | `https://drover.fyi/privacy` | Live page on drover.fyi; comprehensive data flow, retention, and self-hosted privacy policy. |

### Promotional text (up to 170 characters)

```text
Supervise your personal coding-agent fleet from your phone. Inspect live transcripts, grant execution permissions, review diffs, and track provider quota on the go.
```

### Keywords (up to 100 characters)

```text
developer tools,coding agents,terminal,claude code,codex,self-hosted,devops,llm,pair programming
```
*(96 characters including commas).*

### Description (up to 4,000 characters)

```text
Drover is the local-first command, context, and supervision client for your personal fleet of autonomous coding agents.

Whether your agents run on a Mac Studio, a dedicated Linux workstation, or a remote build box, Drover brings visibility and control to your pocket:

• FLEET AT A GLANCE: Monitor running, waiting, and completed sessions across all your registered hosts from a single unified Home screen.
• LIVE STREAMING SESSIONS: Inspect real-time agent output, streaming tool calls, file diffs, and terminal execution as work unfolds.
• DECISION & APPROVAL GATES: Safely keep humans in the loop. Review and authorize shell commands, database migrations, or code commits with a single tap.
• ACTIONABLE INBOX: Never miss an agent blocked on input. Dedicated Attention and Insights queues ensure decisions are surfaced immediately.
• USAGE & CAPACITY ANALYTICS: Track token consumption, project activity, and model provider quota across Anthropic Claude, OpenAI Codex, and local models.
• PRIVATE & SELF-HOSTED: Connects directly to your own Drover hub over localhost, a private LAN, or Tailscale. No third-party relays, no tracking, and no cloud accounts required.

Reviewers and curious developers can explore the complete application immediately without a running server using the built-in offline Demo Mode.
```

### What's new in this version (1.0.0)

```text
Initial public release of Drover for iOS:
• Full support for live streaming agent transcripts and interactive session composer.
• One-tap human authorization gates for tool approvals and migrations.
• Unified 1.0 design system across Home, Inbox, Analytics, Accounts, and Settings.
• Built-in offline demo mode to test all features without server setup.
• Push notification routing for sessions requiring operator input.
```

---

## 2. App Privacy Details (App Store Connect questionnaire)

The iOS binary contains an embedded privacy manifest (`Drover/PrivacyInfo.xcprivacy`) and connects exclusively to the operator's configured server. The app does not include advertising SDKs, third-party trackers, or analytics packages.

### Summary table for App Store Connect entry

| Data type | Collected? | Linked to user? | Used for tracking? | Purposes |
| --- | --- | --- | --- | --- |
| **Name** | Yes | Yes | No | **App Functionality** (device name sent to operator's hub during QR pairing to identify phone). |
| **Device ID** | Yes | Yes | No | **App Functionality** (APNs device token registered with hub for alert notifications). |
| **User Content** | Yes | Yes | No | **App Functionality** (prompts, terminal input, and text pasted to terminal). |
| **Photos or Videos** | Yes | Yes | No | **App Functionality** (images explicitly picked by the user via system `PhotosPicker` to attach to agent turns). |
| **Other Data Types** | Yes | Yes | No | **App Functionality** (hub URL, pairing token, Keychain API token, session IDs, and model preferences). |

- **Tracking Declaration**: Does this app or any third-party SDK collect data used for tracking? **NO**.
- **Required Reason APIs**: `UserDefaults` declared with reason code `CA92.1` (solely for persisting app preferences, display options, and cached model metadata).

---

## 3. App Review information & demo-mode instructions

Paste the following verbatim into the **App Review Information** section in App Store Connect:

```text
DEMO MODE INSTRUCTIONS FOR REVIEWERS:

Drover is designed as a client for self-hosted developer tools running on a personal private network or workstation. To allow the App Review team to thoroughly test all native screens and functionality immediately without requiring a local server setup or computer, a complete, self-contained offline Demo Mode is built directly into this build.

Steps to access and review all features:

1. Launch the app. On the initial welcome screen, tap the button labeled:
   "Try a demo — no computer needed" (sparkles icon).
2. The app will immediately launch into Demo Mode. A visual "DEMO" badge appears in the header.
3. HOME SCREEN (Fleet Overview):
   - Review active synthetic hosts ("Mac Studio", "Linux Workstation", "CI Build Box").
   - Notice running sessions across harnesses (Claude Code, Codex).
4. LIVE STREAMING CHAT & COMPOSER:
   - Tap the session "Streaming checkout flow refactor".
   - Review live agent messages, tool executions, and file diffs.
   - In the composer field at the bottom, type any question (e.g., "Check unit tests") and tap the send button. The simulated offline engine will generate an instant reply.
   - Tap the back button to return to the fleet.
5. APPROVAL & DECISION GATE (Inbox):
   - Notice the "1 item needs your attention" callout on Home or tap the "Inbox" tab at the bottom.
   - In the "Needs you" queue, tap the pending approval session: "Database migration for user sessions table".
   - The review gate displays a pending SQL migration command. Tap the "Allow once" button.
   - Notice the approval is recorded and execution continues with confirmation.
   - Tap "Inbox" in the top bar to return.
6. ANALYTICS:
   - Tap the "Analytics" tab at the bottom.
   - Review 7-day and 30-day token usage graphs, dimension breakdowns (by host, harness, and repo), and cost summaries.
7. ACCOUNTS:
   - Tap the "Accounts" tab at the bottom.
   - Review provider capacity meters and health for configured models.
8. SETTINGS & CLEAN EXIT:
   - Tap the "Settings" tab at the bottom.
   - Test the local notification preview with "Preview Sample Notification".
   - Toggle theme between light and dark modes.
   - Tap "Exit Demo" to cleanly exit back to the initial welcome screen. No Keychain items, tokens, or network requests are persisted.

All demo data operates strictly in-process with a synthetic URLProtocol (demo.drover.invalid) and does not contact external servers.

Reviewer Contact:
Arnab Saha (arniesaha@gmail.com)
```

---

## 4. Prepared App Store screenshots

Screenshots were captured from automated XCTest runs (`DemoModeUITests`) against native iOS and iPadOS simulators. Every asset matches Apple's exact dimension requirements.

### Screenshot inventory & supported device classes

Assets are archived in the repository under:
- `docs/assets/app-store/screenshots/iphone-6.9/` (iPhone 16/17/18 Pro Max, 6.9" display)
- `docs/assets/app-store/screenshots/ipad-13/` (iPad Pro 13-inch M4/M5, 13" display)

| Order | Asset Name | Screen / Journey Step | iPhone 6.9" Size | iPad 13" Size | Purpose & Content |
|---|---|---|---|---|---|
| **01** | `01_Onboarding_Welcome.png` | Welcome & First Run | 1320 × 2868 | 2064 × 2752 | Clean welcome screen with "Set Up Drover" and "Try a demo — no computer needed" CTAs. |
| **02** | `02_Fleet_Home_Demo.png` | Home / Fleet Overview | 1320 × 2868 | 2064 × 2752 | Live fleet dashboard showing host status, running agents, and active session cards with DEMO badge. |
| **03** | `03_Session_Streaming.png` | Live Chat & Streaming | 1320 × 2868 | 2064 × 2752 | Real-time agent transcript with syntax-highlighted code, tool execution details, and prompt composer. |
| **04** | `04_Session_ApprovalPrompt.png` | Approval Prompt | 1320 × 2868 | 2064 × 2752 | Human-in-the-loop security authorization gate displaying pending bash/migration execution and "Allow once" CTA. |
| **05** | `05_Session_Approved.png` | Approved Confirmation | 1320 × 2868 | 2064 × 2752 | Post-approval state confirming migration execution and next steps. |
| **06** | `06_Session_DiffArtifacts.png` | Diff & Artifact Viewer | 1320 × 2868 | 2064 × 2752 | Structured file diff view and floating artifact badges for inspection. |
| **07** | `07_Analytics_Overview.png` | Analytics Dashboard | 1320 × 2868 | 2064 × 2752 | Token consumption over time, ranked host/model utilization charts, and activity summaries. |
| **08** | `08_Settings_DemoMode.png` | Settings & Privacy | 1320 × 2868 | 2064 × 2752 | Clean preferences interface showing connection status, notification controls, and docs links. |
| **09** | `09_Returned_To_Onboarding.png` | Clean Exit to Onboarding | 1320 × 2868 | 2064 × 2752 | Verification of safe return to initial state with zero leftover state. |

### Visual review and accuracy checklist

- [x] **No Debug or Test Chrome**: Screenshots show no "Reconnecting" banners, fixture overlays, or test execution artifacts.
- [x] **Accurate Design System**: Headers use the canonical `ScreenHeader` typography and consistent 1.0 IA tabs (Home, Inbox, Analytics, Accounts, Settings).
- [x] **No Truncation**: Text strings and button labels are clean, legible, and unclipped.
- [x] **Both Required Device Classes**: Full 9-screen sequence available for iPhone 6.9" (1320 × 2868) and iPad 13" (2064 × 2752).

---

## 5. Security & infrastructure audit

Submission assets and metadata were audited against `scripts/check_public_release.py`:

```sh
python3 scripts/check_public_release.py
# Public release audit: 0 finding(s)
```

### Confidentiality verifications

1. **No Private IP Addresses**:
   No RFC1918 addresses (`192.168.x.x`, `10.x.x.x`, `172.16.x.x`) appear in text or visual assets.
2. **No Private Tailnet Hostnames**:
   No internal MagicDNS or `*.ts.net` domain names appear in screenshots or copy.
3. **No Personal File Paths**:
   No developer workstation filesystem paths (`/Users/arnab/...` or `/home/...`) appear in metadata or assets; synthetic paths use standard virtual roots (`/projects/acme-web-app`).
4. **No Secrets or Credentials**:
   No real bearer tokens, APNs keys, private certificates, or production API tokens are present. Synthetic mock tokens (`demo-offline-token-no-secret`) and reserved domains (`demo.drover.invalid`) are used exclusively.

---

## 6. Pre-upload checklist & release authority

- [x] App Store metadata complete and reviewed.
- [x] Privacy manifest (`Drover/PrivacyInfo.xcprivacy`) aligns with App Store Connect questionnaire.
- [x] Support destination verified live (`https://drover.fyi/support`).
- [x] Privacy policy destination verified live (`https://drover.fyi/privacy`).
- [x] App Review demo mode instructions verified end-to-end via automated UI test harness.
- [x] 6.9" iPhone and 13" iPad screenshots captured at native resolutions and audited.
- [x] Security audit clean (0 findings).
- [ ] **Arnab Approval**: Required prior to dispatching `.github/workflows/ios-distribution.yml` or submitting candidate in App Store Connect.
