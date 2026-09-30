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

The bridge accepts no prompt, arbitrary command, or Factory mutation field. It forces Drover's existing `structured` launch mode, resolves the command from the selected harness adapter, and requires the existing isolated Git-worktree path to succeed. A non-Git working directory, disabled structured adapter, unavailable model selection, or worktree failure is rejected before a Factory observer session is started.

The Factory idempotency key is deterministically mapped to Drover's existing `client_session_id` uniqueness fence. Repeating the exact request returns the existing bounded session metadata rather than starting another host process or worktree.

## Observer projection and boundaries

The Harness UI derives a `factory_observer` display block from existing session correlation fields. It shows the Factory run ID, launch-bound expected revision, selected host, and the live Drover harness-session status. It labels TaskFlow as authoritative and suppresses the Harness UI's kill control for observer sessions.

That projection is intentionally not a Factory status API: `expected_revision` is the revision that authorized this launch, not a claim about the current Factory run. Drover does not call TaskFlow and cannot approve, cancel, advance, resume, finish, or otherwise mutate Factory state. Factory controls remain in the Factory/TaskFlow surface.

## Configuration and authentication

No new Drover configuration is required. The target host must already be paired/registered, reachable through its existing direct or relay route, and have the chosen harness's structured adapter and model catalog available. The caller needs an existing authenticated Drover bearer credential accepted by the hub; `preflight` credentials are read-only and are refused by the normal authentication gate. Use a dedicated revocable existing Drover credential for the Factory client rather than the legacy shared token where available.

No Factory credential, TaskFlow URL, Studio access, production service change, or new database migration is required by this bridge.
