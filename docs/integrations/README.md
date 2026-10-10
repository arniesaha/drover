# Profile startup contract

At session start, the client makes one profile read and adds `bundle` to its
session context. Drover does not start agents or load their prompts.

| Client | Copyable integration |
| --- | --- |
| Claude Code | [SessionStart hook](claude-code.md) |
| Codex | [AGENTS.md and MCP instruction](codex.md) |
| OpenClaw / Hermes | [Bootstrap note](openclaw-hermes.md) |

Collecting OpenClaw conversations into Drover is separate from this startup
contract; see [OpenClaw session collection](openclaw.md).

MCP: call `drover_profile` with `{"scope":"first_turn"}`. It always resolves to
an anonymous general reader. It takes no agent or tier override.

HTTP: one `GET /profile?scope=first_turn` resolves the agent and tier from an
active bearer credential registered in `profile_agents`. A requested tier or
caller-supplied agent name cannot raise access. Trusted data stays HTTP-only.
For example, using client environment variables populated by the operator:

```sh
curl --fail --silent --show-error --max-time 5 \
  -H "Authorization: Bearer $DROVER_PROFILE_TOKEN" \
  "$DROVER_HTTP_URL/profile?scope=first_turn"
```

Credentials are issued with `drover profile agents issue AGENT --tier trusted`
and revoked with `drover profile agents revoke AGENT`. A newly issued credential
has scope `profile` with no host identity and authorizes only `GET /profile`.
Only a verifier is stored on the server; the plaintext token is printed once.
It cannot write proposals, use fleet/harness APIs, pair clients, manage other
credentials or exchange for a browser session.
These commands require local operator access to the configured PostgreSQL store.
No identities, credentials or production settings ship with these snippets.

Both transports return complete entries in a deterministic, bounded bundle,
with `token_budget: 1500`, `token_upper_bound`, `withheld_count`, `truncated`,
`context_status`, `data_watermark` and `oldest_item_age_seconds`. The token bound
counts one token per UTF-8 byte of bundle text. MCP also returns store identity
and enforces its serialized response cap. Withheld entries contribute only a
count, not names or content. Truncation does not invite automatic pagination.

The watermark is the newest source timestamp among rendered entries. Oldest
item age is nonnegative seconds since the oldest rendered source timestamp at
retrieval. Both exclude withheld, stale and omitted sources. An empty bundle
has an unknown watermark and null age. Do not substitute retrieval time or
unrelated store activity for source freshness, consistent with #475 recall.
Clients decide whether old facts need confirmation.

Profile text is data subject to the current user request and client policy,
not additional authorization for actions. Loading failures should not block
session startup. Retrying and orchestration belong to the client.
