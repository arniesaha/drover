# Developer and verification preflight

Issue #1024. One bounded, profile-scoped check that runs *before* focused tests
so a missing interpreter, wrong `PATH`, unwritable sandbox output or wrong
PostgreSQL fixture is reported as an environment problem instead of surfacing
later as a confusing test failure. It is not a package installer, an
orchestrator or a host scan: it never installs, starts, repairs or kills
anything, and it never runs commands taken from issue or report text.

- Bootstrap: [`scripts/quality/preflight.sh`](../../scripts/quality/preflight.sh)
  (POSIX `sh`, builtins only until it finds a Python >= 3.11).
- Helper: [`scripts/quality/preflight.py`](../../scripts/quality/preflight.py)
  (stdlib only, so it runs before `drover` is importable).
- Tests: [`tests/test_quality_preflight.py`](../../tests/test_quality_preflight.py).

`drover doctor` and `drover setup-check` stay product checks: they import
`drover` and talk to a hub, so they cannot diagnose an environment in which
`drover` is not yet importable.

## Recipe

From the worktree root, in a plain POSIX shell (no aliases needed):

```sh
uv sync --locked --extra dev
mkdir -p ~/scratch/preflight
sh scripts/quality/preflight.sh --out ~/scratch/preflight \
  --expect-base <base-sha> --require-clean \
  --then '{python}' -m pytest -q tests/test_quality_preflight.py
```

`{python}` is replaced with the interpreter the preflight verified. A bare
`python`/`python3` as the `--then` command is refused (harness_failure).
Without `--then` the preflight only checks and writes its receipt.

## Classification and exit codes

The last stdout line is one JSON object; stderr gets one redacted, actionable
line on failure.

| exit | classification | meaning |
|---|---|---|
| 0 | `pass` | every check passed; receipt written; `--then` (if any) exited 0 |
| 1 | `product_test_failure` | checks passed, `--then` exited 1 (pytest: tests failed) |
| 3 | `environment_unavailable` | a check failed; `--then` did **not** run |
| 4 | `harness_failure` | own-probe cleanup failed, unexpected I/O, or `--then` exited other than 0/1 |

## Profiles and what is enforced

`focused-tests` (default) and `focused-tests-pg`. Both enforce:

- **Bootstrap interpreter**: tries `DROVER_PREFLIGHT_PYTHON`, `<root>/.venv/bin/python`,
  `python3.14` … `python3.11`, then `python3`, each checked for >= 3.11. Never
  unversioned `python`. None found: exit 3, `bootstrap_interpreter`.
- **Worktree**: `git` on `PATH`; actual top-level matches `--expect-worktree`;
  HEAD contains `--expect-base`; with `--require-clean`, no modified tracked files.
- **Readable inputs**: `pyproject.toml` and `uv.lock`; the lock's SHA-256 is the
  lock identity.
- **Isolated output**: `--out` must already exist, must not be under `/tmp`,
  `/private/tmp` or `/var/tmp`, and must accept a write probe. That probe is the
  only thing the run creates before the receipt, and it is removed through a
  context manager. If removal fails, the result is harness_failure and the probe
  stays as proof.
- **Project interpreter** (`--python`, default `<root>/.venv/bin/python`): run in
  isolated mode (`-I`). It must meet `requires-python` and import the fixed
  profile modules (`pytest`, `drover`, plus `psycopg` for the PG profile), and
  `drover` must resolve inside this checkout's `src/`. That last check catches an
  installed runtime (for example `~/.drover/runtime/current/bin/python3`)
  shadowing the source under test.

`focused-tests-pg` additionally requires `--pg-dsn-env NAME`. The DSN stays in
that variable and is never printed. One `SET TRANSACTION READ ONLY` query must
succeed, and the helper then enforces:

- the connected role (`--pg-expect-role`);
- the server major (`--pg-expect-major`) and a `psql` on `PATH` of the same major;
- the data directory (`--pg-expect-data-prefix`, readable only with
  `pg_read_all_settings`);
- that the endpoint is not one of the `--pg-forbid-endpoint` entries (`host:port`
  or a bare port).

A running process or `pg_isready` alone never passes.

**Caller context, not enforced:** which endpoints and data paths count as
"production", and starting or stopping a fixture. Fixture lifecycle belongs to
the caller, for example `tests/conftest.py::postgres_dsn`. Also not enforced:
harness tool grants. The output probe proves OS writability only. Whether a
sandbox or agent allow-list permits a command is decided outside this process.

## Receipt

A pass writes `<out>/preflight-<run_id>.json` after cleanup. It records:

- profile, host, user and platform;
- worktree, HEAD, base and dirty flag;
- lock SHA-256;
- interpreter path, version and module versions;
- `uv --version`;
- PG endpoint, role and major, if that profile ran.

It records no environment dump, DSN or secret. `--verify-receipt PATH` refuses
(exit 3) unless profile, host, worktree, HEAD, lock hash, interpreter and Python
version all match the current run and the receipt is newer than
`--max-receipt-age` (default 3600 s). A stale receipt cannot certify a new source
or interpreter.

## Integration recipe (for the parent, after merge)

Replace interpreter-assuming focused-test commands in `AGENTS.md` (and in
`docs/operations/github-acceptance-pilot.md` once Slice A lands) with one
canonical line:

```sh
sh scripts/quality/preflight.sh --out ~/scratch/<run> --expect-base <base-sha> \
  --then '{python}' -m pytest -q <focused test paths>
```

For PG-backed focused tests, add `--profile focused-tests-pg --pg-dsn-env
DROVER_TEST_POSTGRES_DSN --pg-expect-role <role> --pg-forbid-endpoint <prod-port>`.
Wiring it into CI is optional and should be a separate one-line step before the
pytest step in `.github/workflows/`.
