# NAS deploy runbook

How to update `drover-harnessd` on the NAS.

> **Before this repo goes public:** this file contains host-specific values
> (`arnabsnas.local`, user `Arnab`, `100.119.176.108`, `192.168.1.149`).
> Parameterize or scrub them — same cleanup as `docs/install-shipper.md`.

**Written 2026-08-04**, rewritten the same day after the first real run.
The original draft was written blind (the NAS was unreachable, see
"Appendix: the 2026-08-04 outage") and guessed at the layout. The layout is
now verified from the host itself and recorded below — no discovery phase
needed.

The NAS is Debian 12 bookworm, user `Arnab`, harnessd on port 7081,
`host_id: nas`. The hub is the Mac at `192.168.1.149:7080`.

## The layout (verified 2026-08-04)

| Thing | Value |
|---|---|
| systemd unit | `drover-nas-harnessd.service` — **user-level**, `~/.config/systemd/user/` |
| repo / `WorkingDirectory` | `/home/Arnab/dev/drover-harness-prod` — a real clone of `origin/main` |
| install mode | **editable** (`__editable__.drover-0.1.0.pth` → `<repo>/src`) |
| DuckDB | `/home/Arnab/.drover/drover.duckdb` — set by `duckdb_path` in `~/.drover/config.toml` |
| config | `/home/Arnab/.drover/config.toml` |
| agent CLIs | `codex`/`gemini` via nvm (`~/.nvm/versions/node/v24.13.0/bin`), `claude` via `~/.local/bin` |

The unit is **not** defined in this repo — it was written by hand on the NAS.
Copy of it lives at the bottom of this file so it can be recreated.

> Until 2026-08-04 the deploy directory was **not a git repo**: it was a pile of
> files copied over SMB from the Mac, complete with `._*` AppleDouble forks and
> `.pre-worktree-*` hand backups. `git pull` was impossible. It is now a proper
> clone, which is what makes Phase 3 below a two-line operation. The old tree is
> archived at `~/.drover/backups/drover-harness-prod-20260804-164818.tar.gz` and
> retired in place at `/home/Arnab/dev/drover-harness-prod.retired-20260804`;
> delete both once a few deploys have gone cleanly.

---

## Phase 0 — can the NAS start new processes?

Skip if `ssh Arnab@arnabsnas.local id` returns promptly, or if you are already
running on the NAS.

If SSH authenticates and then hangs, the machine usually cannot fork. Get in
via the console (DSM web terminal or physical) and check the four usual
causes:

```bash
df -h                      # full disk, especially / and /home
free -m                    # memory and swap exhausted
ps -eo pid | wc -l         # PID count vs `ulimit -u` and kernel.pid_max
dmesg -T | tail -50        # OOM killer, I/O errors, hung-task warnings
mount | grep -E 'nfs|cifs' # a wedged network mount hangs anything touching it
```

Hung-task or I/O errors against the volume holding `/home/Arnab` is the
common answer. A reboot is the usual fix; if the console itself cannot spawn
a shell, it is a power cycle.

**Do not continue until `ssh Arnab@arnabsnas.local id` returns.** Everything
below spawns processes.

## Phase 1 — back up

Do this every time. The backup is the only thing standing between a bad
release and lost history.

```bash
mkdir -p ~/.drover/backups
TS=$(date +%Y%m%d-%H%M%S)
systemctl --user stop drover-nas-harnessd     # quiesce the DB writer first
cp ~/.drover/drover.duckdb ~/.drover/backups/drover-$TS.duckdb
ls -lh ~/.drover/backups/
```

Stop before copying: DuckDB is single-writer and a hot copy can be torn.

If a release drops or rewrites a table, say so here. The 2026-08-04 release
dropped `harness_transcript_chunks` via `bootstrap_harness_tables` on first
start — irreversible, and code alone will not bring the rows back. That drop
was safe because every chunk row duplicated a `terminal.output` event
byte-for-byte (verified 1:1 on live data: 159 rows / 1,910 chars each for the
busiest session, identical content), and events additionally carry the
ANSI-cleaned `content_preview` that chunks never had.

