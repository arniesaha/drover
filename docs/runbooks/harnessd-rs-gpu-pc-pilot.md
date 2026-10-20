# Rust harnessd GPU PC pilot (Phase 2 / M2)

This is an **operator runbook**, not authorization for an automated agent to
connect to the GPU PC. The orchestrator performs every host operation below.
The implementation work and CI use disposable local fixtures/GitHub-hosted
runners only. Never run these commands on the hub or another live host.

## Preconditions and evidence

Use the successful CI **push run on main after the Phase 2 PR merges**, with
its merged source SHA. The PR must first pass on its exact, current head. Record the source SHA,
CI run ID, Linux binary byte size, SHA256, contract counts, and this runbook in
the pilot log. Require both `harnessd contract (py)` and `(rs)`, both platform
Rust jobs, and the repository's required checks to be green on that exact SHA.
PR Linux artifacts have GitHub OIDC/Sigstore-signed provenance; macOS PR builds
also have an ad-hoc code signature. Release macOS binaries use the existing
Developer ID/notarization workflow. A checksum alone is not signature evidence.

The pilot supports shell and Codex. Interactive auth, provider probes, model
catalog, other structured adapters, advisory content and self-update are later
phases. Authenticate Codex locally before cutover; `/model-catalog` intentionally
returns 404 and `/providers/usage` returns an empty account list. Do not allow
Python's updater or another service to start Python during the Rust pilot.

The orchestrator must inspect the existing Python user unit, config, environment
file, executable and restart policy, and record them privately. Do not paste
bearers into logs. Resolve these variables from that inspection (examples are
only the usual paths; **retain the actual host identity, URLs and config**):

```bash
export PILOT_RUN_ID='<green post-merge main push CI run id>'
export PILOT_SOURCE_SHA='<that run head SHA>'
export HOST_ID='<existing GPU PC host id>'
export CONFIG="$HOME/.drover/config.toml"
export PY_UNIT=drover-harnessd.service
export RS_UNIT=drover-harnessd-rs.service
export CENTRAL_URL='<existing hub URL>'
export LISTEN='127.0.0.1:7081' # copy the existing bind address and port exactly
export PILOT_ROOT="$HOME/.drover/harnessd-pilot/$PILOT_SOURCE_SHA"
mkdir -p "$PILOT_ROOT/download" "$PILOT_ROOT/evidence"
chmod 700 "$PILOT_ROOT"
systemctl --user is-active "$PY_UNIT"
cp "$(systemctl --user show "$PY_UNIT" --property=FragmentPath --value)" \
  "$PILOT_ROOT/python-unit.before"
chmod 600 "$PILOT_ROOT/python-unit.before"
```

Export `DROVER_API_TOKEN` securely from the same source Python uses (an existing
EnvironmentFile or `~/.drover/api_token`). The examples below use the file and
avoid tokens in process arguments. Clear the variable after the work.

## Install beside Python, without activation

Run on the GPU PC as the existing service user, with `gh`, `curl`, `sqlite3`,
`git`, Codex and the existing Python runtime available:

```bash
gh run view "$PILOT_RUN_ID" --repo arniesaha/drover \
  --json headSha,conclusion --jq '[.headSha,.conclusion] | @tsv'
# Require exactly PILOT_SOURCE_SHA and success before continuing.
gh run download "$PILOT_RUN_ID" --repo arniesaha/drover \
  --name harnessd-pilot-x86_64-unknown-linux-musl --dir "$PILOT_ROOT/download"
cd "$PILOT_ROOT/download"
sha256sum --check SHA256SUMS
gh attestation verify drover-harnessd-x86_64-unknown-linux-musl \
  --repo arniesaha/drover --signer-workflow arniesaha/drover/.github/workflows/ci.yml \
  --source-digest "$PILOT_SOURCE_SHA"
install -d "$HOME/.drover/harnessd/versions/$PILOT_SOURCE_SHA"
install -m 755 drover-harnessd-x86_64-unknown-linux-musl \
  "$HOME/.drover/harnessd/versions/$PILOT_SOURCE_SHA/drover-harnessd"
export RS_BIN="$HOME/.drover/harnessd/versions/$PILOT_SOURCE_SHA/drover-harnessd"
"$RS_BIN" --version
file "$RS_BIN" # x86-64 static ELF, no Python or glibc dependency
wc -c < "$RS_BIN"
```

