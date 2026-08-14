# Native Harness Model Catalog

Status: Approved

Date: 2026-08-14

## Context

Drover's iOS client currently owns static model and reasoning-effort lists in
`HarnessRunPreferences`. That list has already drifted from the installed
harnesses: Codex offers `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`, but
Drover only shows a subset. The same problem will recur whenever Codex, Claude
Code, or agy changes its catalog, account entitlements, aliases, or supported
reasoning levels.

The model list is not a fleet-global property. It depends on the selected host,
the harness binary and version installed there, its provider configuration, its
policy, and the account authenticated on that host. The catalog must therefore
be discovered where the harness runs and normalized for Drover clients.

This design requires one bootstrap application update. After that update,
future model and reasoning-effort additions must appear without another iOS
release.

## Goals

- Discover the choices offered by Codex, Claude Code, and agy on the selected
  host using that harness's authenticated account and effective configuration.
- Give all clients one versioned, forward-compatible catalog contract.
- Drive both model and model-specific reasoning-effort controls from the
  catalog.
- Keep the picker responsive by showing cached data immediately and refreshing
  it without blocking interaction.
- Retain a last-known-good catalog when live discovery fails, with an explicit
  stale indication.
- Always allow the harness to choose its own default without sending an
  override.
- Remember the user's selection independently for every host and harness.
- Pass provider model and effort identifiers through unchanged.

## Non-goals

- Drover will not define a universal list of model families or reasoning
  levels.
- Drover will not union catalogs across hosts or accounts.
- Drover will not guarantee that a provider keeps a model available after a
  session has launched.
- This work will not make Claude Code change model or effort inside an existing
  persistent session. Its startup preferences remain locked.
- The first version will not expose every optional provider dimension, such as
  service tier, context length, price, or input modality. The schema may add
  those fields later.
- Shell and third-party harnesses are not required to support discovery. They
  continue to offer only Harness default until they implement an adapter.

## Alternatives considered

### Keep model lists in the app

This is the current design. It is simple but guarantees release-time drift,
cannot reflect account policy, and directly conflicts with the requirement that
new harness models need no app update.

### Discover models centrally

The Drover server could run provider queries itself. That server does not
necessarily share the selected host's binary, credentials, environment,
enterprise policy, or gateway configuration. A central result could therefore
offer a model that the target harness rejects or omit one it can use.

### Discover on each harness host and normalize the result

This is the selected design. `harnessd` owns one discovery adapter per harness.
The central server proxies and caches the normalized catalog, while the iOS
client renders the contract generically. It adds an adapter boundary but keeps
authority beside the runtime that will ultimately receive the selection.

## Architecture

```text
iOS model picker
    |
    | GET catalog for selected host + harness
    v
Drover server
    |  - authenticates the client
    |  - proxies to the selected host
    |  - persists the last-known-good response
    v
selected host's harnessd
    |  - identifies the effective account/configuration scope
    |  - runs a bounded native discovery adapter
    |  - normalizes model and effort metadata
    v
Codex app-server | Claude provider/configuration | agy CLI
```

Catalog discovery is a dedicated endpoint rather than part of the normal host
heartbeat. Discovery may touch credentials, start a short-lived process, or use
the network; it must not add that latency or payload to every fleet refresh.

## API contract

The public route is:

```text
GET /harness/hosts/{host_id}/model-catalog?harness={harness}&refresh={0|1}
```

The central server forwards it to the selected host as:

```text
GET /model-catalog?harness={harness}&refresh={0|1}
```

Both route parsers must use the same quoting, response-size bounds, bearer-token
forwarding, and relay support as the existing host auth and native-session
proxies.

A successful or safely degraded response has this shape:

```json
{
  "schema_version": 1,
  "host_id": "mac-mini",
  "harness": "codex",
  "account_scope_id": "opaque-local-scope",
  "harness_version": "0.147.0",
  "discovered_at": "2026-08-14T18:22:00Z",
  "stale": false,
  "stale_reason": null,
  "models": [
    {
      "id": "gpt-5.6-terra",
      "display_name": "GPT-5.6 Terra",
      "description": "Balanced model for everyday coding",
      "is_default": false,
      "reasoning": {
        "supported": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "default": "medium"
      }
    }
  ]
}
```

Contract rules:

- `schema_version` versions the envelope. Clients ignore unknown fields.
- Model IDs, effort IDs, display names, and descriptions are data, never app
  enums.
- `account_scope_id` is opaque and contains no email, organization name, token,
  or reversible account identifier. It lets clients detect a scope change and
  lets caches avoid treating different authenticated accounts as equivalent.
  It is `null` only when discovery has never succeeded.
- `discovered_at` and `harness_version` are also nullable only in that
  never-succeeded degraded envelope.
- `is_default` describes a native named model when the harness reports one. It
  does not replace the separate Harness default choice. At most one valid
  entry may be marked as the named default.
- `reasoning` may be absent. `supported` may contain future identifiers that the
  current app has never seen. `default` must either be in `supported` or be
  absent.