## Phase 2 — update the code

```bash
cd /home/Arnab/dev/drover-harness-prod
git status --short           # stop if dirty; a pull would clobber local edits
git pull origin main
git log --oneline -1
uv sync                      # only if pyproject/uv.lock changed
```

The install is **editable**, so `src/` changes take effect on restart with no
reinstall. `uv sync` is still needed when dependencies move.

## Phase 3 — restart and verify

```bash
systemctl --user start drover-nas-harnessd
sleep 8
systemctl --user status drover-nas-harnessd --no-pager | head -10
curl -s localhost:7081/healthz; echo
```

Expect `{"active_sessions": 0, "host_id": "nas", "ok": true}`.

**Give it ~8 seconds.** Bind takes a few seconds after the unit reports
`active (running)`; a `healthz` at t+2s returns empty and looks like a failed
start. Wait for `drover-harnessd nas listening on 0.0.0.0:7081` in
`journalctl --user -u drover-nas-harnessd -n 20`.

Then verify against the **real** database — read the path from the config
rather than hardcoding it, so a typo cannot silently create an empty DuckDB
file and report a green result:

```bash
cd /home/Arnab/dev/drover-harness-prod
.venv/bin/python -c "
import duckdb, tomllib, pathlib
cfg = tomllib.loads(pathlib.Path.home().joinpath('.drover/config.toml').read_text())
path = cfg['paths']['duckdb_path']
con = duckdb.connect(path, read_only=True)
t = {r[0] for r in con.execute('select table_name from information_schema.tables').fetchall()}
print('db:', path)
print('harness tables:', sorted(x for x in t if x.startswith('harness_')))
print('events:', con.execute('select count(*) from harness_events').fetchone()[0])
"
```

Want `harness_events`, `harness_hosts`, `harness_sessions`, and a non-zero
event count.

Use `.venv/bin/python`, not `uv run python` with a `sys.path` hack — the venv
is what systemd actually executes, so it is the only interpreter whose import
resolution proves anything about the running daemon.

Release-specific check (2026-08-04, the Codex resume fix). This builds the
real argv rather than grepping for a string, so it fails if the flag ever
regresses in a way a text match would miss:

```bash
cd /home/Arnab/dev/drover-harness-prod
.venv/bin/python -c "
from drover.server.harness.structured.codex import CodexDriver
d = CodexDriver(['codex'], None, lambda m: None)
d._thread_id = 'thread-abc'
argv = d._argv_for('hi')
assert '--sandbox' not in argv, 'BROKEN: resume still passes --sandbox'
assert 'sandbox_mode=danger-full-access' in argv, 'BROKEN: no sandbox_mode override'
print('OK:', ' '.join(argv))
"
```

Finally, confirm the NAS is reporting into the fleet. This works from the NAS
itself; from the Mac use `127.0.0.1:7080`:

```bash
TOK=$(cat ~/.drover/api_token)
curl -s -H "Authorization: Bearer $TOK" http://192.168.1.149:7080/harness \
  | python3 -c "
import sys, json
for h in json.load(sys.stdin)['hosts']:
    print(h['host_id'], h['status'], h['last_seen_at'])
"
```

`nas` should be `online` with `last_seen_at` within a minute.

> Expand the token into a variable first. `-H 'Authorization: Bearer $(cat …)'`
> inside single quotes does not expand, and the endpoint answers
> `{"error": "authentication required"}` — which looks exactly like the daemon
> failing to register.

## Rollback

```bash
systemctl --user stop drover-nas-harnessd
cd /home/Arnab/dev/drover-harness-prod
git checkout <previous-sha>
uv sync                                    # only if deps moved
cp ~/.drover/backups/drover-<timestamp>.duckdb ~/.drover/drover.duckdb
systemctl --user start drover-nas-harnessd
sleep 8 && curl -s localhost:7081/healthz; echo
```

