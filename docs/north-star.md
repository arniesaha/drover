# Drover — North Star

Status: design of record (brainstormed 2026-07-07). This is the umbrella
document: positioning, capability pillars, brand identity, and visual
direction. The rename, the design system, the OSS release, and the
capability roadmap all pull from here. When a downstream decision conflicts
with this doc, this doc wins unless amended here.

---

## 1. Philosophy

Drover exists because a solo builder now runs *several* coding agents —
Claude Code, Codex, Gemini — across *several* machines, and the experience is
a scatter: terminal tabs on a laptop, an SSH session to a NAS, no memory
between them, no way to glance at "what is everything doing right now" from a
phone. The agents are powerful; the *operations layer* around them is missing.

Drover is that layer, built on three convictions:

- **You should command the fleet, not babysit terminals.** Starting,
  steering, interrupting, and handing off agent work should feel like driving
  a herd — one cockpit, from anywhere, including your pocket.
- **Context is the moat, not the model.** Agents forget; Drover doesn't. The
  durable record of what happened — transcripts, summaries, decisions,
  handoffs — is what turns disconnected sessions into continuous work.
- **Local-first is a feature, not a limitation.** Your metal, your data,
  optionally your models. No required cloud, no lock-in, no per-seat SaaS.
  This is what makes it trustworthy *and* what makes it indie.

**Decision:** the primary promise that leads the brand is **Command** (control
+ mobility). Memory, local-first, and observability are supporting pillars,
not the hero. This choice sets the tagline, the README hero, and the mascot's
energetic/in-control personality.

## 2. Positioning

**One-liner:**
> Drover is the local-first cockpit for driving your personal fleet of CLI
> coding agents — Claude Code, Codex, Gemini — from anywhere.

**Positioning statement:**
> For indie builders who run several coding agents across their own machines,
> Drover is the self-hosted control plane + durable memory that turns a
> scatter of terminal sessions into one fleet you command from your pocket.
> Unlike a generic web terminal (no memory) or a cloud agent platform (not
> yours), Drover is local-first — your metal, your data, optionally your
> models — and it remembers everything across agents and sessions.

**Category:** personal agent-operations layer / meta-harness. Not an IDE, not
a model provider, not an agent framework.

## 3. Audience

The **indie builder**: solo dev or small-team hacker who already runs
Claude Code + Codex + Gemini, owns a machine or two (maybe a NAS or a GPU
box), wants to kick off and babysit agent work from a phone, and would rather
own their data than rent another SaaS.

**Explicitly NOT for** (this "not" list is a product feature — it keeps Drover
sharp and prevents scope creep toward a different, worse product):
- Enterprises needing RBAC, SSO, audit governance, or policy planes.
- Teams needing multi-tenant collaboration or shared cloud workspaces.
- Users wanting a hosted sandbox / "agents in someone else's cloud."
- People who don't already live in CLI agents — Drover wraps them, it does
  not teach them.

## 4. Capability pillars

Four pillars. Pillar 1 leads the brand; all four are the roadmap spine for
the capabilities track.

1. **Command** *(lead)* — start, attach, steer, interrupt, and hand off agent
   sessions across every host from a native phone app (and web). Multi-host,
   multi-harness, one cockpit. Structured sessions (headless JSON drivers)
   render as native chat with first-class approvals; a real terminal is the
   escape hatch.
2. **Memory** — durable transcripts, summaries, decisions, and searchable
   history; cross-harness handoff (continue a Claude session in Codex with
   context intact). You and your agents never lose the thread. This is the
   context plane (DuckDB + parquet lakehouse) inherited from Nexus.
3. **Local-first & yours** — runs on your own hardware, data in your own
   store, works with local models (Ollama) or hosted. No required cloud, no
   lock-in. A shared cluster token (bearer + signed cookie) secures every
   surface over LAN/Tailscale.
4. **Observability** — per-session attention states (needs-you / working /
   done / errored), quality signals, and a span/trace lakehouse. Know when to
   step in and when to go touch grass.

## 5. Brand identity

