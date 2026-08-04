# NAS deploy runbook

How to update `drover-harnessd` on the NAS by hand.

> **Before this repo goes public:** this file contains host-specific values
> (`arnabsnas.local`, user `Arnab`, `100.119.176.108`). Parameterize or scrub
> them — same cleanup as `docs/install-shipper.md`.

**Written 2026-08-04**, while the NAS was unreachable for deploys (see
"Appendix: the 2026-08-04 outage"). Phase 2 is deliberately written as
discovery, not a recipe: **this repo does not define a `drover-harnessd`
systemd unit** (see `docs/multi-host.md`, "NAS direct (systemd host)"), so
whatever runs on the NAS was written there by hand and its unit name and
repo path have never been verified from this side.

The NAS is Debian 12 (`OpenSSH_9.2p1 Debian-2+deb12u6`), user `Arnab`,
harnessd on port 7081, `host_id: nas`.

---

## Phase 0 — can the NAS start new processes?

Skip only if `ssh Arnab@arnabsnas.local id` already returns promptly.

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

## Phase 1 — confirm forking works

```bash
ssh Arnab@arnabsnas.local id
```

**Do not continue until this returns.** Everything below spawns processes.

## Phase 2 — find the layout

```bash
systemctl --user list-units --type=service | grep -i drover
systemctl list-units --type=service | grep -i drover    # if not user-level
```

Then read the unit (substitute the real name):

```bash
systemctl --user cat drover-harnessd
```

Record two things:

- **`WorkingDirectory`** — the repo clone to update in Phase 4
- **`ExecStart`** — which decides whether Phase 4 needs `uv sync`

| `ExecStart` shape | Phase 4 needs |
|---|---|
| `…/uv run --quiet drover-harnessd …` | `git pull` **and** `uv sync` |
| `<repo>/.venv/bin/drover-harnessd …` | `git pull` **and** `uv sync` |
| editable install (`pip install -e`) | `git pull` only |

To tell an editable install from a normal one:

```bash
ls <venv>/lib/python*/site-packages/ | grep -i drover
# __editable__.drover-*.pth  -> editable: code is read live from the repo
# drover/ directory          -> copied install: must reinstall after pulling
```

If no unit matches, find it from the live process instead:

```bash
ps aux | grep -i harnessd | grep -v grep
```

## Phase 3 — back up before pulling

**This release drops a table.** `bootstrap_harness_tables` runs
`DROP TABLE IF EXISTS harness_transcript_chunks` on the first start after the
update. It is irreversible, and code alone will not bring the rows back.

```bash
mkdir -p ~/.drover/backups
cp ~/.drover/harnessd.duckdb \
   ~/.drover/backups/harnessd-$(date +%Y%m%d-%H%M%S).duckdb
ls -lh ~/.drover/backups/
```

If `~/.drover/harnessd.duckdb` does not exist, the data dir is elsewhere —
check the unit's `EnvironmentFile` and any `--duckdb`/`DROVER_*` settings.

Why the drop is safe: every chunk row duplicated a `terminal.output` event
byte-for-byte. Verified 1:1 on live data — 159 rows / 1,910 chars each for
the busiest session, identical content. Events also carry the ANSI-cleaned
`content_preview`, which chunks never had. Back up anyway.

## Phase 4 — update the code

```bash
cd <WorkingDirectory from Phase 2>
git status --short           # stop if dirty; a pull would clobber local edits
git pull origin main
git log --oneline -1         # expect ce760d0 or later
uv sync                      # unless Phase 2 showed an editable install
```

## Phase 5 — restart and verify

```bash
systemctl --user restart drover-harnessd     # real unit name from Phase 2
sleep 5
systemctl --user status drover-harnessd --no-pager | head -20
curl -s localhost:7081/healthz; echo
```

Expect `{"active_sessions": 0, "host_id": "nas", "ok": true}`.

Confirm the migration actually ran:

```bash
cd <repo>
uv run python -c "
import duckdb, os
con = duckdb.connect(os.path.expanduser('~/.drover/harnessd.duckdb'))
t = {r[0] for r in con.execute('select table_name from information_schema.tables').fetchall()}
print('chunks table gone:', 'harness_transcript_chunks' not in t)
print('harness tables:', sorted(x for x in t if x.startswith('harness_')))
"
```

Want `chunks table gone: True`, and the three surviving tables
`harness_events`, `harness_hosts`, `harness_sessions`.

Confirm the Codex fix is present — this is the one that actually matters for
the NAS (see "What this deploy fixes"):

```bash
cd <repo>
uv run python -c "
import sys; sys.path.insert(0,'src')
from drover.server.harness.structured.codex import CodexDriver
d = CodexDriver(['codex'], None, lambda m: None)
d._thread_id = 'thread-abc'
argv = d._argv_for('hi')
assert '--sandbox' not in argv, 'BROKEN: resume still passes --sandbox'
assert 'sandbox_mode=danger-full-access' in argv, 'BROKEN: no sandbox_mode override'
print('OK: codex resume argv is fixed')
print(' ', ' '.join(argv))
"
```

Expected output:

```
OK: codex resume argv is fixed
  codex exec resume thread-abc --json --skip-git-repo-check -c sandbox_mode=danger-full-access hi
```

This builds the real argv rather than grepping for a string, so it fails if
the flag ever regresses in a way a text match would miss.

Finally, from the **Mac**, confirm the NAS still reports into the fleet:

```bash
curl -s -H "Authorization: Bearer $(cat ~/.drover/api_token)" \
     http://127.0.0.1:7080/harness \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print([h['host_id'] for h in d['hosts']])"
```

The NAS should appear, and its `last_seen_at` should be within a minute.

## Rollback

```bash
systemctl --user stop drover-harnessd
cd <repo>
git checkout ce760d0~1
uv sync                                   # if not an editable install
cp ~/.drover/backups/harnessd-<timestamp>.duckdb ~/.drover/harnessd.duckdb
systemctl --user start drover-harnessd
curl -s localhost:7081/healthz; echo
```

The database restore is the load-bearing step. Reverting the code without it
leaves the table dropped.

---

## What this deploy fixes

Ordered by what actually matters on the NAS.

1. **Codex sessions died on their second turn.** `codex exec resume` does not
   accept `--sandbox` (that flag exists only on the parent `codex exec`), so
   every follow-up turn aborted at arg-parse with
   `error: unexpected argument '--sandbox' found`. The first turn worked,
   which made it look like the session had simply stopped responding. Now
   uses `-c sandbox_mode=danger-full-access`. **This is the reason to deploy
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
   Mostly a hub concern, so little NAS impact.

After a day of real use, check `drover_harness_dropped_events_total`. Non-zero
means the retry budget is too small for real contention — the retry path has
only ever been exercised against simulated failures.

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
