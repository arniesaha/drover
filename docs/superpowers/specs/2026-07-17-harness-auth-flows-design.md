# Harness Auth Flows Design

## Summary

Drover will let a user repair logged-out harness CLIs from the Drover app
without SSHing into the host or opening a local terminal. The first version is
interactive-flow first: each provider CLI remains responsible for its own
credential storage, while Drover starts and observes the CLI's login flow,
normalizes the browser/device-code instructions, and exposes those instructions
through the central API and iOS app.

This design covers Claude Code, Codex, and Gemini. It deliberately excludes
provider-password entry, API-key paste flows, OAuth token storage, and
provider-specific OAuth reimplementation.

## Goals

- Show harness authentication status per host and harness.
- Start a provider login flow from the app when a CLI is logged out or status is
  unknown.
- Surface login URLs, device codes, provider messages, progress, success, and
  failures in a common shape.
- Keep provider credentials on the harness host, managed by the provider CLI.
- Reuse Drover's existing central-to-harnessd proxy pattern and bearer-token
  auth.
- Support cancellation and bounded runtime for login subprocesses.

## Non-Goals

- Drover will not collect provider passwords, provider API keys, OAuth refresh
  tokens, or long-lived access tokens in v1.
- Drover will not write provider config files except through the provider CLI's
  own login command.
- Drover will not implement provider OAuth/device-code protocols directly.
- Gemini v1 will not claim the same machine-readable login guarantees as Claude
  or Codex unless the installed CLI exposes them during implementation.

## Architecture

`drover-harnessd` is the auth authority for installed CLIs on a host. It owns
provider-specific auth adapters, starts short-lived login subprocesses, tracks
flow state in memory, and exposes host-local HTTP endpoints:

- `GET /auth/{harness}/status`
- `POST /auth/{harness}/start`
- `GET /auth/{harness}/flows/{flow_id}`
- `POST /auth/{harness}/flows/{flow_id}/cancel`

The central server proxies these endpoints under:

- `GET /harness/hosts/{host_id}/auth/{harness}/status`
- `POST /harness/hosts/{host_id}/auth/{harness}/start`
- `GET /harness/hosts/{host_id}/auth/{harness}/flows/{flow_id}`
- `POST /harness/hosts/{host_id}/auth/{harness}/flows/{flow_id}/cancel`

The iOS app talks only to central, just as it does for sessions, turns,
permissions, interrupts, and handoffs. Central continues to enforce Drover
bearer-token auth before proxying.

## Provider Adapters

Each adapter has two responsibilities:

1. Report best-effort auth status.
2. Start and monitor one login flow.

Adapter interface:

```python
@dataclass(frozen=True)
class HarnessAuthStatus:
    harness: str
    state: str  # authenticated | unauthenticated | unknown | unavailable
    label: str | None = None
    detail: str | None = None

@dataclass(frozen=True)
class HarnessAuthFlowSnapshot:
    flow_id: str
    harness: str
    state: str  # starting | waiting_for_user | authenticated | failed | cancelled | expired
    login_url: str | None = None
    device_code: str | None = None
    user_code: str | None = None
    message: str | None = None
    expires_at: str | None = None
    last_error: str | None = None

class HarnessAuthAdapter(Protocol):
    harness: str

    def status(self) -> HarnessAuthStatus: ...
    def start(self, *, cwd: Path | None = None) -> AuthFlowHandle: ...
```

Claude adapter:

- Status command: `claude auth status --json`.
- Login command: `claude auth login`.
- Variants such as `--console`, `--sso`, or `--email` are not required for v1,
  but the request body may reserve optional fields for later.
- The adapter parses browser URLs and human-readable instructions from
  stdout/stderr. Success is confirmed by process exit plus a fresh status check.

Codex adapter:

- Status command: `codex login status`.
- Login command: `codex login --device-auth`.
- The adapter parses the device URL and user code when emitted. Success is
  confirmed by process exit plus a fresh status check.

Gemini adapter:

- Status is best-effort from installed CLI/config/environment signals and may
  return `unknown`.
- Start command uses the installed CLI's interactive login/setup path observed
  during implementation. If no stable login command is available, v1 exposes the
  CLI's instructions as a managed interactive flow instead of pretending there is
  a clean device-code protocol.
