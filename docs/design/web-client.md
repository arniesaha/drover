# Web client

Status: accepted direction, implementation proposed. Approved by Arnab on
2026-10-18 at 23:32 PT. This document records the approved product decisions
and proposes the implementation gates; it does not start P0.

## Problem

Drover's iOS app is the personal fleet client. The hub's existing HTML console
offers useful pieces, but does not provide the same navigation, transcript,
attention, analytics and account experience on a laptop. A browser client must
work with the user's existing hub without creating an account service, moving
session data to Drover infrastructure, or making a private hub public.

The implementation baseline is `origin/main` at `1ee1a506` (v0.6.24). The
screen map below comes from the Swift implementation, not a proposed new API.

## Decisions

- **One static SPA, two entry points.** Serve it from the hub at `/ui` and
  from `https://drover.fyi/web`. Both browsers call the user's hub directly.
  There is no Drover-run backend, API proxy, relay, SSR server or user-data
  service at drover.fyi. The hosted site delivers files and aggregate usage
  counters only. Hub payloads and credentials never go there in normal operation.
- **Replace the client console incrementally; retain the Observatory.** Move
  the current `/ui` Observatory to `/ui/observatory`, keeping its operator
  authentication. Existing harness/history/terminal pages remain during P0.
  Preserve their links until the equivalent SPA views pass their gates.
- **Pair like iOS.** Hub address plus a device pairing code redeems
  `POST /auth/pair` with `{"code": "…", "device_name": "Web – <browser>"}`.
  Require the returned scope to be `device`. Scope is determined by the code,
  not a client-supplied scope field. Store the device token in that browser
  origin; label it using a coarse browser name, without a fingerprint.
  Credentials remain individually revocable at the hub.
- **No OAuth or Drover accounts.** Provider CLI sign-in remains a hub feature,
  distinct from Drover identity. Revisit identity only if a hosted relay needs
  ownership/recovery, or a team product needs invitations, roles and shared
  authorization. Neither trigger justifies adding accounts to this client now.
- **Use the iOS API.** Do not introduce web projections or parallel business
  rules. The sole proposed API addition is a narrow browser WebSocket ticket
  exchange, justified under Streaming. Static asset routes and CORS are
  transport changes, not alternate fleet APIs.
