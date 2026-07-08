# Meta Harness host daemon footprint

Issue: #203

The host daemon is the always-on data plane for NAS, Mac Mini, and GPU PC. It
should own PTYs, signals, resize, attach/detach, capability reporting, and
bounded transcript forwarding. It should not pay the resident import cost for
summaries, embeddings, OTLP, MCP, PyArrow, or server observability.

## Entry points

- `nexus-harnessd`: preferred resident host daemon entry point. It imports only
  the harness daemon path plus config/schema bootstrapping.
- `nexus-server harnessd`: compatibility subcommand. It still goes through the
  full `nexus-server` Click module and is expected to have a larger import
  footprint until the monolithic server CLI is split further.

## Budgets

- Python MVP: acceptable for protocol validation when idle RSS is under roughly
  100 MB per host.
- Attached shell: daemon overhead should remain near idle; the shell process is
  measured separately.
- Attached agent CLIs: Claude Code, Codex, Gemini, and OpenClaw RSS belongs to
  the child process budget, not daemon overhead.
- Rust/Go target: under 15 MB idle RSS, negligible idle CPU, and a single
  static-ish binary for packaging.

## Measurement

Run on each target host:

```bash
uv run python scripts/profile_harnessd_footprint.py
```

For installed packages, use the venv interpreter:

```bash
python scripts/profile_harnessd_footprint.py --python .venv/bin/python
```

Record:

- idle RSS for `nexus-harnessd`
- idle RSS for `nexus-server harnessd`
- RSS with one shell session
- RSS with one attached WebSocket terminal
- child RSS for Claude Code, Codex, Gemini, and OpenClaw sessions

Use `ps -o pid,ppid,rss,pcpu,command -p <pid>` for process snapshots and
`pgrep -P <harnessd-pid>` to separate daemon RSS from child CLI RSS.

## Current Linux baseline

Measured on the NAS development environment after adding the standalone entry
point:

| Scenario | RSS |
| --- | ---: |
| `import nexus.server.harness.cli` | 64.8 MB |
| `import nexus.server.__main__` | 142.9 MB |
| `nexus-harnessd` idle | 117.3 MB |
| `nexus-server harnessd` idle | 159.0 MB |

The standalone path avoids loading `pyarrow`, `grpc`, `mcp`, `anthropic`,
`nexus.server.summarizer.worker`, `nexus.server.embeddings.worker`,
`nexus.server.otlp.receiver`, and `nexus.server.mcp.server` at import time.
DuckDB is still loaded by the Python MVP because the daemon writes best-effort
host/session records into the local registry. That keeps the standalone path
above the long-term host-agent target and slightly above the initial Python
budget on this Linux host; the next footprint slice should either make registry
writes remote/optional or move the data plane to Rust/Go.

## Rewrite decision criteria

Keep the Python daemon for v0 while all are true:

- the standalone entry point stays under the 100 MB Python MVP budget on NAS,
  Mac Mini, and GPU PC
- idle CPU is negligible
- protocol iteration is still active
- the process remains limited to PTY/session data-plane behavior

Start a Rust or Go `threadline-harnessd` replacement when any of these becomes
true:

- idle RSS cannot stay under the Python MVP budget after import cleanup
- packaging Python plus native DuckDB/PyArrow dependencies is the main rollout
  blocker
- long-running host stability depends on tighter resource isolation
- the harness protocol has stabilized enough to freeze a binary compatibility
  contract

Compatibility contract for a future binary:

- same HTTP endpoints: `GET /healthz`, `GET /capabilities`, `POST /sessions`
- same WebSocket terminal endpoint: `/sessions/{session_id}/terminal`
- same JSON message envelope for terminal attach/input/output/resize/exit
- same registry semantics for host/session/event/transcript records, even if
  the binary forwards them to central Nexus instead of writing DuckDB directly