- API-key entry is outside v1 unless Gemini's own CLI prompts for it and stores
  it without Drover receiving the secret.

## Flow State And Redaction

Harnessd stores active flows in memory:

- `flow_id`
- harness name
- process handle
- current normalized snapshot
- stdout/stderr tail after redaction
- created and updated timestamps
- timeout deadline

Flow output crossing to central or the app must be redacted before storage or
serialization. Redaction removes likely secrets from URLs and text, including
query parameters named `token`, `code`, `access_token`, `refresh_token`,
`id_token`, `client_secret`, `api_key`, `key`, or `secret`. Device/user codes
intended for the user may be exposed in `user_code` or `device_code`.

Flows are short-lived. Default timeout is 10 minutes. Terminal states are
retained in memory for another 10 minutes so the app can show the result, then
garbage-collected.

## API Shape

Status response:

```json
{
  "host_id": "mac-mini",
  "harness": "claude-code",
  "state": "authenticated",
  "label": "arniesaha@gmail.com",
  "detail": "Claude subscription"
}
```

Start response:

```json
{
  "host_id": "mac-mini",
  "harness": "codex",
  "flow_id": "auth-flow-...",
  "state": "waiting_for_user",
  "login_url": "https://...",
  "user_code": "ABCD-EFGH",
  "message": "Open the URL and enter the code."
}
```

Poll response uses the same flow fields and updates `state`, `message`,
`last_error`, and URL/code fields as the subprocess produces output.

Error responses use the existing Drover JSON convention:

```json
{"error": "unknown harness auth adapter: openclaw"}
```

## iOS App UX

The first app surface is pragmatic and task-oriented:

- Add an auth entry point for enabled host harnesses in the launch/session area.
- Show host, harness, current auth state, and a "Sign in" button when status is
  `unauthenticated` or `unknown`.
- Starting a flow opens an auth sheet with provider name, host, current state,
  URL/code when present, progress text, cancel, and retry.
- When `login_url` is present, use iOS `openURL` so the user can authorize in
  Safari or the provider app.
- Poll every 1-2 seconds while the flow is non-terminal.
- On `authenticated`, dismiss or show success and refresh the harness snapshot.
- On `failed`, show the normalized failure message and a retry action.

The UI does not ask users to paste secrets. If a provider flow prints a URL
and code, the app can display and copy/open those values.

## Error Handling

- Unknown host: central returns 404.
- Host without endpoint: central returns 502.
- Unsupported harness auth: harnessd returns 404 with a clear error.
- Concurrent start for the same host/harness: harnessd returns the active flow
  instead of spawning a duplicate, unless the prior flow is terminal.
- Subprocess exits nonzero: flow state becomes `failed`, with a redacted output
  tail.
- Timeout: harnessd terminates the process and marks the flow `expired`.
- Cancel: harnessd terminates the process and marks the flow `cancelled`.
- Central proxy errors preserve the host's status/body when possible, matching
  the existing harness session proxy pattern.

## Testing Plan

Python tests:

- Adapter unit tests with fake CLI scripts for authenticated,
  unauthenticated, URL/device-code output, nonzero exit, timeout, and redaction.
- Harnessd route tests for status, start, poll, cancel, duplicate start, and
  unsupported harness.
- Central proxy tests for status/start/poll/cancel and host endpoint failures.

Swift tests:

- Model decoding tests for auth status and flow snapshots.
- `NexusClient` tests for the four auth endpoints and error mapping.
- Store/model tests for polling and terminal-state handling.

End-to-end smoke:

- Fake harnessd CLI emits a login URL and exits success after a marker; central
  proxies it; the app/client observes `waiting_for_user` then `authenticated`.

## Rollout

Ship in this order:

1. Harnessd auth adapter interface and fake adapter tests.
2. Claude and Codex adapters.
3. Harnessd auth endpoints.
4. Central proxy endpoints.
5. iOS client/models.
6. iOS auth sheet and entry points.
7. Gemini adapter as best-effort/interactive, constrained by live CLI behavior.

This order produces useful Claude/Codex repair first while keeping Gemini's
uncertain auth surface honest.
