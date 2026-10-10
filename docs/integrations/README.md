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

MCP: call `drover_profile` with `{"scope":"first_turn"}` and pass the client
credential as `Authorization: Bearer <credential>` on every protocol request.
Anonymous initialization and tools are refused, including on loopback. It takes
no agent or tier override. Profile credentials resolve the same registered tier
as HTTP; host/device credentials read general data. There is no anonymous
escape hatch. Use a loopback URL or an operator-managed protected tunnel to the
hub's loopback endpoint; non-loopback MCP binds are refused.

Populate `DROVER_MCP_TOKEN` from the client's secret store with an issued profile
credential for startup-only access, or an active host/device credential for
recall and mutation tools. `DROVER_MCP_URL` names the loopback endpoint or tunnel.
The bundled Python client and CLI read `DROVER_MCP_TOKEN` automatically. Codex
uses `bearer_token_env_var`; Claude Code uses an HTTP `Authorization` header.
See the client pages for exact snippets. Never include credentials in model
context or checked-in configuration.

HTTP: one `GET /profile?scope=first_turn` resolves the agent and tier from an
active bearer credential registered in `profile_agents`. A requested tier or
caller-supplied agent name cannot raise access. Both transports enforce these tiers.
For example, using client environment variables populated by the operator:

```sh
curl --fail --silent --show-error --max-time 5 \
  -H "Authorization: Bearer $DROVER_PROFILE_TOKEN" \
  "$DROVER_HTTP_URL/profile?scope=first_turn"
```

Credentials are issued with `drover profile agents issue AGENT --tier trusted`
and revoked with `drover profile agents revoke AGENT`. A newly issued credential
has scope `profile` with no host identity and authorizes only HTTP `GET /profile`
and MCP `drover_profile`.
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