**Name: Drover.** A drover drives herds over long distances; you drive a fleet
of agents. Availability-checked clean (decision recorded on Nexus issue #199):
no funded product or agent-space collision. During transition, docs may say
"Drover (formerly Nexus)."

**Tagline / hero:**
- Hero line: **"Drive your whole agent fleet — from your pocket."**
- Short tagline: **"Drive your fleet."**
- Supporting one-liners in the wild: "Your agents are working. Go touch
  grass." · "Claude, Codex, Gemini — one cockpit." · "Handed off to Codex,
  context intact."

**Mascot: a working drover's dog.**
- **Decision:** the mascot is a working herding dog — Kelpie / Blue Heeler /
  Border Collie lineage (a "drover's dog" is a real thing). Alert, smart,
  energetic, keeps the herd (your agents) in line. It reads instantly as
  "this thing herds things for me."
- **Personality:** capable, tireless, loyal, a little cheeky. Calm under
  load, springs to action when the fleet needs you. Never corporate, never
  cutesy-to-the-point-of-toy.
- **Uses:** app avatar / launch icon, notification glyph, empty-state and
  loading illustrations, the face of the README and landing page. The dog can
  earn a name in a later pass (deferred — not blocking).
- **Concept + direction only** at this stage: no final logo art here. This
  briefs the asset creation (design-system sub-project). Logo direction: a
  clean, confident head silhouette or a "ready stance" full-body mark that
  works at favicon size and as an app icon.

**Voice / tone:** plainspoken, capable, a little wry — like a good stockman.
No hype, no corporate fluff, respects the builder's time. Prefers the concrete
("Three agents need you") over the abstract ("You have pending notifications").
Occasional dry humor is on-brand ("Go touch grass"); jokes never get in the
way of information.

## 6. Visual direction

**Decision:** warm & rugged — outback/craft temperature, keeping dev-native
credibility. Chosen deliberately to stand out against both the blue-SaaS crowd
and the green-terminal crowd, and to pair naturally with the dog mascot.

- **Palette direction** (tokens formalized in the design-system sub-project;
  these are the *intent*, not final hex):
  - Base: warm paper / cream (light); warm charcoal, not pure black (dark).
  - Primary: **ochre / rust** — the outback dirt, the dog's coat, energy,
    action. The "Command" color.
  - Secondary: **deep teal / forest** — calm, trustworthy, the "resting/OK"
    state.
  - Ink: near-black warm neutral for text.
  - Semantic → attention states: amber = needs-you, teal/green = working-fine,
    muted neutral = done, rust-red = errored. (These map directly onto the
    app's existing `AttentionState` enum.)
- **Type:**
  - UI: a warm **humanist sans** (readable, a little character — not cold
    geometric). Candidates to evaluate: Figtree, Hanken Grotesk, General Sans,
    Public Sans.
  - Agent output / terminal surfaces: one **monospace** (JetBrains Mono,
    Commit Mono, or Berkeley Mono). Keep it to exactly one sans + one mono.
- **Aesthetic keywords:** warm, crafted, rugged, self-reliant, calm-under-load.
  Subtle paper/canvas texture is allowed; rounded-but-not-bubbly corners;
  generous whitespace. The iOS app (currently vanilla SwiftUI) and the web UI
  both adopt this in the design-system pass.
- **Surfaces the brand applies to:** iOS app, web UI, docs + landing page,
  GitHub README, notification/app icons.

## 7. Narrative (the story we tell)

> You've got Claude Code refactoring an auth module on the Mac Mini, Codex
> chewing through a migration on the NAS, and a Gemini session you started
> this morning you've half-forgotten. Three terminals, two machines, zero
> memory between them. You're out for coffee.
>
> Your phone buzzes: *Claude needs you — approve `Bash(git push)`?* One tap,
> approved. You glance at the fleet: two working, one done. The done one's
> summary is already written and searchable. You hand it off to Codex to keep
> going — context intact, no copy-paste. You put the phone away.
>
> That's Drover. Your agents do the work. You drive.

This narrative is the spine for the landing page, the README hero, and the
demo script. It leads with **Command**, shows **Memory** (summary + handoff)
and **Observability** (attention states) as payoffs, and is quietly
**local-first** throughout (it's all your machines).

## 8. How this feeds the downstream sub-projects

- **Rename + relocate** (porting-and-cutover.md): uses the name, tagline, and
  positioning for repo description, package naming, and service labels.
- **Design system** (later spec): tokenizes §6 palette/type into the iOS app
  and web UI; produces the actual mascot art + logo from §5.
- **OSS release** (Nexus #177 → its own spec): README hero from §7, mascot
  from §5, voice from §5, plus sanitized tree, license, demo dataset,
  contributor docs.
- **Capabilities roadmap** (its own track): the four pillars in §4 are the
  spine; the context-plane features (handoff, brief injection, live
  cross-session awareness) are the near-term substance.
