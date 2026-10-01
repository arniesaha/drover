# Factory Observer Bridge

The Factory observer bridge is a narrow adapter over Drover's existing authenticated host-session launch route:

```text
POST /harness/hosts/<target_hostname>/sessions
Authorization: Bearer <existing Drover non-preflight credential>
```

It does not add a Drover Factory endpoint, table, scheduler, or lifecycle ledger. TaskFlow remains authoritative for Factory state. Drover only starts one constrained host-local structured harness session and projects that harness session in the existing Harness UI.

## Request contract

Send this exact body shape to the existing route. `target_hostname` must equal the host in the route and the host-local harness daemon checks it again.

```json
{
  "factory_observer": {
    "run_id": "run_FACTORY001",
    "expected_revision": 4,
    "idempotency_key": "factory.launch:run_FACTORY001:4",
    "target_hostname": "studio",
    "repo": {
      "owner": "arniesaha",
      "name": "drover",
      "branch": "factory/run_FACTORY001"
    },
    "worktree": {
      "cwd": "/absolute/path/to/drover",
      "policy": "isolated_required"
    },
    "command": "harness_default"
  },
  "harness": "codex",
  "model": "gpt-5",
  "thinking_effort": "medium"
}
```

`harness` and `model` are required. `thinking_effort` is optional (omission or
`null`); when supplied it must be a non-empty string supported by the selected
model's catalog. For Claude Code models advertising `reasoning: null`, use
`"harness": "claude-code", "model": "opus"` and omit `thinking_effort`.
Supplying an effort for such a model returns HTTP 400 with
"The selected reasoning effort is not supported by this model."

The bridge accepts no prompt, arbitrary command, or Factory mutation field. It forces Drover's existing `structured` launch mode, resolves the command from the selected harness adapter, and requires the existing isolated Git-worktree path to succeed. A non-Git working directory, disabled structured adapter, unavailable model selection, or worktree failure is rejected before a Factory observer session is started.

Isolation is required even when the adapter does not advertise the worktree
capability (including Claude Code). The host creates the same per-session Git
worktree used for Codex; it never falls back to the requested checkout for a
Factory launch. Non-Git directories or repositories without commits return 400;
worktree creation failures return 503. The existing session row records the
isolated cwd and requested repo owner/name/branch, while the projection records
the actual worktree path/branch. A Git worktree isolates checkout changes, not
all host filesystem access or credentials.

### Claude Code unattended permissions

Ordinary Claude structured sessions currently default to `bypassPermissions`.
Factory launches explicitly replace that flag with `--permission-mode dontAsk`,
including when a session is recovered after a daemon restart. The existing
Drover session field `permission_mode: "auto"` is a generic launch-policy label;
it does **not** select Claude's `auto` classifier mode or permission bypass.
The bridge accepts no permission override or bypass option.

[Claude Code documents `dontAsk`](https://code.claude.com/docs/en/permissions#permission-modes)
as denying tool calls that would otherwise prompt, while allowing tools that
need no approval or are already allowed by permission rules. Consequently an
unattended Factory session does not park waiting for tool approval, but may be
unable to complete edits or commands without preconfigured allow rules. Drover
does not add allow rules or disable managed policies. This is deliberately a
restricted unattended launch, not a guarantee that every delegated task can
complete. The driver's existing `control_request` / `control_response` approval
mapping is fixture-tested but has not been verified with a live approval capture
(see `tests/fixtures/structured/FINDINGS.md`); this launch does not rely on it.
Factory creation starts a session without a prompt; the existing turn endpoint
is still needed to submit work.

The Factory idempotency key is deterministically mapped to Drover's existing `client_session_id` uniqueness fence. Repeating the exact request returns the existing bounded session metadata rather than starting another host process or worktree.

## Observer projection and boundaries

The Harness UI derives a `factory_observer` display block from existing session correlation fields. It shows the Factory run ID, launch-bound expected revision, selected host, and the live Drover harness-session status. It labels TaskFlow as authoritative and suppresses the Harness UI's kill control for observer sessions.

That projection is intentionally not a Factory status API: `expected_revision` is the revision that authorized this launch, not a claim about the current Factory run. Drover does not call TaskFlow and cannot approve, cancel, advance, resume, finish, or otherwise mutate Factory state. Factory controls remain in the Factory/TaskFlow surface.

## Configuration and authentication

No new Drover configuration is required. The target host must already be paired/registered, reachable through its existing direct or relay route, and have the chosen harness's structured adapter and model catalog available. The caller needs an existing authenticated Drover bearer credential accepted by the hub; `preflight` credentials are read-only and are refused by the normal authentication gate. Use a dedicated revocable existing Drover credential for the Factory client rather than the legacy shared token where available.

No Factory credential, TaskFlow URL, Studio access, production service change, or new database migration is required by this bridge.
