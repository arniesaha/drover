# OpenClaw adapter contract

Historical tracking: Nexus [#106](https://github.com/arniesaha/nexus/issues/106), [#107](https://github.com/arniesaha/nexus/issues/107), [#108](https://github.com/arniesaha/nexus/issues/108), [#134](https://github.com/arniesaha/nexus/issues/134); AgentWeave [#187](https://github.com/arniesaha/agentweave/issues/187), [#216](https://github.com/arniesaha/agentweave/issues/216), [#217](https://github.com/arniesaha/agentweave/issues/217).

Implementation handoff docs:

- [`openclaw-agentweave-implementation-plan.md`](openclaw-agentweave-implementation-plan.md)
- [`openclaw-agentweave-schema-evolution.md`](openclaw-agentweave-schema-evolution.md)
- [`openclaw-agentweave-validation-matrix.md`](openclaw-agentweave-validation-matrix.md)

## Purpose

OpenClaw is the runtime and harness: it owns hooks, plugins, sessions,
subagents, channels, command lifecycle, and message flow. Drover should not
scrape private OpenClaw internals or assume a single on-disk layout. Drover
should consume a stable adapter contract that can be produced by any of these
paths:

1. native OpenClaw JSONL/session exports,
2. an OpenClaw hook or plugin that emits normalized Drover events,
3. AgentWeave spans from the OpenClaw bridge, or
4. a combination of native events and provenance spans.

Drover is the local context server and session archive. It stores, links,
summarizes, embeds, and recalls the facts OpenClaw and AgentWeave emit; it does
not become an OpenClaw runtime, plugin host, or tracing dashboard.

## Contract goals

- Keep native OpenClaw runtime details behind an adapter boundary.
- Make OpenClaw session identity joinable across native session events and
  AgentWeave spans once AgentWeave #187 lands.
- Preserve the distinction between canonical session UUIDs and route/session
  keys used by OpenClaw's live runtime.
- Carry enough attribution to power handoff and recall even when only one source
  is available.
- Keep sensitive channel/source and prompt/response data redacted or omitted
  unless explicitly safe.

## Event envelope

Every OpenClaw-derived event accepted by Drover should normalize to the regular
`AgentEvent` shape plus OpenClaw-specific fields in `raw_data`.

Required top-level fields:

- `id`: stable source event id, or an adapter-generated deterministic id.
- `session_id`: the canonical OpenClaw session UUID when known.
- `timestamp`: source event time in UTC.
- `agent_id`: stable agent identifier, such as `nas-openclaw` or an OpenClaw
  agent id from the runtime event.
- `event_type`: one of the normalized event types below.
- `raw_data.harness`: always `openclaw`.

Recommended top-level or first-class ingest columns, where available:

- `repo_owner`, `repo_name`, `branch`: derived at collect time from `cwd` or
  provided `repository` metadata.
- `task_id`: explicit user/task id if present; otherwise Drover may derive one
  from repository attribution.

OpenClaw-specific `raw_data` fields:

```json
{
  "harness": "openclaw",
  "harness_version": "0.0.0",
  "runtime_id": "openclaw:<host-or-install-id>",
  "runtime_api": "diagnostic-events/v1",
  "session_uuid": "018f4c2a-...",
  "session_key": "agent:main:subagent:abc",
  "parent_session_uuid": "018f4c2a-parent-...",
  "parent_session_key": "agent:main:main",
  "agent_id": "main",
  "agent_type": "primary|subagent|tool|unknown",
  "channel": "terminal|web|api|unknown",
  "source_surface": "cli|plugin|hook|webhook|unknown",
  "cwd": "/path/to/repo",
  "workspace_dir": "/path/to/workspace",
  "repository": "https://github.com/owner/repo.git",
  "project": "owner/repo",
  "topic": "short human task label",
  "event_name": "message.queued",
  "redaction": {
    "level": "none|preview|redacted|sensitive",
    "fields": ["message.content"],
    "preview_bytes": 2000
  },
  "provenance": {
    "trace_id": "...",
    "span_id": "...",
    "parent_span_id": "...",
    "source": "agentweave"
  }
}
```

Adapters may include additional source fields in `raw_data`, but consumers
should depend only on this contract.

## Stable identity fields

### Canonical session UUID

`session_id` and `raw_data.session_uuid` should be the canonical OpenClaw
session UUID that native OpenClaw session exports use. This is the durable
archive key in Drover.

If the native event does not expose the UUID yet, the adapter should:

- leave `session_id` as the best available stable id,
- set `raw_data.session_uuid_missing = true`, and
- preserve `raw_data.session_key` so future reconciliation can map the event.

### Session key / route key

`raw_data.session_key` is the OpenClaw runtime route/session key, for example
`agent:main:main` or `agent:main:subagent:abc`. It must not overwrite the
canonical session UUID. AgentWeave #187 tracks the same distinction for spans:
`prov.session.id` should become the canonical UUID, while the route key should
remain available as `prov.session.key`.

### Parent/child links

Subagent and delegated sessions should carry both durable and route-key links
when known:

- `raw_data.parent_session_uuid`
- `raw_data.parent_session_key`
- `raw_data.child_session_uuid` for lifecycle events that announce a child
- `raw_data.child_session_key` for lifecycle events that only know the route key

Drover session-link reconciliation should prefer UUID-to-UUID links, then fall
back to session-key mapping for partial data. Missing links should not block
native event ingest.

## Normalized event types

Adapters should map OpenClaw runtime event names to this small vocabulary while
preserving the source event name in `raw_data.event_name`.

- `session_start`: session created, loaded, or attached.
- `session_end`: session closed, reset, archived, or otherwise completed.
- `user_turn`: user-authored message or command.
- `assistant_turn`: assistant response content.
- `tool_call`: tool invocation request.
- `tool_result`: tool result or observation.
- `command`: OpenClaw command lifecycle such as `command:new` or
  `command:reset`.
- `lifecycle`: bootstrap, plugin, gateway, hook, or session state events that
  are useful for context but are not conversational turns.
- `error`: runtime, plugin, hook, or bridge error.
- `unknown`: accepted but not interpreted; kept for forward compatibility.

Suggested mappings:

- `session:start`, `agent:bootstrap`, relevant `session.state` start events ->
  `session_start` or `lifecycle` depending on semantics.
- `message.queued` with a user/source role -> `user_turn`.
- `message.processed` with assistant content -> `assistant_turn`.
- OpenClaw tool diagnostics -> `tool_call` / `tool_result`.
- `command:new`, `command:reset` -> `command`.

## Attribution fields

OpenClaw events should carry attribution at the point where paths are still
resolvable, usually on the host running `drover-collect` or in the OpenClaw
plugin/hook itself.

Preferred fields:

- `raw_data.cwd`: current working directory for the turn/session.
- `raw_data.workspace_dir`: OpenClaw workspace root when distinct from `cwd`.
- `raw_data.repository`: remote URL or `owner/repo` string.
- `raw_data.project`: stable project key, preferably `owner/repo`.
- `raw_data.topic` or `raw_data.task_label`: human task label if supplied.
- `_repo_owner`, `_repo_name`, `gitBranch`: Drover collect-time enrichment keys.

If both `cwd` and `repository` are present, repository metadata wins for
project identity, while `cwd` remains useful for files-touched and replay.

## Channel and source safety

Channel/source fields are useful for handoff but can leak private deployment
shape. Adapters should prefer coarse labels:

- `channel`: `terminal`, `web`, `api`, `webhook`, `unknown`.
- `source_surface`: `cli`, `plugin`, `hook`, `scheduler`, `unknown`.

Avoid storing private hostnames, LAN IPs, terminal titles, webhook secrets, raw
headers, or full user identifiers unless the deployment explicitly opts in.

## Redaction and sensitivity

OpenClaw adapters should mark payload sensitivity rather than forcing Drover to
guess later.

Recommended flags:

- `raw_data.redaction.level`: `none`, `preview`, `redacted`, or `sensitive`.
- `raw_data.redaction.fields`: JSON-path-like field names that were omitted or
  changed.
- `raw_data.redaction.preview_bytes`: maximum preview size emitted.
- `raw_data.sensitivity`: optional tags such as `secret`, `credential`,
  `customer_data`, `private_path`, or `unknown`.

Drover should store raw payloads only when the source marks them safe. Prompt,
response, tool input, and tool output previews should be truncated before they
reach embedding or summary jobs.

## AgentWeave provenance links

When an OpenClaw event has matching AgentWeave provenance, include:

- `raw_data.provenance.trace_id`
- `raw_data.provenance.span_id`
- `raw_data.provenance.parent_span_id`
- `raw_data.provenance.source = agentweave`

When only AgentWeave spans are available, Drover should still ingest the spans as
provenance facts and later link them to native OpenClaw events by
`session_uuid`, `session_key`, timestamp window, and agent id. When only native
events are available, Drover should still provide replay and handoff without
trace/cost details.

## Join strategy

Preferred join order:

1. `agent_events.session_id = spans.session_id`, where both are the canonical
   OpenClaw UUID.
2. `raw_data.session_key = spans.session_key` for data produced before
   AgentWeave #187 is resolved.
3. `(agent_id, parent_session_id, timestamp window)` for partial subagent spans.
4. Repo/day fallback for aggregate project activity only, not exact replay.

The join should be additive. A bad or missing AgentWeave span must not corrupt
native session context, and a missing native event stream must not drop valid
usage/provenance facts.

## Adapter health checks

Read-only checks should report:

- recent OpenClaw native events observed,
- recent OpenClaw AgentWeave spans observed,
- percent of events/spans with canonical session UUIDs,
- percent with `session_key` separate from UUID,
- percent with repo/project attribution,
- percent with parent links for subagent sessions,
- preview/redaction policy in effect,
- sample unmatched native sessions and unmatched spans.

These checks prove flow and linkability without requiring private runtime
details.

## Compatibility notes

- AgentWeave #216 tracks verification against current OpenClaw diagnostic APIs
  including `onDiagnosticEvent`, `onModelDiagnosticEvent`, `message.queued`,
  `message.processed`, `session.state`, and `model.usage`.
- AgentWeave #217 tracks preserving live Mux routing while adding context attrs
  such as `prov.cwd` and `prov.repository`; Drover should ingest Mux routing
  facts as evidence, not become the router.
- Older OpenClaw runtimes may lack canonical UUIDs or cwd/repository attrs. The
  adapter should mark those fields missing and continue ingesting safely.
