# Architecture

Drover has two cooperating planes backed by one local data boundary.

![Drover architecture](drover-architecture.png)

## Command Plane

The command plane carries live fleet operations:

1. The iOS app or another authenticated client calls `drover-server` over the
   `/harness` HTTP and WebSocket API.
2. `drover-server` maintains the fleet registry and routes each operation to a
   host connection.
3. A per-host `drover-harnessd` owns local agent processes, structured
   protocol adapters, PTY/tmux sessions, and terminal I/O.
4. Direct hosts accept private inbound connections. Relay hosts dial out to the
   central server over the same trusted LAN or tailnet.

The central server does not execute remote commands itself. The host daemon is
the authority for processes and filesystem access on its machine.

## Context Plane

The context plane turns local agent activity into durable, queryable memory:

1. `drover-collect`, hooks, and OTLP producers emit agent events and spans.
2. Ingest normalizes identifiers, attributes repository context, deduplicates
   records, and writes partitioned Parquet facts.
3. DuckDB views expose normalized events, spans, sessions, links, pull-request
   events, and routing records without duplicating the fact store.
4. Workers create summaries, project briefs, decisions, and embeddings in
   mutable DuckDB tables.
5. MCP tools query raw and derived context for recall and handoff.

See [Context Store](context-store.md) for table ownership, identity, and
provenance rules.

## Process Boundaries

| Component | Runs on | Owns |
| --- | --- | --- |
| iOS app | iPhone or simulator | Presentation, local settings, token in Keychain |
| `drover-server` | Central machine | Fleet API, ingest, local store, workers, MCP |
| `drover-harnessd` | Every harness host | Agent processes, adapters, PTY, terminal stream |
| `drover-collect` | Source hosts | Local log parsing and source-side attribution |
| DuckDB + Parquet | Central storage | Durable facts, serving state, derived context, ledger |
| Redis Streams | Optional central dependency | Retry coordination only |

## Interfaces

- Harness API: authenticated HTTP and WebSocket, normally port `7080`
- Host daemon: private HTTP and WebSocket, normally port `7081`
- MCP: streamable HTTP at `/mcp`, normally port `7077`
- OTLP: gRPC ingest, normally port `4317`
- Files: JSONL inputs under `~/.drover/incoming/`

Ports and bind addresses are configurable. Only localhost, private LAN, and
private Tailscale deployments are supported for v0.1.

## Failure And Recovery

- Ingest uses stable deduplication keys, so replaying a source batch is safe.
- Parquet facts survive a DuckDB catalog rebuild; views are recreated during
  bootstrap.
- Pipeline receipts fence duplicate work, attempts remain append-only, and
  artifacts record supersession explicitly.
- Optional workers can remain unavailable without stopping the command plane or
  durable ingest.
- Redis coordination can be disabled or rebuilt from durable DuckDB intent.

## Compatibility

Public processes, commands, APIs, and MCP tools use Drover naming. Historical
Parquet spans and stored integration values may retain `nexus.*` identifiers.
Readers preserve those values as compatibility inputs; new producers should
emit Drover naming. Compatibility storage is not a second public product name.

## Security Boundary

All components belong to one trusted operator. A shared bearer token protects
the central API, but v0.1 does not provide per-host identity, RBAC, SSO, or
multi-tenant isolation. See [Security](security.md).
