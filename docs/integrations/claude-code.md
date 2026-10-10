# Claude Code startup profile

Configure a SessionStart command hook in your project settings. The client owns
this configuration; Drover does not install it. Set `DROVER_MCP_URL` to the hub's
MCP loopback or protected-tunnel URL in the client environment. Also set
`DROVER_MCP_TOKEN` from the client secret store to an issued profile credential.
The bundled Python client sends it on every request. Use a Python environment
with Drover installed.
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
The hook uses the credential's registered profile tier for MCP, with the same
policy as HTTP. For native MCP tools, configure the client's `.mcp.json`:

```json
{
  "mcpServers": {
    "drover": {
      "type": "http",
      "url": "${DROVER_MCP_URL}",
      "headers": {"Authorization": "Bearer ${DROVER_MCP_TOKEN}"}
    }
  }
}
```

Populate both variables before starting Claude Code. An issued profile
credential only permits profile reads. Use a host/device credential when the
client also needs recall or mutations. Environment substitution keeps the
secret out of the configuration file. See the [shared contract](README.md) and
[Claude Code MCP configuration](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson).