The database restore is the load-bearing step. Reverting the code without it
leaves any dropped table dropped.

---

## What the 2026-08-04 deploy fixed

Ordered by what actually mattered on the NAS.

1. **Codex sessions died on their second turn.** `codex exec resume` does not
   accept `--sandbox` (that flag exists only on the parent `codex exec`), so
   every follow-up turn aborted at arg-parse with
   `error: unexpected argument '--sandbox' found`. The first turn worked,
   which made it look like the session had simply stopped responding. Now
   uses `-c sandbox_mode=danger-full-access`. **This was the reason to deploy
   the NAS at all.**
2. **Structured handoffs carried no transcript.** Handoff prompts read only
   `harness_transcript_chunks`, which only the PTY terminal mirror ever wrote,
   so every chat-mode handoff shipped "Recent transcript: not available in
   central Drover yet." Transcripts now replay from `harness_events`.
3. **Registry writes no longer vanish.** Both write paths ended in a bare
   `except: pass`, so a DuckDB write-write conflict lost the event forever and
   invisibly. Now three attempts with backoff, then a `transcript.gap` marker
   and a counter exported as `drover_harness_dropped_events_total`.
4. **The fleet snapshot stopped copying the whole database per request.**
   Mostly a hub concern, little NAS impact.

Two things to watch on this release:

- `drover_harness_dropped_events_total` after a day of real use. Non-zero
  means the retry budget is too small for real contention — the retry path has
  only ever been exercised against simulated failures.
- **Resident memory.** The pre-deploy process sat at 10.7 MB RSS after 12 days;
  the new one settles around 117 MB. Some of that is transcript replay now
  reading from `harness_events`, but a 10x jump has not been explained. If it
  keeps climbing across days, that is a leak, not a baseline.

---

## The systemd unit

`~/.config/systemd/user/drover-nas-harnessd.service`, enabled, `default.target`.
Recorded here so it can be recreated; this repo does not install it.

```ini
[Unit]
Description=Drover NAS harness daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/Arnab/dev/drover-harness-prod
ExecStart=/home/Arnab/dev/drover-harness-prod/.venv/bin/drover-harnessd \
  --config /home/Arnab/.drover/config.toml \
  --host-id nas --display-name NAS --kind linux \
  --listen 0.0.0.0:7081 \
  --local-url http://100.119.176.108:7081 \
  --tailscale-url http://100.119.176.108:7081 \
  --central-url http://192.168.1.149:7080
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

(The real file has `ExecStart` on one line; it is wrapped here for reading.
If you recreate it from this block, keep the backslashes or unwrap it.)

Rebuilding the deploy directory from scratch, if it is ever lost:

```bash
cd /home/Arnab/dev
git clone https://github.com/arniesaha/drover drover-harness-prod
cd drover-harness-prod && uv sync
systemctl --user daemon-reload && systemctl --user restart drover-nas-harnessd
```

Build the venv **at the final path**. `uv sync` bakes absolute paths into the
venv's console scripts, so creating it elsewhere and renaming the directory
leaves `drover-harnessd` pointing at an interpreter that no longer exists.

---

## Appendix: the 2026-08-04 outage

Recorded because the symptom is misleading.

SSH authenticated normally and then hung with no output and no exit, on both
the Tailscale (`100.119.176.108`) and LAN (`arnabsnas.local`) paths. Ping was
clean at 3.4ms, 0% loss.

The obvious guess — a bad `.bashrc` or shell startup — was **wrong**:

| channel | uses login shell? | result |
|---|---|---|
| `ssh … 'id'` (exec) | yes | hang |
| `ssh -tt` (interactive) | yes | hang |
| `sftp` (sftp-server subsystem) | **no** | hang |

`sftp` bypasses the login shell entirely, so its hanging rules shell startup
out. Meanwhile harnessd — already running, files already open — kept serving
`/healthz` in 16ms.

Existing processes fine, new processes impossible: that is a host that cannot
fork. Check PIDs, memory, disk, and wedged mounts (Phase 0), not the network
and not SSH config.
