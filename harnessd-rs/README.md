# drover-harnessd (Rust Phase 2 pilot)

The daemon supports shell PTYs and Codex structured turns on Linux and macOS.
It owns `~/.drover/harnessd/harnessd.sqlite`, a bounded event ingress queue,
serialized StoreWriter and unified durable delivery/reconciliation path. Pending
and quarantined originals survive acknowledged-tail pruning. It never opens the
Python DuckDB registry. Worktree ownership locks are shared with Python.

```sh
cargo build --locked --release -p drover-harnessd
cargo test -p drover-harnessd
cargo clippy -p drover-harnessd -- -D warnings
HARNESSD_IMPL=py pytest tests/contract -m rust_phase2 -q
HARNESSD_IMPL=rs pytest tests/contract -m rust_phase2 -q
HARNESSD_IMPL=rs pytest tests/contract/test_rust_migration_contract.py -q
```

The shared contract gate requires the shipped binary and an isolated HOME;
missing binaries fail the Rust gate. `--daemon=rust` selects the same launcher.
Provider probes and model catalogs are disabled stubs; interactive auth is
explicitly false. `--help` documents the CLI/config subset and offline importer.

See [the Phase 2 audit](../docs/runbooks/harnessd-rs-phase2-audit.md) and
[the operator pilot runbook](../docs/runbooks/harnessd-rs-gpu-pc-pilot.md).
The GPU PC 72-hour soak is an operator gate, not a completed implementation test.