Keep the Python runtime, original unit, config, credentials and DuckDB registry
intact. Do not change `~/.drover/runtime/current`. Rust's SQLite ledger lives at
`~/.drover/harnessd/harnessd.sqlite`; Rust never opens DuckDB. Do not run an
import while Rust is serving: the store ownership lock rejects it.

## Spare-port dress rehearsal first

Python stays on its usual port. Rust gets an isolated HOME, worktree directory,
store, identity and spare loopback port **without a central URL**. This prevents
a rehearsal from registering over the existing host or replaying its history.
Use an authenticated temporary token; the HOME is private and disposable.

```bash
export REHEARSAL="$PILOT_ROOT/rehearsal"
mkdir -p "$REHEARSAL/home/.drover"
chmod 700 "$REHEARSAL" "$REHEARSAL/home"
openssl rand -hex 32 > "$REHEARSAL/home/.drover/api_token"
chmod 600 "$REHEARSAL/home/.drover/api_token"
printf '[paths]\nworktrees_dir = "%s/worktrees"\n' "$REHEARSAL" > "$REHEARSAL/config.toml"
# Unset inherited production tokens so the rehearsal uses its own token file.
env -u DROVER_API_TOKEN HOME="$REHEARSAL/home" "$RS_BIN" \
  --config "$REHEARSAL/config.toml" --host-id gpu-pc-rs-rehearsal \
  --listen 127.0.0.1:17081 > "$REHEARSAL/stdout.log" 2> "$REHEARSAL/stderr.log" &
REHEARSAL_PID=$!
curl --fail --silent http://127.0.0.1:17081/healthz
curl --fail --silent -H "Authorization: Bearer $(cat "$REHEARSAL/home/.drover/api_token")" \
  http://127.0.0.1:17081/capabilities
```

Authenticate the rehearsal Codex CLI in the isolated HOME first (for example,
`env HOME="$REHEARSAL/home" codex login` in an operator terminal). Keep its
credentials separate from the production HOME.

Create a shell session in a disposable directory and exercise terminal attach,
input, resize, ping, interrupt and exit. Use a scratch Git repository for one
Codex turn; verify its isolated worktree and the `git_diff` evidence event in
SQLite. Terminate it and confirm edited work is retained. Repeat after a daemon
restart and test a native-ID recovery. CI's `test_phase2_contract.py` and relay
contracts are the executable reference for these checks; if running them here,
use the checked-out approved SHA and their isolated fixtures/fake hub, never a
production hub. Do not use the rehearsal HOME as the production import target.

```bash
kill -TERM "$REHEARSAL_PID"
wait "$REHEARSAL_PID"
systemctl --user is-active "$PY_UNIT"
# Save rehearsal logs and results in PILOT_ROOT/evidence before proceeding.
```

## Drain, stop Python, export, import, and cut over

Schedule a quiet window. Pause new launches at the orchestrator, confirm no
unrelated pilot-host sessions are being created, and resolve active terminal
work with its owners. `/drain` explicitly closes shell terminals and fences new
launches/turns for ten minutes while existing structured turns finish. A 409
means a turn is still busy: do not stop the service or proceed with cutover.
Cancel a failed/aborted drain with `POST /drain {"cancel":true}`.

```bash
export DROVER_API_TOKEN="$(cat "$HOME/.drover/api_token")"
export LOCAL_URL="http://$LISTEN" # use 127.0.0.1 instead if LISTEN binds 0.0.0.0
curl --fail --silent --max-time 310 \
  -H "Authorization: Bearer $DROVER_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"timeout":300}' "$LOCAL_URL/drain"
# Require {draining:true,quiescent:true}; then stop promptly, before the gate expires.
systemctl --user stop "$PY_UNIT"
! systemctl --user is-active --quiet "$PY_UNIT"
```

