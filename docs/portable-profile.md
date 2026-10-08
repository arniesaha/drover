# Portable user profile

Iteration 2 hardens import and adds operator commands and client startup examples.
Profile storage introduced in iteration 1 keeps durable profile facts and their
proposal history in Drover PostgreSQL, using migration **15**. Files remain read-only import sources.
See [ADR 0003](adr/0003-portable-profile-iteration-1.md) and the
[iteration 2 plan](plans/2026-10-08-portable-profile-iteration-2.md).

## Read a first-turn bundle

Call MCP `drover_profile(scope="first_turn")`, or HTTP `GET /profile`.
Scopes are `first_turn`, `full`, `user`, `work` and `decision`. Every scope
keeps the 1,500-token ceiling. `full` includes all layers within that ceiling.

Responses include `bundle`, `token_budget`, `token_upper_bound`,
`withheld_count`, `truncated`, `context_status`, `data_watermark` and
`oldest_item_age_seconds`. Age is nonnegative seconds since the oldest rendered
source timestamp, or null for an empty bundle. The watermark
uses only sources rendered in the bundle; MCP preserves it alongside
`store: "hub"` and `store_authoritative: true` for the PostgreSQL profile. The renderer conservatively
counts one token per UTF-8 byte, including headings, counts and truncation text.
This is an upper bound rather than a tokenizer estimate, so actual bundles can
be substantially smaller than 1,500 model tokens. Complete items are selected
in deterministic order: rules, other user items, work, decisions; newest first
within each priority, then stable ID. Oversized items are omitted, allowing
smaller later items to fit. `truncated` signals omissions.

Threads leave the bundle after 14 days without activity, decisions after 30 days.
Items exactly at the boundary remain eligible. Explicit expiry takes precedence.
User preferences and standing rules do not have an implicit expiry.

Work includes imported threads, current PostgreSQL project briefs and existing
context containers. Empty or unavailable context storage does not suppress
project briefs. `context_status` reports unavailable optional context storage.
Unclassified contexts are private; only code contexts with the existing
`session-summary-redacted` policy are general. Each source contributes at most
1,000 candidate rows; candidate limits also set `truncated`. Context withheld
counts describe the retrieved candidates, while profile-item counts cover all
eligible stored items. Freshness uses activity timestamps, not bundle retrieval.

## Agent tiers and identity

General readers see general items. Trusted readers also see trusted items.
Private readers see all three tiers. Hidden content is represented only by
`Withheld: N items.` No hidden titles, categories, bodies or IDs are rendered.

HTTP derives identity from an active bearer credential and the PostgreSQL
`profile_agents` registry. Unknown agents default to general. The operator can
issue and revoke registered credentials with `drover profile agents`; trusted
access requires explicit `--tier trusted`. Deployment identities are deliberately
absent from public fixtures
and source. No registry or production configuration is modified by installation
of this code alone.

The cluster operator bearer represents the user: it can read private content,
review proposals, approve changes, reverse changes and manage tier bindings.
Device, host and profile bearers cannot approve changes. Profile bearers issued
by `drover profile agents issue` authorize only `GET /profile`; they cannot write
proposals, use fleet/harness APIs, pair clients, manage credentials or mint
browser sessions. Browser cookies and authentication-
disabled requests receive general profile access. Do not put agent or reader
identity in a query parameter or proposal body; unsupported fields are rejected.

Bind an already issued, active `profile` credential with operator-authorized
`POST /profile/agents`:

```json
{"credential_id":"example-credential-id","agent_id":"example-agent","tier":"trusted"}
```

The existing MCP transport has no verified caller identity. Its profile reader
is always general and its proposals always start pending. Trusted agents use
credential-authenticated HTTP for trusted reads and automatic acceptance.

## Proposals, review and reversal

`POST /profile/proposals` and MCP `drover_profile_propose` accept:

```json
{
  "layer": "user",
  "kind": "preference",
  "tier": "general",
  "body": "Use tables for comparisons.",
  "session_id": "example-session"
}
```

Layers are `user`, `work` and `decision`. Kinds are short descriptive strings;
`rule` receives highest rendering priority. An optional `item_id` updates an
existing accessible item. Optional `expires_at` requires a timezone-qualified
ISO timestamp. Body text is limited to 65,536 UTF-8 bytes. Responses return
proposal ID, item ID, status and an idempotence flag, without echoing content.

Proposals from a registered trusted identity on an otherwise authorized transport
automatically accept unless the target tier is private. Issued profile credentials
are read-only and cannot call proposal routes.
Editing an existing private item also requires user approval; readers cannot
edit items they cannot access. All other proposals start pending. This includes
operator-created proposals, keeping approval explicit.

The operator can inspect `GET /profile/proposals?status=pending&limit=25`.
The queue includes proposed content and source identity. Allowed statuses are
pending, accepted, rejected and reverted; limits are 1-100. The oldest proposals
come first; `truncated` signals additional results. The queue is operator-only.