- Unknown or malformed individual models are discarded. If normalization
  produces no valid entries, discovery is considered failed rather than
  publishing an empty catalog as a successful refresh.
- The response is bounded by model count, string length, and total bytes before
  it is persisted or forwarded.

`refresh=1` bypasses freshness TTLs but does not discard last-known-good data.
It is used by the explicit Retry action, not by every picker presentation.

Unknown hosts and disabled harnesses return normal `404` errors. A known,
enabled harness whose discovery fails returns `200` with the last-known-good
models, `stale: true`, and a safe `stale_reason`. If no catalog has ever
succeeded, it returns the same envelope with `models: []`; the client still
offers Harness default. Safe reason categories are `offline`, `timeout`,
`not_authenticated`, `unsupported`, and `protocol_error`. Provider response
bodies, command stderr, paths, and credential details never cross this API.

## Harness discovery adapters

All adapters implement the same internal interface and return the normalized
catalog plus an opaque account/configuration scope. They run with an explicit
timeout, cancellation, output-size limit, and executable allowlist. Discovery
does not use an arbitrary shell command or accept a command from the client.

### Codex

The Codex adapter starts the installed `codex app-server` and calls its v2
`model/list` JSON-RPC method. It requests non-hidden models, follows pagination,
and maps the response's model ID, display metadata, default flag, default
reasoning effort, and supported reasoning efforts. The adapter records the
Codex version and rejects a partially framed or oversized response.

`~/.codex/models_cache.json` may be used only as a last-known native source when
app-server discovery is unavailable. It is not a Drover-authored fallback list,
and its result is marked stale unless the cache metadata proves it is current.

### Claude Code

The Claude adapter must represent the choices available to the installed
Claude Code process, not a Drover-maintained alias list. It resolves the same
effective provider, authenticated account, environment, and merged settings
that Claude Code uses. Its native sources include the provider model inventory
(`GET /v1/models` for Anthropic API and compatible gateway configurations) and
the documented Claude Code selection policy: `availableModels`, model aliases,
model overrides, custom model options, and provider-specific pinned models.

The adapter applies restrictions before returning the catalog. It obtains
effort levels from the installed CLI/provider capabilities and omits the
reasoning object for a model when support cannot be established. It must not
guess support from a model name.

Because Claude Code does not currently expose a documented machine-readable
`models` CLI command, the adapter requires conformance coverage against the
same host's `/model` picker for direct Anthropic subscription/API auth and any
configured Bedrock, Vertex, Foundry, or LLM gateway path. A source that cannot
be reconciled with the effective picker fails safely to last-known-good data or
Harness default; it does not fall back to a static list.

References:

- https://code.claude.com/docs/en/model-config
- https://platform.claude.com/docs/en/api/models/list

### agy

The agy adapter executes `agy models` directly with a bounded timeout and parses
its tab-separated model ID and display name output. A future structured output
mode should replace text parsing automatically inside the adapter without any
API or application change.

agy currently includes reasoning tiers in many model IDs, and Drover's agy
driver does not forward a separate effort override. The initial adapter
therefore omits `reasoning` for those entries. If a future agy version exposes
model-specific effort capabilities and the driver forwards them, the adapter
can populate `reasoning` without changing the client.

## Cache and refresh behavior

There are three deliberately different pieces of state:

1. `harnessd` keeps a short-lived discovery result to avoid repeatedly starting
   a CLI or making provider calls while the picker is reopened.
2. The central server persists the last successful normalized catalog per host,
   harness, and opaque account scope. This is the authoritative
   last-known-good cache shared by all Drover clients.
3. The iOS client keeps its most recently displayed catalog so opening the
   picker never waits on network I/O.

The app loads its local copy immediately, requests the selected pair, and
replaces the display only if the response still matches the current host and
harness. It refreshes when:

- the host or harness selection changes;
- the picker opens and the displayed catalog is older than five minutes;
- an auth flow completes;
- the user taps Retry.

Normal requests may reuse a harnessd result for up to five minutes. The central
proxy has a bounded live-request timeout. On timeout or transport failure it
returns its persisted catalog as stale. A forced refresh bypasses the five
minute TTL but retains the old catalog until a new response has been fully
validated and stored.

An authentication change, harness binary version change, or relevant provider
configuration change invalidates freshness but does not delete last-known-good
data. The next response carries the new `account_scope_id`; the app then clears
any incompatible selection before showing the new catalog. If the host is
offline, the central server cannot re-confirm the account scope. It returns the
most recently successful catalog for that host and harness, labels it stale,
and does not present the scope as currently verified.

## iOS behavior

The bootstrap app removes static model and effort suggestions from
`HarnessRunPreferences`. It adds Codable catalog types whose identifiers are
plain strings and a loader keyed by the selected host and harness.

