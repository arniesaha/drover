# Installing a Nexus shipper on a new host

For a single-user setup, `nexus-server` owns the local `~/.nexus/` store
on one workstation. Any additional personal host can run a **shipper** —
a small periodic job that bundles new agent session files into JSONL and
rsyncs them to the workstation's `~/.nexus/incoming/<host_id>/` directory.
The `nexus-server` watcher picks them up and merges them into the local
DuckDB + Parquet store.

In the current dogfood deployment, the receiving workstation is the Mac
mini (`Arnabs-Mac-mini.local`), so the examples below use
`arnabmac@Arnabs-Mac-mini.local:~/.nexus/incoming/<host_id>/`.

This guide is for adding a new shipping host. There are two flavors
depending on the OS:

| Host type | OS | Scheduler | Install script |
|---|---|---|---|
| NAS (`arnabsnas`) | Debian / Linux | systemd user timer | [`scripts/install_shipper_linux.sh`](../scripts/install_shipper_linux.sh) |
| Work MacBook | macOS | launchd | [`scripts/install_shipper_macos.sh`](../scripts/install_shipper_macos.sh) |

Both scripts are idempotent — re-running upgrades the install in place.

---

## Pre-flight (do this once before running either script)

The shipper rsyncs over SSH from the new host to the mac-mini. The new
host's SSH key must be in mac-mini's `authorized_keys`:

```bash
# On the new host (NAS or work MacBook)
ssh-copy-id arnabmac@Arnabs-Mac-mini.local
ssh -o BatchMode=yes arnabmac@Arnabs-Mac-mini.local "echo OK; mkdir -p ~/.nexus/incoming"
```

The second command should print `OK` without a password prompt. If it
asks for a password, the key isn't installed yet.

You also need `git` and `rsync` on the new host. The install scripts
will fetch `uv` if it's missing.

---

## NAS — `scripts/install_shipper_linux.sh`

Installs the shipper on a Debian-flavored Linux box. Defaults are tuned
for the NAS (`host_id=nas-claude`, sources: `claude_code` +
`openclaw`).

```bash
# On the NAS, as the Arnab user
git clone git@github.com:arniesaha/nexus.git ~/jenny/nexus  # or git pull if it exists
cd ~/jenny/nexus
bash scripts/install_shipper_linux.sh
```

The script:

1. Installs `uv` if missing (via the official curl-installer).
2. Runs `uv sync` to materialize the venv.
3. Writes `~/.nexus/collect.toml` with `host_id=nas-claude`,
   `remote_host=Arnabs-Mac-mini.local`, sources:
   - `claude_code` → `~/.claude/projects/`
   - `openclaw` → `~/.openclaw/agents/main/sessions/`
4. Renders the systemd unit templates from
   [`scripts/systemd/`](../scripts/systemd/) into `~/.config/systemd/user/`.
5. Enables `loginctl enable-linger $USER` so the timer runs without
   an active login.
6. `systemctl --user enable --now nexus-collect.timer`.
7. Triggers an immediate first run and shows the log tail.

Knobs (override via env if defaults don't match):

```bash
HOST_ID=nas-claude \
NAS_REMOTE_HOST=Arnabs-Mac-mini.local \
NAS_REMOTE_USER=arnabmac \
bash scripts/install_shipper_linux.sh
```

After it completes:

```bash
systemctl --user list-timers nexus-collect.timer
journalctl --user -u nexus-collect -n 30 --no-pager
ssh arnabmac@Arnabs-Mac-mini.local 'ls -la ~/.nexus/incoming/nas-claude/.processed/ | head'
```

---

## Work MacBook — `scripts/install_shipper_macos.sh`

Same idea, macOS-flavored. Defaults: `host_id=work-macbook-claude`,
sources: `claude_code` (the standard `~/.claude/projects/`).

```bash
# On the work MacBook
git clone git@github.com:arniesaha/nexus.git ~/jenny/nexus
cd ~/jenny/nexus
bash scripts/install_shipper_macos.sh
```

The script:

1. Installs `uv` if missing.
2. `uv sync`.
3. Writes `~/.nexus/collect.toml` with `host_id=work-macbook-claude`,
   `remote_host=Arnabs-Mac-mini.local`.
4. Renders `~/Library/LaunchAgents/com.arnab.nexus-collect.plist`
   (every 5 minutes via `StartInterval`).
5. `launchctl unload && launchctl load -w` to (re)install.
6. Triggers an immediate first run and tails `/tmp/nexus-collect.log`.

Knobs:

```bash
HOST_ID=work-macbook-claude \
REMOTE_HOST=Arnabs-Mac-mini.local \
REMOTE_USER=arnabmac \
bash scripts/install_shipper_macos.sh
```

After it completes, the next ship fires automatically every 5 minutes.

---

## Live verification (run from anywhere)

Once a host has shipped at least once, you can confirm landing from
the mac-mini side:

```bash
# On the mac-mini — see incoming + processed manifests
ls -lat ~/.nexus/incoming/*/        # what's queued
ls -lat ~/.nexus/incoming/*/.processed/  # what's been ingested

# Live row count by host
uv run python -c "
import duckdb
con = duckdb.connect('/Users/arnabmac/.nexus/nexus.duckdb', read_only=True)
for r in con.execute('''
  SELECT agent_id, count(DISTINCT session_id) AS sessions, count(*) AS events,
         max(TRY_CAST(timestamp AS TIMESTAMP)) AS last_seen
  FROM agent_events
  WHERE TRY_CAST(timestamp AS TIMESTAMP) > now() - INTERVAL 24 HOUR
  GROUP BY 1 ORDER BY 4 DESC
''').fetchall():
    print(r)
"
```

You should see at minimum: `macmini-claude` (from the mac-mini's own
shipper), plus any newly-installed hosts (`nas-claude`,
`work-macbook-claude`).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `rsync: connection unexpectedly closed` | SSH key not in mac-mini's `authorized_keys` | Run `ssh-copy-id arnabmac@Arnabs-Mac-mini.local` from the new host |
| `nexus-collect run` succeeds but the receiving host sees nothing | `host` field in collect.toml is wrong, or remote `~/.nexus/incoming/` doesn't exist | `ssh arnabmac@Arnabs-Mac-mini.local "mkdir -p ~/.nexus/incoming"` |
| New host's events appear under the wrong `agent_id` | `host_id` in collect.toml mis-set | Edit `~/.nexus/collect.toml` and restart the timer (`systemctl --user restart nexus-collect.timer` / `launchctl kickstart`) |
| Validation errors in the watcher logs about `token_usage` | Pre-2026-05-09 release of nexus on the receiving server | `git pull && systemctl restart` (or relaunch) the **mac-mini** server — the strict-int schema was relaxed in commit `ba7f287` |
| Shipper runs but the mac-mini watcher isn't running | `nexus-server run` not active on the mac-mini | On mac-mini: `nohup uv run nexus-server run > /tmp/nexus-server.log 2>&1 &` (long-term: install a launchd plist) |

---

## Hosts already shipping

| Host | OS | Scheduler | Sources |
|---|---|---|---|
| `Arnabs-Mac-mini.local` (receiving host itself) | macOS | launchd `com.arnab.nexus-collect` | claude_code, claude_macmini, hermes |
| `arnabsnas` | Debian | systemd `nexus-collect.timer` | claude_code, openclaw |
| Work MacBook | macOS | launchd `com.arnab.nexus-collect` | claude_code |

To add a fourth host, copy whichever install script matches the OS,
adjust `HOST_ID` + the source list, and run.