- **Private connectivity.** Keep hub access within the LAN/tailnet. Recommend
  Tailscale Serve HTTPS at `https://<host>.<tailnet>.ts.net`, forwarding to the
  hub's loopback HTTP listener. No port forwarding or public Funnel. Serve
  provisions TLS and remains tailnet-only; tailnet access rules still apply.
  See [Serve](https://tailscale.com/docs/reference/tailscale-cli/serve) and
  [Serve examples](https://tailscale.com/docs/reference/examples/serve).
- **Counts, no identity.** Measure `/web` visits and approximate connected-hub
  counts only. No account IDs, hub addresses, credential IDs, session content
  or stable measurement identifiers leave the browser. See Usage measurement
  for the limits of counting without identity.

## Feature parity and API map

Paths in this table are hub paths. `{s}` is a URL-encoded session ID, `{h}` a
host ID, `{a}` a harness ID, `{f}` a finding ID, and `{flow}` an auth flow ID.
Unless a method is given, the method is `GET`. Controls follow the capabilities
published in `/harness`; unknown adapters must work without name-based logic.

| iOS screen / behavior | Web equivalent and phase | Hub endpoints used by the screen |
| --- | --- | --- |
| Home / fleet (`SessionsView`, `FleetOverviewView`, `SessionStore`) | P0 Home: fleet summary, host availability, sessions grouped by host/project, live versus finished state, exporter/readiness warning and last refresh. Host and project filters combine with AND, as in `FleetFilterState`; filter locally from the same snapshot. | `/harness`; `/readyz` (read useful exporter status even on 503); `/cockpit/overview?days=7` for capacity/activity/insight summaries. |
| Inbox / Needs you (`SessionsView`, `WorkAttentionView`, `WorkAttentionStore`) | P0 Inbox combines sessions needing attention with environment gates, PRs, questions and failed/blocked work; show repository, source, observation age and stale retained rows. GitHub-only operations remain links. P1 enables only actions the refreshed row offers, with confirmation and workspace/request checks. | `/harness`; `/work/list?view=attention&limit=100`; P1 `POST /work/decide` with the existing `goal_id`, `number`, `decision_id`, `action` body. The workspace/source identity is checked against the refreshed row before sending. |
| Inbox / Insights (`InsightsView`, `InsightDetailView`, `CockpitStore`) | P0 filtered, cursor-paged findings, severity/confidence/source, evidence, uncertainty and detail. Accessible both from Inbox and a navigation shortcut, matching iOS's dedicated Insights tab. P1 acknowledge, dismiss with reason and check again. | `/insights` with `state`, `severity`, `confidence`, `analyzer_class`, `host`, `harness`, `target_type`, `target_id`, `cursor`, `limit`; `/insights/{f}`; P1 `POST /insights/{f}/acknowledge`, `/dismiss`, `/check`. |
| Inbox / History (`HistoryView`, `HistoryModel`) | P0 all-host history, retired-host labels, search, host/harness/repo/state/date filters, local-day sections, cursor paging and a read-only transcript. Inbox owns the route, and Home keeps a History shortcut. Restore filters/scroll on Back. A 501 means unsupported control store, not an unreachable hub. | `/sessions/history` with the existing filters and opaque cursor; `/sessions/history/facets`; `/harness/sessions/{s}/messages` with newest/`before_seq` paging and `limit <= 200`. |
| Session detail / activity (`ChatView`, `ChatModel`, `SessionActivityView`) | P0 session metadata, state, live recap, context/usage, activity and capability explanation from the snapshot/events. P1 Continue/handoff, native resume candidates, interrupt and confirmed terminate. Preserve uncertain outcomes instead of claiming success. | `/harness`; `/harness/sessions/{s}/messages`; P1 `/harness/hosts/{h}/native-sessions` with harness/source context; `POST /harness/sessions/{s}/continue`, `/interrupt`, `/terminate`. |
| Transcript (`MessageStream`, `Transcript`, `DisplayBlocks`, `ToolStep`, `EditDiff`) | P0 paged text/Markdown, tools, diffs, attachments, gaps and status with bounded rendering and “Load earlier.” Keep scroll anchored; auto-follow only when already at the tail. P0 REST updates; P1 resumable WebSocket updates. No raw HTML from a transcript. | `/harness/sessions/{s}/messages?limit=…`, `?before_seq=…`, `?after_seq=…&through_seq=…`; P1 WebSocket `/harness/sessions/{s}/stream?after_seq=…` plus the ticket exchange described below. |
| Composer / approvals (`ChatView`, `ChatModel`, `HarnessRunPreferences`) | P1 text, supported image paste/upload, model/effort selection, drafts, pending-turn reconciliation and allow/deny with optional note. Distinguish a harness permission from a work-queue decision. Never silently resend a mutation after a lost response. | `POST /harness/sessions/{s}/turns` (existing image payload and `client_turn_id`); `POST /harness/sessions/{s}/permission` (`request_id`, `decision`, `note`); `/harness/hosts/{h}/model-catalog`. |
| PTY session (`TerminalView`, `TerminalStream`, `TerminalWire`) | P0 transcript/snapshot view where available, with an explicit “interactive terminal in P1” state. P1 lazy-loaded terminal, keyboard, resize and reconnect using the current terminal wire protocol. Terminal commands are an explicit interactive surface. | P1 WebSocket `/harness/sessions/{s}/terminal`, ticket exchange; `/harness` for metadata/capabilities. The existing operator terminal remains usable during migration. |
| New Session (`LaunchView`, `LaunchModel`, `FolderBrowserModel`, `AuthFlowModel`) | P1 host/harness/mode, prompt/images, folder browser, manual path/completion, host-scoped recents, model/effort and launch. Preserve iOS capability and auth gating, including native-resume entry points. A browser folder picker is not a substitute for browsing the remote host. | `/harness`; `/harness/hosts/{h}/model-catalog`; `/fs/list`, `/fs/complete`, `POST /fs/exists`, `POST /fs/create` under `/harness/hosts/{h}`; `/native-sessions` under the same host; `POST /harness/hosts/{h}/sessions` with `client_session_id`; provider auth routes below. |
| Provider CLI authentication (`AuthFlowModel`) | P1 existing provider device/browser/manual auth flow. Open the provider's offered URL safely and show status, input and cancel; this is not OAuth for Drover accounts. | `/harness/hosts/{h}/auth/{a}/status`; `POST …/start`; `GET …/flows/{flow}`; `POST …/flows/{flow}/input`, `…/cancel`. |
| Analytics (`AnalyticsView`, `DistributionSectionView`) | P0 time range, host/harness/provider/model/project filters, totals, observed coverage/freshness, provider windows and distributions by project/agent/host/model. Load each dimension's next page independently and retain data on busy/stale responses. Provide a data table for every chart. | `/analytics` with `days`, `host_id`, `harness`, `provider`, `model`, `project_key`, `metric`, `limit` and `project_cursor` / `harness_cursor` / `host_cursor` / `model_cursor`; `/harness` for host names and API capability; `/cockpit/overview` for shared summaries. |
| Accounts / quota (`ProviderAccountsView`, `ProviderCapacitySection`) | P0 provider accounts, per-host source, used/remaining windows, resets and stale/degraded/unknown states. Unknown quota stays unknown, not zero. No provider secrets copied to browser storage. | `/cockpit/overview?days=7` (`provider_capacity`); `/harness` for host labels. Provider sign-in actions use the existing auth routes in P1. |
| Settings / pairing (`SettingsView`, `PairingView`, `ContentAnalysisSettings`) | P0 minimum connection bootstrap described below. P1 full hub switch/re-pair, connection test, browser preferences, sign out/revoke and content-analysis consent/status/purge. Show browser storage and HTTPS guidance. Manual code entry is required; camera QR scanning is optional on secure contexts. | Public `/healthz`; `POST /auth/pair`; `DELETE /auth/device/credential`; `/insights/content-analysis`; `POST /insights/content-analysis/consent`, `…/revoke`; `DELETE /insights/content-excerpts`. Existing `/auth/credentials` and `DELETE /auth/credentials/{id}` remain hub credential-management tools; a web client does not acquire operator authority. |
| Notifications (`Notifier`, `NotificationRouting`, `APNsRegistration`, `NotificationDelivery`) | P0 unread/Needs you count in the page title and navigation, with session/attention deep links. P1 optional foreground browser notifications after a user gesture, with generic text. Closed-tab delivery and Web Push are later work. No promise of background polling. | Existing iOS calls are `PUT`/`DELETE /auth/device/apns` and `GET`/`PUT /auth/device/notifications`. Web does not register APNs or pretend these create Web Push subscriptions. P0/P1 alerts derive from `/harness` and `/work/list`; no new notification endpoint. |

Native-only details have explicit equivalents: file paste/upload replaces the
photo picker; clipboard/download replaces the share sheet; URL navigation
replaces universal links; desktop keyboard controls replace mobile accessory
keys. Widgets, Live Activities, APNs/background refresh and the native offline
demo are outside P0-P2. A static test fixture/demo may be added later without a
hub endpoint. Interactive PTY is a P1 capability, not a P0 parity claim.

The source references are relative to `apps/drover/`: UI screens live under
`Drover/Screens/`, the tab shell in `Drover/MainTabView.swift`, and the API,
models and stores in `DroverKit/Sources/DroverKit/`. In particular,
`DroverClient.swift` defines the HTTP methods/query/body contracts;
`FleetFilterState.swift`, `SessionHistory.swift`, `MessageStream.swift` and
`RetryPolicy.swift` define behavior worth porting as fixture-tested state
machines. Reuse the contracts and fixtures, not a second handwritten backend.
See also [Session History](session-history.md),
[Work queue phase 3](work-queue-phase-3.md),
[Folder picker](new-session-folder-picker.md) and
[Adapter extension points](adapter-extension-points.md).

## Origins, pairing and browser constraints

### Connection and credential lifecycle

Use a device bearer token for **both** `/ui` and `/web`, with
`Authorization: Bearer …` on hub fetches and `credentials: "omit"`. Pairing
also omits cookies and uses the existing one-use, throttled code redemption.
Reject a host-scoped pairing result, clear it locally and explain that a device
code is needed. Do not accept the cluster/operator token as the SPA login.

P0 needs a small address/code connection gate to make read-only parity usable
on a secured hub. This is the necessary bootstrap exception to “read-only”;
it issues a credential but offers no session/work mutations or general Settings.
P1 expands that gate into the complete Settings/pairing flow. The pairing
request is not automatically retried: an expired/burned code returns 410;
a lost response requires a fresh code and inspection of unused hub credentials.

Keep the active token in memory and persist it in IndexedDB, keyed by the
canonical hub origin and returned `server_id`/credential ID. Persistence is
origin-local: `/ui` and drover.fyi pair separately, and HTTP/HTTPS addresses
are separate browser origins. Offer session-only storage and a clear
“Remember this hub on this browser” choice. Do not persist transcripts or
attachments by default; P1 drafts are local and cleared on sign out/hub switch.
Storage denial/private mode falls back to memory with an explanation.

Treat a changed hub URL as a new trust decision: never copy its old token to
another origin, follow cross-origin API redirects, or infer a hub from a URL
query and immediately contact it. Validate scheme/host/port, reject userinfo,
paths other than `/`, fragments and embedded credentials. Confirm the address
before redemption. Deep links select a route only within the selected hub;
they cannot replace the hub or supply tokens. On hub switch, abort reads and
sockets, discard in-memory content and ignore obsolete async responses.

A 401 stops polling/stream reconnects and requires re-pairing. Sign out calls
`DELETE /auth/device/credential`, then clears token, drafts and caches across
tabs. If offline, clear local data and state plainly that hub revocation is
still needed. Hub credential management can revoke the browser independently.

The existing `/auth/login` cluster-token exchange creates an operator
`HttpOnly; SameSite=Strict` cookie. Keep that cookie on the legacy operator
pages, including Observatory; do not use it as SPA identity. A new
device-bound HttpOnly cookie could reduce token theft on same-origin `/ui`,
but would add session/CSRF machinery and would not solve cross-origin third
party cookie blocking at `/web`. Shared bearer semantics preserve iOS auth and
per-device revocation. A device credential is broad authority, not a read-only
scope merely because P0 hides action controls.

### Origin matrix

| Page origin | Hub origin | Supported behavior |
| --- | --- | --- |
| `http://lan-host:7080/ui` | same HTTP origin | P0 works with same-origin REST, no CORS; later `ws:`. Transport is plaintext on the LAN, including pairing/token. Recommend HTTPS when available. Secure-context-only features such as camera/notifications may be absent. |
| `https://<host>.<tailnet>.ts.net/ui` | same HTTPS origin | Recommended full client with REST and `wss:`; no cross-origin allowlist needed. |
| `https://drover.fyi/web` | HTTPS LAN/tailnet hub | P2, after explicit hub CORS opt-in and any browser/OS local-network permission. Valid, trusted TLS is required; tailnet routing and DNS must work on the browser's device. |
| `https://drover.fyi/web` | HTTP hub | Unsupported by product policy. Show the HTTPS/Serve setup route before sending a pairing code. Opening the hub's own HTTP `/ui` remains an option. |

HTTPS pages generally cannot fetch HTTP APIs or use insecure WebSockets.
CORS headers cannot fix mixed content, a certificate error or missing tailnet
connectivity. Chromium has permission-gated local-network exceptions, but
these are not a portable basis for sending device tokens over HTTP. Require
an HTTPS hub for `/web` regardless.
See [Mixed content](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Mixed_content).

### CORS and Private Network Access

The current hub has no general CORS layer. P2 adds an **empty-by-default,
operator-configured exact origin allowlist**, with an explicit option to add
`https://drover.fyi`. Origins include scheme/host/port, never paths: the hub
cannot grant CORS only to `/web`. No `*`, suffix match, arbitrary Origin
reflection, `null`, or implicit localhost entries. Local development origins
must also be configured explicitly.

For an allowed origin and permitted client route/method, answer unauthenticated
`OPTIONS` before the normal auth gate with 204, exact
`Access-Control-Allow-Origin`, `Vary: Origin, Access-Control-Request-Method,
Access-Control-Request-Headers`, explicit `GET, POST, PUT, DELETE, OPTIONS`
as applicable, and `Access-Control-Allow-Headers: Authorization, Content-Type`.
Use a short preflight max-age (300 seconds). Authorization and JSON bodies
trigger preflight; preflight itself must not need a token. Add CORS headers to
actual responses, including 400/401/410/429/501/503, and expose `Retry-After`.
Allow only the client routes in the parity map plus the ticket route, not
operator login, credential issuance, host registration or event ingestion.

Do not emit `Access-Control-Allow-Credentials: true`: this design supplies
an explicit bearer header, omits ambient cookies, and does not need credentialed
cookie fetches. Reject disallowed browser Origins before dispatching client
mutations, including simple requests; CORS response hiding alone does not
prevent writes. Require JSON for pairing/action bodies and preserve existing
auth, scope and pairing throttles. Origin-less native requests retain their
existing bearer behavior. See [CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS).

Private Network Access (PNA) proposed an additional
`Access-Control-Request-Private-Network: true` preflight. If a browser sends it,
respond with `Access-Control-Allow-Private-Network: true` only for an allowed
origin/route under the same explicit opt-in. Never grant it globally.
Current Chromium replaced that rollout with Local Network Access permission
prompts. Permission denial must show a local-network/browser-settings hint
and a link to the hub-served `/ui`, not an infinite reconnect spinner.
Permission and CORS are independent requirements; ordinary CORS still applies.
Test shipping browsers rather than assuming every private/tailnet address
has identical classification. See Chrome's
[Local Network Access update](https://developer.chrome.com/blog/local-network-access).

### CSP and untrusted content

Hub `/ui` uses response headers with `default-src 'none'; script-src 'self';
style-src 'self'; img-src 'self' blob: data:; font-src 'self'; connect-src 'self';
object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`.
Explicitly include the hub's `ws:`/`wss:` origin in `connect-src` where browser
handling of `'self'` requires it. No inline scripts, eval, third party fonts,
CDN runtime dependencies or automatic remote Markdown images. Add
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

For hosted `/web`, allow `https:` and `wss:` in `connect-src` because a static
document cannot know the user-entered HTTPS hub in advance; keep the other
directives equally strict. This is a deliberate weaker egress boundary.
The API layer still binds requests/tokens to the selected origin. Host the
same locally bundled assets, set CSP as hosting headers (not only a meta tag),
and keep hub addresses out of page URLs/referrers. Do not add CSP reporting
that sends private hub URLs to a public collector.

Render transcript Markdown with raw HTML disabled and a sanitizer/URL scheme
allowlist; code/diffs/tool output and finding evidence are text, not DOM HTML.
External links use `noopener noreferrer`; images are inline attachments or an
explicit user-opened resource. Never execute agent-produced links, terminal
escape sequences outside the terminal renderer, or suggested commands on load.
IndexedDB offers no XSS protection over localStorage: any script running on
the origin can read stored tokens. These controls reduce injection risk, not
the power of malicious first-party JavaScript.

## Streaming and freshness

iOS today uses **REST plus WebSocket, not SSE**:

- `SessionStore.startPolling` refreshes `/harness` every 5 seconds in the
  foreground, with backoff/busy handling. `CockpitStore` refreshes shared
  `/cockpit/overview` data every 30 seconds when supported. Insights/analytics
  are fetched on visit/filter change and paged on demand. Work attention uses
  the projection's policy-provided polling interval/freshness thresholds.
- `MessageStream` loads a cold window of 200 events in pages of 50, supports
  older pages, catches up to a fixed `max_seq` over REST, then opens
  `/harness/sessions/{s}/stream?after_seq=…`. It orders/deduplicates sequence
  numbers, handles explicit gap markers and catches up before each reconnect.
  The hub stream reads durable registry events up to five times per second.
- `TerminalStream` connects to `/harness/sessions/{s}/terminal` and uses the
  existing terminal input/output/resize protocol. Both Swift sockets attach
  `Authorization: Bearer …` during the handshake.

P0 ports the REST state machines and uses a 1-second, non-overlapping
`messages?after_seq=…&limit=50` poll for the **open live transcript only**.
Freeze `through_seq` at the first page's high-water mark while catching up,
then start the next pass; never advance a cursor past missing/invalid events.
Finished sessions need no continuous poll. Preserve older pages and explicit
gaps. Fleet remains on its independent 5-second lane, overview on 30 seconds,
and work attention on its policy interval. Cancel when hidden/offline and
refresh/catch up when visible; browsers may suspend hidden tabs entirely.

Use bounded transcript windows (200 events initially, 200 maximum per older
request, 1,000 materialized events with loadable placeholders), virtualized
history (300 full rows plus page cursors), and size-bounded decoded attachment
previews. Do not fetch every transcript for the fleet. Show last successful
refresh and stale data after outages, respect `Retry-After` independently for
fleet/history/analytical reads, and stop rather than loop on 401. These are
client targets to verify, not claims about current web behavior.

### Browser WebSocket authentication (P1)

The browser's [WebSocket constructor](https://developer.mozilla.org/en-US/docs/Web/API/WebSocket/WebSocket)
accepts a URL and subprotocols, not arbitrary request headers. The operator
cookie can authenticate today's same-origin console socket, but must not
silently become the paired SPA's credential. Cross-origin cookies are also
an unreliable dependency. REST polling is a safe P0 bridge; streaming and
interactive PTY justify one new, reusable transport endpoint:

`POST /auth/device/stream-ticket`, authenticated by the existing device bearer,
with `{"session_id": "…", "transport": "stream" | "terminal"}`. Return a
random opaque ticket and expiry with `Cache-Control: no-store`. Proposed
contract: 30-second expiry, single redemption, stored hashed in a bounded
hub-local cache, bound to credential ID, exact session, transport and browser
Origin. Require same hub Origin in P1 or a configured allowlisted Origin in
P2. Deny missing/`null` browser Origin; this endpoint is not needed by iOS.

Use the ticket as `?ticket=…` on the existing `/stream` or `/terminal` path;
retain `after_seq` for replay. Validate/consume it atomically before 101 and
resolve the originating device credential again. It grants no REST access,
other session, other transport or credential issuance. Redact ticket queries
from hub/proxy access logs and errors; never place long-lived tokens in URLs
or subprotocols. Rate-limit issuance/redemption and cap outstanding tickets
per credential. Expired, replayed, wrong-origin and revoked tickets fail closed.
Hub restart simply loses tickets; obtain a fresh one.

Every browser handshake checks Origin separately from CORS, which does not
protect WebSockets. Do not trust arbitrary forwarded Host/Origin headers;
configure the canonical external origin behind Serve. Track browser sockets
by credential and close them promptly on revocation, including remote
credential changes (target <= 5 seconds with periodic credential recheck).
Revoke unused tickets too. This is new work: existing long-lived socket loops
must not be assumed to reauthenticate after their initial gate.

P1 keeps REST polling as a visible degraded fallback for structured chat when
WebSockets fail. PTY reports a disconnected state and requires reconnection;
it cannot use transcript polling as an interactive substitute. No automatic
replay of terminal keystrokes or action POSTs. Port iOS catch-up/order/gap and
jittered reconnect rules; reuse current event and terminal wire schemas.

## Stack and packaging

Choose **TypeScript + Preact + Vite**, a client-only build in proposed
`apps/web/`. Preact supplies small composable views for the many stateful
screens; TypeScript makes the shared API shapes explicit. Vite produces
static HTML/CSS/JS, with no runtime Node process or SSR. Keep routing small
(hash routes under `/ui/#/…` and `/web/#/…`), and separate route state from
hub origin/auth state. Hash routes avoid server fallback routing and preserve
the existing `/ui/history` and `/ui/harness` pages during migration.
See [Preact](https://preactjs.com/guide/v10/getting-started/) and
[Vite static builds](https://vite.dev/guide/build.html).

Use native fetch, small explicit stores/state machines, CSS variables, system
fonts and semantic HTML. A full meta-framework/SSR server brings no benefit
to an authenticated private API client. Expanding the legacy inline HTML
pages would duplicate navigation/auth/state across screens. Do not start with
a large component kit, charting suite, query framework or icon font.
Lazy-load sanitized Markdown/diff rendering, analytics and the terminal
renderer; [xterm.js](https://xtermjs.org/) supplies terminal rendering without
writing ANSI/input parsing. Pin and audit that dependency when P1 begins.

Targets: initial JS <= 100 KiB gzip, initial CSS <= 20 KiB gzip; all non-terminal
route JS <= 250 KiB gzip total. Measure production artifacts in CI, excluding
source maps and separately reporting the lazy terminal chunk. These are
acceptance budgets, not measured results. Avoid remote code/assets at runtime.
Use a locked package manager install and reproducible build command.

Build relative asset URLs so the **same artifact** mounts at either base path.
Copy dist files into proposed `src/drover/server/web/static/client/` during the
release build and include HTML, nested hashed JS/CSS, fonts/icons and the asset
manifest explicitly in `pyproject.toml` package data. Do not commit generated
bundles. CI/release must build the SPA before the Python wheel; building a
release wheel without the client must fail, not silently ship a blank `/ui`.
Validate the installed wheel in a clean environment without Node or the source
checkout. Add a manifest-bounded static loader with MIME types and traversal
protection; never expose arbitrary package resources.

Serve shell/manifest with revalidation, hashed assets with immutable caching,
and credential/transcript/API responses with `no-store`. Do not install a
service worker in P0-P2: offline caching and stale hosted code complicate
revocation, privacy and recovery. Expose build/API compatibility in Settings;
use `/harness` capabilities to disable unsupported features and explain old
hubs. P2 publishes the same build into `site/public/web/` before the existing
Astro site build; Astro remains the static marketing/docs build, not an API.

### Accessibility and theme

Port the Tide theme from `Drover/Design/DroverColor.swift` to CSS variables,
with `prefers-color-scheme` and a local override. Preserve the exact ramps:

| Token | Dark | Light |
| --- | --- | --- |
| bg | `#15161a` | `#e4e7e5` |
| surface | `#1d1f24` | `#f5f7f6` |
| sheet | `#23262c` | `#f5f7f6` |
| text | `#e9e9ec` | `#2a2b2e` |
| muted | `#a4a6ad` | `#5b5d65` |
| faint | `#8c8e96` | `#767881` |
| line | `#2f323a` | `#d3d6d4` |
| accent | `#2aa79c` | `#116e68` |
| accentHi | `#4fc7bb` | `#0e5450` |
| accentTint | `#112628` | `#d8f0ec` |
| warn | `#edb05a` | `#8a4b0a` |

Match `DroverText.swift` hierarchy using system sans/monospace, wrapping text,
tabular numerals and the same attention shapes (filled/hollow/faint rings).
Use `accentHi` for colored body text and `accent` for controls. Tokens do not
automatically make every combination accessible: faint labels/dividers need
context-appropriate treatment, and rendered text must meet WCAG AA (4.5:1
body text, 3:1 large text/UI boundaries). State must have words/icons as well
as color. Terminal keeps its fixed dark palette and an accessible text view.

Require full keyboard operation, visible focus, skip navigation, semantic
landmarks/headings, labelled controls, focus trapping/restoration in dialogs,
Escape/Back behavior, reduced motion, 200% text zoom and reflow at 320 CSS px.
Announce busy/errors and concise new-activity summaries through polite live
regions; never read every streaming token aloud or steal focus on updates.
Virtualized lists need stable row semantics, keyboard navigation and an
explicit Load more/earlier alternative. Chart tables and transcript text must
work with VoiceOver/NVDA. Test light/dark/high-contrast appearances manually
as well as with automated checks.

## Security review

The hub, the local device and the loaded client code are trust boundaries.
Threats include a hostile web origin probing a LAN hub, malicious transcript
content, browser extensions/shared profiles, stolen device tokens, compromised
npm/build/release infrastructure, compromised drover.fyi/site deploy access,
and interception of plain LAN HTTP. Device actions can launch agents and run
terminal commands, so “browser access” can become code execution on a host
within the credential's capabilities. Retaining an operator cookie on the
same origin also means `/ui` XSS can attempt ambient operator requests even
though SPA fetches deliberately omit cookies; avoid opening operator pages
in untrusted browser profiles and keep operator mutation protections intact.

A compromised drover.fyi server does not inherently possess an unpaired hub's
token and cannot read authenticated hub data merely because it hosts files.
But the hosted client **is trusted code**. Malicious JavaScript delivered on
that origin can read its IndexedDB/memory tokens, capture a pairing code,
redeem it, read transcripts and perform any allowed device actions. It can
exfiltrate data/tokens to its own server or another destination. Existing
users who reopen the page are exposed; previously loaded honest tabs are not
magically isolated from future navigations. Other paths on `https://drover.fyi`
share the origin, storage and trust: `/web` is not a security boundary.

CORS opt-in trusts that entire origin. It is not user identity, protection
against native clients, or a sandbox around malicious allowed JavaScript.
Removing the allowlist blocks future browser requests/handshakes but does not
invalidate a token already stolen; revoke the browser credentials. The
incident procedure is to withdraw the hosted build, disable that origin,
revoke affected device tokens, close sockets, and rotate/re-pair after review.

Pin dependencies/lockfiles, minimize install scripts and runtime dependencies,
review release/build permissions and changes to generated output, and publish
versioned artifact hashes/provenance. Attach SRI hashes to built script/style
entry points and module preloads, and verify deploy artifacts against the
release manifest. [SRI](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Subresource_Integrity)
detects changed subresources when the referencing document is trusted; it
does **not** protect against an attacker who can replace both HTML and hashes.
Likewise CSP can be replaced by a compromised hosting origin. HTTPS protects
transport, not a malicious publisher. These controls reduce accidental and
dependency/CDN compromise risk; they do not justify claiming hosted code
cannot steal a hub token.

The hub-served build avoids ongoing trust in drover.fyi deployment and is the
default for P0/P1. It still trusts the wheel/release supply chain and any code
served by the hub. No runtime scripts from drover.fyi are loaded into `/ui`.
For users needing stronger integrity, use an inspected/pinned hub wheel and
HTTPS `/ui`; signed manifests alone do not add an independent browser trust
anchor. Plain HTTP `/ui` remains supported but cannot provide script integrity
against an on-path attacker who can replace its HTML.

## Usage measurement

Keep this separate from the user's hub `/analytics`, which is private product
data. P0/P1 hub-served clients send no usage requests to drover.fyi. P2 records:

- Aggregate page visits to `/web`, using hosting-provider aggregate counters
  with raw visitor logs/IP retention, fingerprinting and cookies disabled.
- One coarse “hub connected” event after an authenticated successful snapshot,
  deduplicated locally per hub origin per browser per UTC day. Store the local
  day/set only in the browser; do not transmit addresses, IDs, hashes of those
  values or a deduplication identifier. Send an empty request to a fixed static
  counter asset such as `/web/_counts/connected.gif`, with no query parameters,
  no referrer, `credentials: "omit"` and caching disabled. Count requests with
  the hosting provider's aggregate facility, not a new Drover app backend.

The second metric estimates browser/hub connections, **not globally unique
hubs**: another browser, cleared storage or a new day counts again. Exact
global uniqueness would require identity and is out of scope. Label the chart
accordingly. Counts can be lost to blockers or inflated by bots and must not
gate features. Only aggregate counts are retained; network requests inevitably
expose transport metadata to the hosting provider, so verify its actual
logging/retention settings before enabling collection. Do not install a stock
analytics SDK that quietly adds identifiers, URL capture or session replay.
If the provider cannot offer count-only retention, ship P2 without that metric
until it can. Honor browser privacy preferences and provide a local opt-out.

## Phased plan and acceptance gates

Estimates are engineering days for one engineer familiar with the repo,
including focused tests, browser QA and review, not calendar commitments.
Approximate changed source/test lines exclude generated bundles and lockfiles.
Sequence these as small PRs; approval of this design does not authorize P0.

| Phase | Scope and estimated size | Hub / packaging changes |
| --- | --- | --- |
| **P0: hub-served read-only parity** | 10-15 days; 4-6 PRs; roughly 4-6K lines. Client shell/connection bootstrap, fleet/host filters, sessions, bounded transcript, Needs you, Insights/detail, History, Analytics and Accounts. All action controls absent or clearly unavailable. | Serve unauthenticated static shell/assets at `/ui` so pairing is reachable; APIs remain gated. Move Observatory to authenticated `/ui/observatory`, retaining existing `/` behavior initially. Manifest-bounded asset loader, CSP/cache headers, wheel package data, client build/CI/release ordering. No new domain endpoints, CORS or auth scopes. |
| **P1: actions and full settings** | 10-15 days; 4-6 PRs; roughly 4-6K lines. Composer/images/reconciliation, approvals/work decisions, launch/folder/provider auth, Continue/native resume, interrupt/terminate, full pairing/settings/content consent, optional foreground alerts, transcript sockets and lazy PTY. | Device stream-ticket exchange/cache, narrow ticket-aware socket auth, exact Origin enforcement, active revocation/closure and log redaction. Reuse all existing mutation routes and idempotency fields. Retire legacy harness/history/terminal client pages only after link compatibility is verified; keep Observatory. |
| **P2: hosted static client** | 4-7 days; 2-3 PRs; roughly 1-2K lines. Publish `/web`, HTTPS onboarding/errors, CORS opt-in, browser network-permission UX and coarse usage counts. | Exact-origin CORS configuration and route/method policy, unauthenticated preflight, error headers/Retry-After exposure, optional legacy PNA response, hosted Origin accepted by ticket exchange. Static site build/deploy headers and aggregate counter configuration. No public hub listener, Drover backend or relay. |

### P0 acceptance

1. Build once and install the wheel in a clean Python environment without
   Node/source checkout. `/ui` and hashed assets load on plain LAN HTTP and
   Serve HTTPS; the shell is public but fleet/history/transcript reads still
   return 401 without a device credential. Asset traversal/unknown names fail.
   `/ui/observatory` retains the operator login gate; old console links work.
2. Pair with a real test device code; verify `device` scope and browser label,
   origin-bound storage, persistence-denied fallback and revocation -> 401 ->
   stop/re-pair. The browser makes no hub mutation other than pairing. Test
   with an existing operator cookie to prove the SPA uses its own bearer.
3. Fixture-driven browser flows cover fleet filters, offline/retired hosts,
   all four attention sources, finding detail, history search/filter/cursor
   reset/Back, independent analytics cursors, and stale/unknown quota. Check
   response capability gates and History 501 without breaking Home.
4. A long transcript loads newest/older/newer bounded pages, preserves gaps,
   ordering/scroll and late-arriving events across reconnect. A 10K-session
   fixture keeps DOM/memory bounded; rapid navigation/filter changes cannot
   apply a previous request to the current hub/view. Busy analytics/history
   honors `Retry-After` without blocking fleet updates; 401 stops every lane.
5. Playwright flows run in Chromium, Firefox and WebKit. Test keyboard-only
   navigation and automated accessibility scans; manually check screen reader,
   light/dark contrast, zoom/reflow and long transcript announcements. Production
   bundle budgets pass. Network inspection shows no third party requests.

### P1 acceptance

1. Launch, send with supported attachments/model/effort, approve/deny, work
   decisions, Continue/native resume and interrupt/terminate work against
   capability fixtures including an unknown adapter. Confirm request bodies
   match iOS. A resolved/stale decision cannot execute again; uncertain POST
   outcomes are inspected/reconciled, never blindly retried. Client turn/session
   IDs remain stable for explicit reconciliation of the same operation.
2. Hub tests prove tickets are single-use, short-lived, session/transport/
   credential/Origin-bound and rate-limited; no REST access is possible with
   a ticket. Test missing/wrong Origin, restart, replay and revoked credentials.
   No token/ticket appears in access logs. Existing iOS bearer handshakes work.
3. Browser sockets catch up/dedupe after a lost connection, cannot cross hubs,
   and close within the revocation target. Structured chat falls back to REST
   visibly; PTY reconnect never replays keystrokes and handles resize correctly.
4. Folder traversal/permission failures, provider auth cancel, pairing-code
   expiry, storage clearing, offline sign out and cross-tab logout have useful
   outcomes. Content consent/revoke/purge shows existing propagation status.
   Foreground notification denial does not block use; closed-tab push is not
   advertised. XSS fixtures in Markdown, links, tool output and findings cannot
   execute or load tracking resources. Approval dialogs pass keyboard/reader QA.

### P2 acceptance

1. The same artifact runs under `/ui` and `/web`; authenticated hub traffic
   goes directly to the chosen HTTPS hub. Site deployment contains no secrets
   and adds no API proxy. HTTP hub entry is rejected before pairing; trusted
   Serve HTTPS works from a device on the tailnet, without exposing a public hub.
2. Integration tests cover default-deny and configured exact Origins, ports,
   `null`/suffix lookalikes, unauthenticated preflights and every mapped method,
   success/error CORS headers, `Retry-After`, and no cookie credential support.
   Disallowed-origin simple POSTs do not redeem codes or execute actions.
   Native iOS requests and same-origin `/ui` behavior remain unchanged.
3. Real Chrome/Edge, Firefox and Safari tests cover LAN/tailnet HTTPS, denied
   browser/OS local-network permission, CORS denial, DNS/offline and bad certs.
   Legacy PNA preflight is tested synthetically. Do not claim fetch can reliably
   distinguish these errors; show a bounded diagnostic checklist and `/ui`
   fallback. Hosted sockets enforce the same allowlist independently of CORS.
4. CSP blocks inline/eval/remote scripts; a modified subresource fails SRI with
   trusted HTML. Verify artifact hashes and site headers. Inspect storage and
   network traces: visit/connection counters contain no private identifiers,
   referrers or cookies; provider raw retention is disabled, local dedupe/opt-out
   works, and metrics failure cannot affect hub use. Review the hosted-origin
   compromise/revocation procedure before enabling production hosting.

## Open questions

1. **Remember credentials by default?** Recommended: session-only initially,
   with an explicit Remember checkbox for personal browsers. No change to
   device scope/revocation; validate onboarding friction during P0.
2. **Which hub's data is visible at once?** Recommended: one active hub per
   tab, with locally saved connections and a full state reset on switch.
   Multi-hub aggregation would create a new product/API coordination problem.
3. **How long to keep legacy client routes?** Recommended: retain them through
   P1, then redirect fleet/history/session links to equivalent hash routes,
   preserving session IDs/filter parameters. Keep `/ui/observatory` permanently.
4. **Hosted release skew versus installed hubs?** Recommended: versioned static
   releases, visible client version, feature detection through existing
   `/harness` capability/API fields, and `/ui` as the compatible fallback.
   Do not force a hub upgrade merely to read an older supported API.
5. **Closed-tab notifications?** Recommended: defer Web Push beyond P2.
   A later design must cover permission UX, subscriptions, VAPID delivery,
   browser push-service metadata and the privacy/network implications. The hub
   could send directly to browser push services without a Drover relay, but
   the current APNs endpoints do not implement that contract.
