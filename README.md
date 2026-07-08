# Drover

> Drive your whole agent fleet — from your pocket.

**Drover** is the local-first cockpit for driving a personal fleet of CLI
coding agents (Claude Code, Codex, Gemini) across your own machines, from
anywhere. It is the rebrand and clean re-seed of the project formerly called
**Nexus**.

This directory (`/Volumes/M2 1/drover`) is the **new home** for Drover. It
starts as a handoff-docs seed; a fresh working session in this path will turn
it into the real repository and execute the port from Nexus.

---

## Start here (for the fresh session)

Read these in order, then decide what to build first:

1. **[docs/north-star.md](docs/north-star.md)** — philosophy, positioning,
   audience, the four capability pillars, brand identity (name, tagline,
   mascot, voice), and visual direction. The umbrella every downstream
   decision pulls from.
2. **[docs/porting-and-cutover.md](docs/porting-and-cutover.md)** — what
   migrates from Nexus (the DuckDB context layer + every interface worth
   keeping), the `nexus→drover` rename map with compatibility aliases, and
   the device-by-device cutover plan for the Mac Mini and the NAS. Includes
   the destructive/live-service steps clearly flagged.

The design decisions and their rationale are recorded inline in both docs
(look for **Decision:** callouts) so nothing is re-litigated.

## Repo strategy (decided)

**New repository, cleanly seeded from Nexus — not a rename-in-place.**

Rationale (full version in porting-and-cutover.md §1):
- The OSS release (Nexus issue #177) *requires* a sanitized-history reset —
  the Nexus git history carries private hostnames, personal paths, dogfood
  memory, and possibly tokens in old commits, which can never be made public.
- We are relocating to `/Volumes/M2 1/drover` regardless (fresh clone + venv
  rebuild happens either way), so this is the cheapest moment to do the clean
  seed — once, now, instead of twice later.
- A new repo lets Drover be branded from commit 1 with the new package
  structure (`src/drover/`, `apps/drover/`) and no legacy baggage.
- Nothing is lost: the `arniesaha/nexus` repo stays as a **private archive**
  and keeps running the live fleet until each device is cut over.

The runtime "compat aliases" (`nexus-*` CLI shims, `NEXUS_API_TOKEN`
acceptance, `~/.nexus` symlink) are code/config in the new repo — unrelated
to git history — and exist only to make the device cutover non-breaking.

## What is NOT in this handoff

- No code, no migration, no service changes have been executed. These are
  design + porting **specifications**. The fresh session implements them,
  gated behind the usual plan→build→review flow.
- Live services still run from the Nexus checkout (`~/jenny/nexus`) against
  `/Volumes/M2 1/nexus`. Do not decommission them until the cutover steps in
  porting-and-cutover.md §5 are done and verified per device.

## Provenance

Seeded 2026-07-07 from Nexus at `~/jenny/nexus` (main @ `27588ed`, the merge
of `worktree-drover-ios-cockpit` — supersedes the `5614750` pin in the first
draft of these docs; the merge carries the iOS-cockpit fixes this spec says to
keep). Git author for all Drover work: `arniesaha <arniesaha@gmail.com>`.
