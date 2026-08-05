# Drover Public Release Checklist

Status: proposed release checklist
Date: 2026-08-05

## Goal

Prepare Drover for a first public repository release without blurring the
product boundary. The release should show Drover as a local-first cockpit and
durable context store for personal CLI agent fleets. It should not expose
private dogfood assumptions, stale Nexus branding, or half-migrated operational
contracts as if they were stable public APIs.

## Current Open Issues

Checked with `gh issue list --state open --limit 200` on 2026-08-05.

| Issue | Title | Release class | Notes |
|---|---|---|---|
| [#4](https://github.com/arniesaha/drover/issues/4) | Pre-public-repo work: DroverKit rename, docs debranding, Traycer positioning, sanitization | Blocker | Superseded in part by #14; keep for Swift package rename, public docs, sanitization, positioning, and repo visibility. |
| [#5](https://github.com/arniesaha/drover/issues/5) | Retire Nexus rollback artifacts after soak | Ops cleanup | Do after live soak and physical-device verification; not a repo-code blocker unless paths leak into public docs. |
| [#11](https://github.com/arniesaha/drover/issues/11) | NAS user services do not start at boot | Release candidate | Public release can document direct/relay setup, but dogfood reliability should prefer #16 relay migration or a system-scope unit before public demos. |
| [#12](https://github.com/arniesaha/drover/issues/12) | Harness auth flows from iOS | Release candidate | Implementation is mostly complete; live phone verification remains. Public docs can mention auth flows only after Mac/NAS live checks land. |
| [#13](https://github.com/arniesaha/drover/issues/13) | Per-host tokens for the funnel-exposed hub | Blocker if Funnel is documented | A public Funnel story cannot rely on a single shared bearer token where any holder can claim any host. Either ship per-host tokens or omit Funnel from public quickstart. |
| [#14](https://github.com/arniesaha/drover/issues/14) | Full nexus->drover rename incl. storage contracts | Blocker decision gate | Decide whether first public release includes storage-contract migration. Do not blindly rename `nexus.*` span keys without a lakehouse migration and compatibility plan. |
| [#15](https://github.com/arniesaha/drover/issues/15) | OKF assessment doc for the context layer | Documentation/research | Update scope from OKF v0.1 to OKF v0.2. Assessment only; implementation should stay post-release unless the verdict says otherwise. |
| [#16](https://github.com/arniesaha/drover/issues/16) | Migrate NAS harnessd to relay mode | Release candidate | Good mitigation for NAS boot and inbound reachability issues. Useful for demos but not required for a local-only quickstart. |
| [#17](https://github.com/arniesaha/drover/issues/17) | Session-row token usage server-side rollup | Post-public polish | Not a release blocker. Keep session rows honest by hiding unsupported usage rather than showing wrong values. |
| [#18](https://github.com/arniesaha/drover/issues/18) | LaunchModel hides stale hosts | Post-public polish | Improve host picker clarity later; not a public-release blocker if docs explain online/stale host states. |
| [#20](https://github.com/arniesaha/drover/issues/20) | Background notifications need push path | Public docs caveat | Do not claim timely background alerts without APNs or third-party push relay. Foreground attention state can ship. |
| [#21](https://github.com/arniesaha/drover/issues/21) | Naive timestamps make iOS show fresh sessions as old | Blocker for app credibility | Fresh sessions showing as hours old undermines the cockpit. Fix aware UTC wire timestamps before public app screenshots or demos. |
| [#23](https://github.com/arniesaha/drover/issues/23) | Gemini cannot read image attachments outside workspace | Release candidate | Either fix Gemini image handling or document Gemini image attachments as unsupported. Claude/Codex image paths should not imply universal support. |
| [#24](https://github.com/arniesaha/drover/issues/24) | Verify ATX headings render on device | Verification item | Close with one physical-device smoke; not a code blocker. |

## First Public Release Blockers

- [ ] Resolve the #14 rename policy:
  - [ ] Decide whether storage-contract migration is in this release.
  - [ ] If yes, write a migration plan for `nexus.*` span attrs, stored
        `nexus_handoff` values, MCP compatibility aliases, and historical
        parquet/DuckDB reads.
  - [ ] If no, document which `nexus.*` names are internal compatibility
        contracts and exclude them from public-facing prose.
- [ ] Complete #4 public branding and sanitization:
  - [ ] Rename `apps/drover/NexusKit` to `DroverKit`, including package name,
        test target names, imports, `project.yml`, README, and CI commands.
  - [ ] Sweep current public-facing docs for stale "Nexus" references.
  - [ ] Keep historical migration records as historical, but label them clearly
        and keep them out of first-read public docs.
  - [ ] Add Traycer positioning to the README and north-star doc if the
        competitive framing still holds.
  - [ ] Run a secret/PII/path sweep before making the repository public.
- [ ] Fix #21 timestamp serialization so iOS relative times are credible.
- [ ] Resolve #13 before documenting any Funnel/public-hub setup:
  - [ ] Add per-host tokens and host-id binding, or
  - [ ] remove Funnel from public quickstart and keep it as private dogfood.
- [ ] Add CI gates for the public branches:
  - [ ] Python CI must pass on `main` and pull requests.
  - [x] iOS CI must build/test on `main`, `production`, and pull requests.
  - [ ] Protect `main` so required checks must pass before merge.
  - [ ] Treat `production` as release-candidate tracking, fast-forwarded or
        merged from `main`; no direct feature work.
- [ ] Produce a public demo path:
  - [ ] Local-only quickstart with `drover-server` and one local harness host.
  - [ ] Sanitized screenshots or demo data.
  - [ ] No private hostnames, LAN IPs, Tailscale names, tokens, or home paths.

## Release-Candidate Items

These should land before a polished public announcement but need not block a
private-to-public repo flip if the docs are honest.

- [ ] #12: live phone verification for auth flows on supported harnesses.
- [ ] #16: NAS relay mode, especially if the public demo uses multi-host relay.
- [ ] #23: Gemini image-attachment decision:
  - [ ] native image support,
  - [ ] allowed attachment directory,
  - [ ] workspace-local copy, or
  - [ ] documented unsupported status.
- [ ] #24: physical-device ATX heading smoke.
- [ ] #11: durable NAS boot behavior, if NAS remains part of public demo docs.

## Post-Public Backlog

- [ ] #15: OKF v0.2 assessment doc and follow-up adoption decisions.
- [ ] #17: session-row token usage rollup.
- [ ] #18: stale-host picker behavior.
- [ ] #20: APNs or third-party push path for timely background alerts.
- [ ] Context standards roadmap follow-ups:
  - [ ] OKF export/import for curated context.
  - [ ] Agent Behavior indexing and trace review.
  - [ ] Minimal relational context graph.
  - [ ] Code graph spike comparing CodeGraph and Graphify.

## Documentation Sweep

Public first-read docs should be Drover-first. Status after the 2026-08-05
prep pass:

- [x] `README.md`
- [ ] `docs/north-star.md`
- [x] `docs/architecture.md`
- [ ] `docs/multi-host.md`
- [x] `docs/agent-adoption.md`
- [x] `docs/context-standards-roadmap.md`
- [x] `docs/public-release-checklist.md`
- [ ] `apps/drover/README.md`; CI section is updated, but the file still has
      truthful `NexusKit` references until #4 renames the package.
- [ ] `scripts/claude/README-claude-hooks.md`
- [x] `docs/span-embeddings.md`
- [x] `docs/paperclip-completion-contract.md`
- [x] `docs/openclaw-adapter-contract.md`
- [x] `docs/install-shipper.md` as target-state prose; installer scripts and
      unit templates still need the #14 rename before publication.

Historical or migration records may keep "Nexus" when the old name is the
subject:

- `docs/porting-and-cutover.md`
- `docs/local-lakehouse-spec.md`
- `docs/decommission-gcp.md`
- `docs/handoffs/**`
- `docs/archive/**`
- historical `docs/superpowers/**` specs and plans

Internal compatibility references should be handled deliberately, not as prose
branding:

- `nexus.*` span attributes
- stored `nexus_handoff` / `nexus_control` values
- transition `nexus_*` MCP aliases, if still present for one release
- historical `~/.nexus` paths in migration records

## CI/CD And Branch Policy

### GitHub Actions

- [ ] Keep the existing Python CI workflow on Ubuntu for `main` and pull
      requests.
- [x] Add a separate iOS workflow on macOS:
  - [x] install XcodeGen,
  - [x] generate `Drover.xcodeproj`,
  - [x] run the `Drover` scheme tests on an iOS simulator,
  - [x] build `DroverUITests` for testing,
  - [x] upload result bundles on failure.
- [x] Trigger iOS CI on:
  - [x] pull requests to `main`,
  - [x] pushes to `main`,
  - [x] pushes to `production`,
  - [x] manual dispatch.

### Branches

- [ ] `main` is the integration branch and should be protected.
- [ ] `production` is a release-candidate branch cut from `main`.
- [ ] Do not land feature work directly on `production`.
- [ ] Require Python CI and iOS CI before merging to `main`.
- [ ] Require the same checks before promoting `main` to `production`.

## Public Release Gate

Before flipping the repo public:

- [ ] `git grep` confirms no accidental private hostnames, LAN IPs, Tailscale
      Funnel names, tokens, or personal backup paths in public-first docs.
- [ ] `rg "Nexus|nexus"` output is reviewed and every remaining hit is either
      historical, compatibility-contract, or tracked in #4/#14.
- [ ] Python CI passes.
- [ ] iOS CI passes.
- [ ] Local `drover-server` quickstart works from a fresh checkout.
- [ ] iOS simulator build/test works from a fresh checkout.
- [ ] Physical iPhone smoke covers:
  - [ ] connect to server,
  - [ ] list hosts/sessions,
  - [ ] launch or attach a session,
  - [ ] send a turn,
  - [ ] render headings,
  - [ ] show sane timestamps,
  - [ ] handle the documented notification behavior.
- [ ] README sets expectations clearly:
  - [ ] local-first,
  - [ ] single-user/personal fleet,
  - [ ] no hosted sandbox,
  - [ ] no enterprise RBAC/SSO,
  - [ ] no guaranteed background push without APNs or a relay.