The model control becomes a searchable sheet rather than a fixed `Menu` so it
can show display names, IDs, descriptions, loading state, and stale status. Its
first row is always **Harness default**. This row stores an empty selection and
sends `model: null`, allowing the harness's account-specific default to evolve.

The effort control is derived from the selected catalog entry:

- No `reasoning` metadata means no separate effort control.
- For Harness default, the control uses the single model marked `is_default`
  when one exists. If the harness does not resolve its default to a named model,
  the only available effort behavior is Auto.
- The first choice is **Auto**, displayed with the reported native default when
  available, for example `Auto (Medium)`. Auto sends `thinking_effort: null`.
- Explicit effort choices use the catalog strings unchanged.
- Changing model clears an explicit effort that the new model does not support.
- Unknown effort identifiers receive a readable title generated at render time
  but preserve their exact raw value when submitted.

Selection preferences are namespaced by host and harness. When the same pair's
account scope changes, or a refresh removes the selected model, the app resets
to Harness default and tells the user that the previous model is unavailable.
It never silently submits an identifier absent from the current live catalog.

While a refresh is in flight, the existing catalog remains interactive. A stale
catalog shows `Last updated <time>` and a Retry action. With no cached catalog,
the control contains Harness default plus a `Never refreshed` discovery status;
session launch remains possible.

## Session behavior and validation

New sessions pass the chosen IDs through the existing `model` and
`thinking_effort` fields. Harness default and Auto remain `null`, not magic
strings.

The host validates non-null values against a fresh-enough native catalog before
starting a process or turn:

- A valid value is forwarded unchanged.
- A value invalidated after the picker loaded produces a clear `400` response
  asking the client to refresh; it is not silently replaced.
- Discovery failure does not reject Harness default or Auto.

Codex and agy may continue accepting preference changes on later turns when
their drivers support them. Claude Code continues to lock preferences after
process launch. The catalog advertises availability, not mutability; existing
session editability remains a separate harness capability.

## Security and privacy

- Discovery executes only registered adapters for enabled harnesses.
- Credentials remain on the host and are never returned, logged, placed in a
  subprocess command line, or stored in the catalog cache.
- `account_scope_id` is produced from a host-local secret or random mapping; it
  is not a raw hash of an email address or account ID.
- Provider errors are mapped to safe categories before proxying.
- Subprocess output, HTTP responses, model counts, and strings are bounded.
- Redirect and credential-forwarding protections match existing provider
  probes: credentials are never followed to an untrusted redirect target.
- The client cannot supply an executable, provider URL, or arbitrary discovery
  arguments.

## Testing and acceptance criteria

### Host unit tests

- Each adapter normalizes valid native fixtures, pagination, defaults, effort
  metadata, and unknown future effort strings.
- Timeouts, malformed output, partial responses, oversized responses, missing
  credentials, and provider errors return safe failure categories.
- Claude configuration tests cover restrictions, aliases, overrides, gateway
  discovery, and custom entries without leaking credentials.
- Cache tests prove that failures never overwrite a last-known-good result and
  that account/configuration changes rotate scope.
- Launch validation accepts null defaults, rejects unavailable explicit values,
  and cannot execute a client-supplied command.

### Central-server tests

- Direct and relay routes proxy the selected host, quote parameters, forward
  authentication correctly, and enforce response limits.
- A successful response is persisted under the correct host/harness/scope.
- Offline, timeout, and malformed upstream responses return the persisted
  catalog with `stale: true`.
- A first-ever failure returns an empty catalog envelope rather than a static
  list.

### iOS tests

- Unknown model and effort strings decode and render without an application
  change.
- Catalogs and selections do not leak between hosts or harnesses.
- Changing account scope or removing a model resets the selection to Harness
  default with an explanatory message.
- Incompatible explicit effort resets to Auto when the model changes.
- Stale and empty states remain launchable and always expose Harness default.
- Out-of-order refresh responses cannot replace the current host/harness view.

### Live acceptance

- On the current Codex host, the picker shows Sol, Terra, and Luna from native
  discovery, with the correct differing effort sets and defaults.
- The Claude catalog matches the choices and restrictions visible in `/model`
  for the authenticated account on each configured provider path.
- The agy catalog matches `agy models` exactly for IDs and display names.
- Updating any harness so that it adds a model makes that entry appear after
  refresh without rebuilding or reinstalling the iOS app. Newly advertised
  effort levels do the same when that harness driver supports forwarding an
  effort override.
- Taking the selected host offline leaves its prior catalog visible, clearly
  marked stale, and Harness default remains launchable.

## Rollout

The feature is delivered as one coordinated compatibility release:

1. Deploy harnessd and central-server support. Older clients ignore the new
   endpoint and keep their current behavior.
2. Ship the bootstrap iOS client with generic decoding and the dynamic picker.
3. Remove app-owned suggestions only in that bootstrap client, after all three
   adapters and degraded states pass acceptance tests.

The API is additive. New adapter fields are optional, unknown fields are
ignored, and future harnesses can join by implementing the same host-side
adapter without another model-picker redesign.
