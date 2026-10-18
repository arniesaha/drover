# drover-harnessd (Rust pilot)

The phase-2 pilot daemon supports the Linux shell and Codex harnesses. It owns
`~/.drover/harnessd/` (including `current` when installed) and never reads or
writes the Python DuckDB store.

Run the Rust slice contracts after building a release binary:

```sh
cargo build --release -p drover-harnessd
HARNESSD_IMPL=rs pytest tests/contract -q
```

The contract launcher deliberately uses the production binary and an isolated
HOME. `--daemon=rust` is also accepted by the repository test configuration.