Use the updated Python daemon from this PR for `/drain`. If the installed
version predates it, stage that Python build first or abort the cutover; do not
silently substitute a forced stop. Record the exact original Python executable
for rollback. Disable its restart timer/updater for this window as applicable.

Resolve `REGISTRY` from the Python config's `paths.duckdb_path` and the actual
`DROVER_CONTROL_PLANE_DUCKDB` override, if any. Normally `drover.duckdb` maps to
its sibling `drover.registry.duckdb`. **Never substitute a hub/Postgres store**.
Use the exporter and Python interpreter from the approved source/runtime:

```bash
export REGISTRY='<resolved GPU PC local *.registry.duckdb>'
export PYTHON_BIN='<original Python runtime>/bin/python'
export APPROVED_CHECKOUT='<checkout of PILOT_SOURCE_SHA>'
export HANDOFF="$PILOT_ROOT/import.jsonl"
cp -p "$CONFIG" "$PILOT_ROOT/config.before.toml"
cp -p "$REGISTRY" "$PILOT_ROOT/registry.before.duckdb"
sha256sum "$REGISTRY" > "$PILOT_ROOT/evidence/python-registry.sha256"
"$PYTHON_BIN" "$APPROVED_CHECKOUT/scripts/export_harnessd_handoff.py" \
  --registry "$REGISTRY" --host-id "$HOST_ID" --output "$HANDOFF"
sha256sum --check "$PILOT_ROOT/evidence/python-registry.sha256"
# The export is read-only, mode 0600, and refuses an existing output path.
"$RS_BIN" --config "$CONFIG" --host-id "$HOST_ID" --import-jsonl "$HANDOFF" --import-dry-run
"$RS_BIN" --config "$CONFIG" --host-id "$HOST_ID" --import-jsonl "$HANDOFF"
sqlite3 "$HOME/.drover/harnessd/harnessd.sqlite" \
  'PRAGMA integrity_check; SELECT status,count(*) FROM sessions GROUP BY status; SELECT count(*) FROM event_quarantine;'
```

Export includes session metadata, unreconciled events, original `event_id`,
`dedup_key` and sequence, retained quarantine payloads/receipts, and worktree and
lifecycle records. Import validates and commits atomically; repeat imports do
not duplicate events or overwrite newer session state. Formerly live sessions
become errored, with stale PIDs cleared, and require explicit Codex recovery
using their native ID. Lifecycle operation metadata is retained for inspection;
Rust does not execute imported pending lifecycle/GC operations.

Create a **separate**, initially disabled `drover-harnessd-rs.service` by copying
the existing unit's user, working directory, PATH, HOME, EnvironmentFile and
host flags. Replace only ExecStart with the versioned Rust binary and preserve
its `--config`, `--host-id`, `--display-name`, `--kind`, `--listen`, `--central-url`,
`--local-url`, `--tailscale-url` and `--relay` values. Never put a bearer in the
unit's ExecStart. Use `Restart=on-failure`, `RestartSec=5`, `KillMode=control-group`
and `TimeoutStopSec=30`; intentional retired-host/direct-mode refusal exits
successfully and must not be restarted in a loop. Both daemons claim the same
worktree directory lock, so a second writer fails closed.

```bash
# After writing/reviewing ~/.config/systemd/user/drover-harnessd-rs.service:
systemctl --user daemon-reload
systemctl --user start "$RS_UNIT"
systemctl --user is-active "$RS_UNIT"
curl --fail --silent "$LOCAL_URL/healthz"
curl --fail --silent -H "Authorization: Bearer $DROVER_API_TOKEN" "$LOCAL_URL/capabilities"
journalctl --user -u "$RS_UNIT" --since '5 minutes ago' --no-pager
```

Verify the original host appears online via relay with only shell+Codex and
`interactive_auth:false`; registration consent is persisted with its epoch.
Confirm the importer row counts and hub history agree, pending deliveries drain,
and quarantine is empty or fully explained **before** starting the soak clock.
An unexplained receipt/ownership warning is a failed cutover. Exercise real
shell terminal input/output and one Codex turn through the hub relay, not only
loopback. Test `/fs/list` pagination and folder creation from the iOS picker.
Confirm `/providers/usage` is empty, catalog is 404, `/permission` is 409 for a
live Codex session and 404 for an unknown one. Resume the orchestrator's launches
only after these checks pass.

