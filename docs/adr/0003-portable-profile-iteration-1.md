# ADR 0003: Portable profile iteration 1

Status: accepted. Date: 2026-10-07. Authority: issue #545 decisions comment.

1. First-turn bundles have a 1,500-token budget, including headings and withheld counts.
2. Threads leave the bundle after 14 days without activity; decisions after 30 days.
3. Tiers are general, trusted and private. The two designated agents in the issue
   decisions are trusted; every other agent defaults to general. Private content
   is available only to explicitly private readers. Other readers receive only a
   withheld count, without titles or categories. Public artifacts use example agents.
4. Drover PostgreSQL is authoritative. Markdown files are import sources only.
5. Trusted agents' proposals auto-accept unless they target private content.
   Private changes require user approval. Accepted changes record agent, session
   and time, and can be reverted.

Credential bindings belong in the PostgreSQL agent registry, provisioned by the
operator, rather than embedding deployment identities in public source code.
Unauthenticated MCP has no agent identity and therefore receives general access.
The existing cluster operator bearer represents the user for approvals and
private reads. Authentication-disabled HTTP grants no approval or private access.

The requested write path is included now, superseding the design's v2 deferral.
UI, hooks, schedules, deployment and editing import files are outside this iteration.

Migration coordination: profile persistence uses migration 15, defined by
`PROFILE_MIGRATION` in `src/drover/server/postgres_schema.py`. Lifecycle migrations
13 and 14 are on main and retain their released DDL and hash pins. The combined
registry covers versions 1 through 15, including conditional pgvector migration 8.
