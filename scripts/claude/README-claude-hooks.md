# Wiring `drover-hook` into Claude Code

`drover-hook` is invoked by Claude Code's lifecycle hooks. Add the
following to `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "drover-hook session-start --cwd \"$CLAUDE_PROJECT_DIR\""
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "drover-hook session-end --session-id \"$CLAUDE_SESSION_ID\""
          }
        ]
      }
    ]
  }
}
```

## Optional `~/.drover/hook.toml`

```toml
agent_id = "macmini-claude"            # default: <hostname>-claude
mcp_url  = "http://mac-mini.local:7077/mcp"  # default: http://127.0.0.1:7077/mcp
```

## Behavior

- **SessionStart** prints a markdown handoff block to stdout. Claude
  Code injects this into the system context. If the cwd has no git
  remote, the hook prints a benign skip banner instead.
- **SessionEnd** enqueues a summarize_jobs row by calling
  `drover_session_close`. The summarizer worker on the Mac Mini picks
  it up within a few seconds.

## Failure modes

- drover-server unreachable → both subcommands print `(drover offline)`
  to stderr and exit 0. The agent never blocks.
- Hard 2-second budget per spec §3.1.

## Per-host paths

For non-Mac hosts, set `mcp_url` to point at the Mac Mini over
Tailscale or LAN:

```toml
mcp_url = "http://mac-mini.tailnet.ts.net:7077/mcp"
```