## Rollback command

On any failed gate, stop accepting new launches, let active Rust turns finish,
and record sessions/changes created during the pilot. Rust termination retains
edited/committed worktrees. Python's old ledger will not contain new Rust-only
sessions; record their native IDs, Git paths and hub IDs for explicit recovery
or continuation. Keep the Rust SQLite DB and logs as evidence; never copy SQLite
into DuckDB or replay quarantined events automatically.

```bash
systemctl --user stop "$RS_UNIT" && systemctl --user start "$PY_UNIT"
systemctl --user is-active "$PY_UNIT"
curl --fail --silent "$LOCAL_URL/healthz"
# Verify original host registration and relay, then reopen launches.
```

The Python unit/executable/config and registry are unchanged by the cutover.
The stop releases Rust's worktree/store locks. Do not restore the preflight
DuckDB backup over newer Python data. For an aborted drain before stopping
Python, cancel with `curl --fail -H "Authorization: Bearer $DROVER_API_TOKEN"
-H 'Content-Type: application/json' -d '{"cancel":true}' "$LOCAL_URL/drain"`.

## 72-hour soak exit criteria

Record UTC start/end, source SHA, restart count, representative workload,
counts/histograms and logs. Any restart, warning, rollback or code/config change
resets the uninterrupted 72-hour clock after correction.

| Gate | Measurement and passing criterion |
|---|---|
| Daemon RSS | Sample `/proc/<MainPID>/status` `VmRSS` every minute: every sample **<100 MB** (100,000,000 bytes; require VmRSS <97,656 KiB, interpreting the kernel's `kB` field as KiB). Track Codex child RSS separately. No rising retained-session trend after repeated terminal attaches/exits. |
| Idle CPU | Use `pidstat -p <MainPID> 60` or `/proc/<pid>/stat` deltas during quiet windows: daemon CPU **<1% of one core**. Record at least hourly idle windows; exclude deliberate Codex/shell work from the idle classification. |
| Relay launch latency | Measure at the orchestrator from POST to completed response through the hub's actual relay create route. Include shell and Codex launches (real scratch Git repo/worktree); use unique client_session_id for each attempt. Record all failures/timeouts. Require **p99 <2 s**, zero failed launches, at least 100 samples over 72 hours and coverage of both harnesses. Compute p99 by nearest-rank over all samples, retaining raw timings and workload labels. |
| Ownership / reconciliation | **Zero ownership or reconciliation warnings** during the soak, zero new quarantine receipts, no repeated historical replay batches. Query `event_quarantine` and the unpushed count hourly and after a relay interruption; pending events must drain on recovery. Retained quarantine payloads are never pruned or treated as acknowledgements. |
| Correctness | Shared contracts green for both implementations at the installed SHA; shell terminal WS data/input/resize, Codex resume/recover/interrupt, Git evidence, folder picker, relay reconnect and restart recovery remain functional. No unexplained session/worktree loss. |

Example read-only local measurements (operator runs these):

```bash
PID="$(systemctl --user show "$RS_UNIT" --property=MainPID --value)"
awk '/VmRSS/ {print strftime("%FT%TZ",systime(),1),$2,"kB"}' "/proc/$PID/status"
pidstat -p "$PID" 60 1
sqlite3 "$HOME/.drover/harnessd/harnessd.sqlite" \
  'SELECT count(*) FROM event_quarantine; SELECT count(*) FROM events e WHERE pushed_at IS NULL AND NOT EXISTS(SELECT 1 FROM event_quarantine q WHERE q.event_id=e.event_id);'
journalctl --user -u "$RS_UNIT" --since '<soak UTC start>' --no-pager \
  | rg -i 'ownership|quarantin|reconcil|authorization backoff|unavailable|failed'
```

The orchestrator owns the pilot outcome. M2 implementation readiness is not a
claim that the 72-hour live-host exit gate has already passed. Keep Python
available until the recorded soak meets every gate and rollout is authorized.