Submit an empty JSON object to any of these operator-only endpoints:

- `POST /profile/proposals/{proposal_id}/accept`
- `POST /profile/proposals/{proposal_id}/reject`
- `POST /profile/proposals/{proposal_id}/revert`

Accept and reject apply only to pending proposals; revert applies only to
accepted proposals. Accepted items record proposing agent, source session,
acceptance time, approving actor and proposal ID. Proposal rows retain snapshots
for reversal and acceptance provenance after reversal. Reverting a new item removes it from subsequent bundles; reverting
an update restores the earlier contents and freshness timestamp. Revisions keep
increasing. Reversal remains in the audit history.

Conflicting approvals or reversals return HTTP 409. A reversal cannot overwrite
a subsequent edit. Submit a new proposal against the current item in that case.
Approval scope failures return 403; invalid inputs return 400.

## Markdown import

```sh
drover profile import --from examples/USER.md
drover --config examples/config.toml profile import --from examples/MEMORY.md --tier trusted --apply
```

`drover-server profile import` supports the same options. Dry-run is the default
and reads no configuration or database. It prints the parsed sections for local
review. A directory imports its markdown files recursively in sorted order,
with a limit of 100 files, each at most 2 MiB. Source files are never edited.

ATX and setext headings define section items. Code-fenced headings remain
content. Every item defaults to private, including recognized categories,
unknown categories and unheaded notes. `--tier general` or `--tier trusted`
sets an explicit visibility ceiling; headings can only restrict it.
`--tier private` is also accepted. Sensitive ancestor headings force private for the
entire subtree: health, medical, finance, finances, financial, money, job-search,
personal, tax, income, salary, compensation, address, location, dating,
relationship, career, interview, offer, resignation, therapy, pet health,
portfolio, investment and banking. A later sibling restores the parent's
classification ceiling.

The section text, including its heading path and code fences, is scanned for
obvious street addresses, IPv4/IPv6 addresses, currency amounts, phone numbers
and email addresses. Any hit forces private. These conservative heuristics can
produce false positives and cannot detect every sensitive fact; unmatched text
still defaults to private. Dry runs include `key` (the heading path) and
`tier_reasons` for each item. Reasons are retained in proposal history and in
accepted item provenance as `import_classification`.

With `--apply`, new general/trusted items are explicitly approved by the local
operator import action. Private sections and changed versions of accepted
sections remain pending in the review queue. Exact reruns add no duplicates,
including reruns of pending, rejected or reverted proposals. Content fingerprints
and source-section keys are stored as hashes; host paths are not copied into
profile bodies or provenance. Moving the same file changes its source identity.
Deleted sections are not automatically deleted from PostgreSQL.

## Boundaries

This iteration adds no UI, schedules, personas or host-file writers. Copyable
client startup examples live in [docs/integrations](integrations/README.md);
clients own orchestration and configuration.
It requires PostgreSQL for profile persistence. Proposal writes are included
now, overriding the design artifact's v2 deferral. Context containers remain a
derived work source rather than a second authoritative profile store.

## Operator commands

These commands act on the configured PostgreSQL store using local operator
access. They do not require a running HTTP server. No import or credential
issuance happens without an explicit command.

```sh
drover profile review PROPOSAL_ID accept
drover profile review PROPOSAL_ID reject
drover profile review PROPOSAL_ID revert
drover profile set-tier ITEM_ID --tier general --reason "Reviewed for sharing"
drover profile set-tier ITEM_ID --tier private --reason "Restrict visibility"
drover profile agents issue AGENT_ID --tier trusted
drover profile agents revoke AGENT_ID
```

Default imports remain pending. Accept a private proposal before changing its
item tier. `set-tier` promotes or demotes an active item and immediately accepts
an operator proposal. It retains the old snapshot, revision, actor, time,
classification evidence and reason. Its returned proposal can be reverted,
subject to the normal revision conflict check.

Credential issuance defaults to general. It atomically stores a verifier in
`control_credentials` with scope `profile` and `host_id` null, and binds it to
the agent in `profile_agents`, recording
`updated_by` and `updated_at`. The token is printed once; store it securely in
the client. An agent with an active credential must be revoked before reissue.
Revocation invalidates the bearer and removes trusted access, retaining the
credential's revocation timestamp and registry operator metadata. It is
idempotent. Agent credentials cannot be issued at private tier by this command.
Trusted sessions use HTTP, since MCP has no verified caller identity. The scope
allowlist permits only `GET /profile` (including supported scope queries). Agent
identities do not enter the host namespace, and retiring a same-named host does
not revoke a profile credential. Operators can also list and revoke profile
credentials through the generic credential endpoints; profile tokens cannot
access those endpoints or revoke other credentials.
