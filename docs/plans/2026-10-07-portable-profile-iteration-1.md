# Portable profile iteration 1 implementation plan

Authority: ADR 0003 and issue #545. Read the design branch artifact
`docs/design/portable-profile.html`; the requested `.src.html` is absent there.
Agent proposal policy references below are updated for iteration 2; see the
[current contract](../portable-profile.md#proposals-review-and-reversal) for the
legacy registry binding exception.

## Commit sequence and file exhibits

1. ADR alone, then this plan alone. Validate markdown links and whitespace.
2. Append PostgreSQL migration **15** at `src/drover/server/postgres_schema.py:546`.
   `PROFILE_MIGRATION = 15` is the single version constant. Main includes lifecycle
   migrations 13/14; preserve their released DDL and hash pins. The combined
   registry covers 1-15, including conditional pgvector migration 8.
   Never change migrations 1-14. Add profile items, proposals with before/after
   snapshots and actors, and credential-bound agent tiers. Pin the new hash in
   `tests/test_postgres_schema_migrations.py:23`. Test fresh bootstrap, upgrade
   from 14, direct DDL rerun and recorded migration rerun in disposable PostgreSQL.
3. Add `src/drover/server/profile.py:1` for tier resolution, filtering and rendering.
   Register `drover_profile(scope="first_turn")` at
   `src/drover/server/mcp/server.py:58`, with a READ_CAPS entry at
   `src/drover/server/mcp/contract.py:27`. Expose GET `/profile?scope=first_turn`
   through `src/drover/server/web/app.py:932`. Allowed scopes: first_turn, full,
   user, work, decision. All remain bounded to 1,500 tokens. Return bundle,
   conservative token upper bound, withheld count and truncation flag.
   Use UTF-8 bytes as a conservative token upper bound; no tokenizer dependency.
   Deterministic order: standing rules, user preferences, active work, decisions;
   newest first within priority, stable IDs break ties. Omit whole items that
   cannot fit, reserving space for counts. Do not return hidden IDs or bodies.
   Read PostgreSQL project briefs (`postgres_schema.py:370`) and existing
   context readers (`mcp/tools.py:1017`), including when containers are empty.
   Unclassified non-code contexts are private; redacted code contexts are general.
4. Add POST `/profile/proposals` with layer, kind, tier, body, optional item_id,
   session and expiry. Add MCP `drover_profile_propose` with the same shape.
   Identity and tier are derived from verified credentials, never body arguments.
   POST `/profile/proposals/{id}/{accept,reject,revert}` is operator-only.
   POST `/profile/agents` binds credential_id, agent_id and tier, operator-only.
   Current agent bindings require read-only profile credentials. Proposals from
   the current HTTP/MCP transport setup remain pending until operator approval.
   Transactions lock target items; snapshots and revision checks prevent stale
   accept/revert from overwriting subsequent changes. Return IDs/status only.
5. Add `src/drover/server/profile_cli.py:1`, register at
   `src/drover/server/__main__.py:878`, and a `drover` script in `pyproject.toml:53`.
   `drover profile import --from <path>` reads markdown, dry-run by default;
   `--apply` submits proposals through PostgreSQL, using local user authority.
   Ordinary sections default general or explicit trusted; health, finance,
   job-search and personal headings (including descendants) force private.
   Content hashes make repeated imports idempotent. Never write source files.
6. Document APIs, credential setup, import, approvals, reversal and limitations
   in `docs/portable-profile.md`; link from `docs/mcp.md`.

## Verification

Foreground scoped pytest runs after each code commit: migration contracts;
profile read tests for general/trusted/private filtering, zero-container briefs,
expiry, 14/30-day boundaries, Unicode budget and deterministic truncation;
write tests for credential authority, trusted read isolation, pending proposals,
provenance, reject, revert, conflicts and HTTP/MCP registration;
CLI tests for dry-run, apply, idempotence and sensitive nested headings.
Run black/isort on touched Python, markdown links, public release scan and
`git diff --check`. No push, PR, merge, deployment or host configuration edits.

## Design differences

The authoritative scope moves proposals from v2 into iteration 1. The public
registry is provisioned through operator-only credential bindings rather than
shipping private agent names. Existing MCP lacks authentication and stays general.
Persona rows and hooks are deferred; the requested schema models user/work/decision.
Context containers are derived reads, not copied profile facts. Missing analytical
context storage must not suppress PostgreSQL project briefs.
