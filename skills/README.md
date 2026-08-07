# Drover Agent Skills

The `drover` skill teaches coding agents how to resume prior work, search local
session history, and create grounded handoffs through Drover's MCP tools. Its
`drover-local-lakehouse` companion covers advanced read-only DuckDB and Parquet
diagnostics.

Install the bundle from a Drover checkout into the cross-runtime skills
directory:

```bash
mkdir -p ~/.agents/skills
ln -s "$(pwd)/skills/drover" ~/.agents/skills/drover
```

Run the command from the repository root. Remove an existing destination first
only if it is a stale link you intend to replace. Restart or open a new agent
session so its skill catalog is refreshed; Drover server processes do not need
to restart.
