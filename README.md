# Drover

> Drive your whole agent fleet — from your pocket.

**Drover** is a local-first cockpit for driving a personal fleet of CLI
coding agents (Claude Code, Codex, Gemini) across your own machines, from
anywhere. Agent sessions and traces land in a local DuckDB context store; a
harness control plane lets you watch, chat with, and hand off running
sessions from a native iOS app or a phone-friendly web UI. There is no cloud
component — everything runs on hardware you own.

## Components

The server side lives in `src/drover/` and installs as console scripts (see
`pyproject.toml`):

- **`drover-server`** — central control plane: event ingest into the DuckDB
  context store, plus the `/harness` REST + WebSocket API (session list,
  chat, terminal proxy) and the web UI that clients talk to.
- **`drover-harnessd`** — per-host data plane: owns the PTY/tmux processes
  that run the actual CLI agents and registers them with the central server.
- **`drover-collect`** — per-host shipper: parses agent CLI logs and ships
  events to the server.
- **`drover-hook`** — lifecycle hook CLI invoked by agent harness hooks
  (e.g. Claude Code SessionStart/SessionEnd).
- **iOS app** — native client in [`apps/drover/`](apps/drover/README.md):
  browse sessions across hosts, chat with them, attach a real terminal.

## Repository layout

```
src/drover/     Python package: server, harness daemon, collector, hooks
apps/drover/    Native iOS client (SwiftUI, XcodeGen project)
docs/           Architecture and design docs
deploy/         Kubernetes manifests and Grafana dashboards
scripts/        Install scripts, launchd/systemd units, operational tooling
tests/          Python test suite (pytest)
```

## Getting started

Requires Python 3.11+.

```bash
uv sync            # or: pip install -e .
uv run pytest      # run the test suite
```

## Documentation

- [docs/north-star.md](docs/north-star.md) — philosophy, positioning,
  audience, and the capability pillars.
- [docs/architecture.md](docs/architecture.md) — system architecture: the
  context store, ingest pipeline, and harness control plane.

## License

Apache-2.0 — see [LICENSE](LICENSE).
