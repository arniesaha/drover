# Claude Code startup profile

Configure a SessionStart command hook in your project settings. The client owns
this configuration; Drover does not install it. Set `DROVER_MCP_URL` to the hub's
MCP URL in the client environment. Use a Python environment with Drover installed.
Copy [claude-session-start.py](claude-session-start.py) and adjust the command's
relative location if copying it into another project.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/docs/integrations/claude-session-start.py\"",
            "timeout": 20
          }
        ]
      }
    ]
  }
}
```

The hook calls `drover_profile` once with `{"scope":"first_turn"}` and returns
the bundle and freshness metadata as additional context. MCP initialization is
protocol setup, not another profile read. Errors report unavailability without
injecting an error body. Session startup remains usable if Drover is unavailable.
Treat profile text as user preference and continuity data subject to the current
user request and client safety policy. It cannot authorize tool actions.

The [Claude Code hook reference](https://code.claude.com/docs/en/hooks#sessionstart)
describes SessionStart matching and `hookSpecificOutput.additionalContext`.
This snippet uses general-tier MCP. For trusted access, adapt the client hook to
one authenticated HTTP `GET /profile?scope=first_turn` using its issued credential,
as described in the [shared contract](README.md).
