# Drover — Porting & Cutover Specification

Status: design of record (2026-07-07). How Nexus becomes Drover: the new-repo
seed, the `nexus→drover` rename map with compatibility aliases, what migrates
from the context layer, and the device-by-device cutover for the Mac Mini and
NAS. **Nothing here has been executed** — this is the spec the fresh session
plans and implements.

Source of truth for "current state": Nexus repo at `~/jenny/nexus`
(main @ `27588ed` — updated from the stale `5614750` pin; `27588ed` is the
merge of `worktree-drover-ios-cockpit`, +221 lines across `daemon.py`,
`metrics.py`, `web/app.py`, `tests/test_metrics.py`), live services on the
Mac Mini + NAS, data on `/Volumes/M2 1/nexus` (via the `~/.nexus` symlink).

---

## 1. Decision: new repo, clean seed

**Decision:** Drover is a **new git repository** at `/Volumes/M2 1/drover`,
seeded from the Nexus working tree (not its history). The Nexus repo is
retained, private, as an archive and as the source of the still-running fleet
until cutover completes.

Why not rename-in-place:
- OSS release (Nexus #177) requires a sanitized-history reset regardless; the
  Nexus history can never be published (private hostnames, paths, memory,
  possibly tokens in old commits). Doing the clean seed now = once, not twice.
- We relocate to `/Volumes/M2 1/drover` regardless (fresh clone + venv rebuild
  either way).
- Branded from commit 1, new structure, no legacy baggage; the new repo IS the
  OSS-ready seed.
- Compat aliases are runtime concerns (below), independent of git history.

**Seed procedure (fresh session executes):**
1. Copy the Nexus working tree into `/Volumes/M2 1/drover` as a **whitelist**
   (copy only what is named — never "copy everything then delete"). Keep:
   `src/`, `apps/`, `tests/`, `docs/`, `deploy/`, `scripts/`, `.github/`
   (CI workflow; rename its `nexus` strings), `pyproject.toml`, `uv.lock`,
   `LICENSE`, `README` (replace), `.gitignore`.
   Explicitly excluded (do not seed):
   - `.git/`, `.venv*/`, `.worktrees/`, `node_modules`, DerivedData, caches,
     and anything git-ignored.
   - `local_backup/`, `parquet/`, `*.duckdb*` (data stays in the archive /
     data volume, never in the repo).
   - `legacy/` — dead code, deliberately dropped.
   - `skills/`, `.claude/`, `.superpowers/` — session-local dogfood tooling;
     `skills/nexus` contains private hostnames and personal paths and must
     never enter the OSS-bound tree. Recreate a `drover` skill later if
     wanted.
   - Root junk tracked in Nexus git: `files_to_trigger.txt`,
     `trigger_batch_1.txt`, `function-source.zip`, `sample.jsonl`,
     `example_queries.sql`.
   - **`gcp-key.json` — a live GCP service-account key sitting untracked in
     the Nexus repo root. NEVER copy. If the whitelist is ever loosened
     during implementation, this file is the reason not to.**
2. Apply the rename map (§3) to the copied tree.
3. `git init`; author `arniesaha <arniesaha@gmail.com>`; first commit:
   `feat: seed Drover from Nexus (formerly Nexus)`. Keep these handoff docs
   under `docs/` as the founding design record.
4. Create `arniesaha/drover` on GitHub (private initially; public flip is the
   OSS-release sub-project, gated on the #177 sanitization checklist).
5. Do **not** delete or rename the Nexus repo yet — it runs the fleet.

## 2. Component inventory — keep / port / drop / defer

The Nexus Python package (`src/nexus/`) and its surfaces. Disposition for the
Drover seed:

| Component | What it is | Disposition |
| --- | --- | --- |
| `schema.py` + DuckDB/parquet lakehouse | The **context layer**: tables (tasks, session_summaries, project_briefs, decisions, session/span embeddings, harness_sessions/hosts/events/transcript_chunks, pipeline_*, context_containers, curated_context_*) + parquet (agent_events, spans, pr_events, routing) + views (agent_events, spans, spans_enriched, sessions, active_sessions, session_links, …) | **Port verbatim** (rename module path only). This is the moat; do not restructure during the port. Data migration in §4. |
| `server/metrics.py` (`MetricsCollector`) + `server/web/` | Central HTTP/JSON API, auth (bearer + signed cookie), UI pages, WS proxy, structured-session ingest, messages REST + stream | **Port.** Recent work (auth, headless protocol, HTTP/1.1 WS, session-action proxy) is current — carry as-is. |
| `server/harness/` | harnessd daemon (PTY + structured drivers for claude/codex/gemini), registry, websocket, cli | **Port.** The structured-driver work is the spine of pillar 1. |
| `server/summarizer/`, `server/embeddings/` | Local-model summarization + embeddings (Ollama) | **Port.** Local-first pillar. |
| `server/mcp/` | MCP server exposing `nexus_*` tools to agents | **Port + rename tools** `nexus_*` → `drover_*` with `nexus_*` aliases kept for one release (agents have them wired). |
| `server/otlp/` (:4317) | OTLP gRPC receiver for spans | **Port** (out of scope for auth/rename deep-touch; keep working). |
| `collect/` (shipper, tempo_relay, cursor) | Event shipper + Tempo→OTLP relay | **Port.** |
| `hook/` | Claude Code hook integration | **Port.** |
| `apps/drover/` (NexusKit + SwiftUI iOS app) | The cockpit client | **Already named Drover** — port as-is; only the server URLs/token env it points at change at runtime (§3). |
| `agent_aliases.py`, `event_identity.py`, `attribution.py`, `dedup.py`, `parsers.py`, `session_audit.py`, `task_id.py`, `context_containers.py`, `models.py` | Supporting libs | **Port verbatim** (module path rename only). |
| `deploy/kubernetes/`, `deploy/grafana/` | NAS k3s Prometheus ServiceMonitor + Grafana dashboard | **Port**; update label/name strings in the rename pass. |
| `local_backup/`, root `parquet/` | Cold CSV exports + old parquet (already symlinked to `/Volumes/M2 1/nexus-archive`) | **Do not seed** into the repo. Leave in the archive. |
| Git history | Private paths/memory/tokens | **Drop** (that's the point of the new repo). |

**Defer to their own specs (not in the port):** OSS sanitization (#177), the
design-system visual pass, and net-new capabilities (decision extraction #17,
span embeddings #16, self-learning #178). The port's job is a *faithful,
renamed, relocated* Drover that runs the current fleet — not new features.

## 3. Rename map (`nexus → drover`) with compat aliases

**Decision:** staged rename with compatibility aliases so the live fleet never
drops during cutover. Accept both old and new for one release, then remove the
aliases in a later cleanup.

| Surface | Nexus | Drover | Compat alias (transition) |
| --- | --- | --- | --- |
| Python package | `src/nexus/` | `src/drover/` | — (internal; update all imports) |
| Import root | `import nexus.…` | `import drover.…` | none needed (fresh code) |
| CLI: server | `nexus-server` | `drover-server` | ship a `nexus-server` shim entry point that execs the new one + prints a deprecation line |
| CLI: harnessd | `nexus-harnessd` | `drover-harnessd` | `nexus-harnessd` shim |
| CLI: collect | `nexus-collect` | `drover-collect` | `nexus-collect` shim |
| CLI: hook | `nexus-hook` | `drover-hook` | `nexus-hook` shim |
| CLI: dogfood smoke | `nexus-dogfood-smoke` | `drover-dogfood-smoke` | internal tool — no shim needed (was missing from the first draft of this map) |
| Config dir | `~/.nexus/` | `~/.drover/` | during transition, symlink `~/.nexus → ~/.drover` (both resolve) |
| Config file | `~/.nexus/config.toml` | `~/.drover/config.toml` | loader reads `~/.drover/config.toml`, falls back to `~/.nexus/config.toml` |
| Token file | `~/.nexus/api_token` | `~/.drover/api_token` | falls back to `~/.nexus/api_token` if new absent |
| Env var | `NEXUS_API_TOKEN` | `DROVER_API_TOKEN` | resolution accepts **both** for one release (prefer DROVER_) |
| Token resolution order | `NEXUS_API_TOKEN` > config > file | `DROVER_API_TOKEN` > `NEXUS_API_TOKEN` > config `[auth]` > `~/.drover/api_token` > `~/.nexus/api_token` | — |
| MCP tools | `nexus_*` | `drover_*` | register both names for one release |
| Web session cookie | `nexus_session` (HMAC key `nexus-session:<token>`) | `drover_session` / `drover-session:<token>` | none — costs one web re-login, no fleet impact. Bearer-token clients (iOS, machine) unaffected. |
| Env knobs (non-token) | `NEXUS_EMBEDDINGS_*`, `NEXUS_QUALITY_*`, summarizer `NEXUS_*` | `DROVER_*` | resolution accepts both for one release (prefer DROVER_) — same policy as the API token |
| launchd labels (Mac) | `com.nexus.server`, `com.nexus.redis`, `com.arnab.nexus-collect`, `com.nexus.mac-ollama-embeddings`, `com.nexus.ollama-tunnel`, `com.nexus.mac-ollama-idle-reaper` | `com.drover.*` | new plists; bootout old only after new verified |
| systemd (NAS) | `nexus-nas-harnessd`, `nexus-tempo-relay`, `nexus-collect.timer` | `drover-nas-harnessd`, etc. | new units; stop old only after new verified |
| Data volume | `/Volumes/M2 1/nexus` | `/Volumes/M2 1/drover` (repo) + `/Volumes/M2 1/drover-data` (runtime data) | keep `/Volumes/M2 1/nexus` until data migrated |
| Redis stream prefix | `nexus:jobs` | `drover:jobs` | drain old streams before switching, or dual-read one release |
| Repo | `arniesaha/nexus` (archive) | `arniesaha/drover` (new) | — |
| Swift package (iOS) | `apps/drover/NexusKit` | `DroverKit` | **Deferred — not part of this port.** Xcode package renames are churny; the app already works. MUST be renamed before the OSS public flip (a `NexusKit` in the public tree undercuts "branded from commit 1"). Tracked here so it is not forgotten. |

Ports stay the same (7080 metrics, 7077 MCP, 4317 OTLP, 7081 harnessd) — no
reason to churn them; the Drover iOS app already targets them.

**Note on Redis (`redis:jobs` prefix):** the summarizer/embeddings/brief job
queues run through Redis Streams. Safest cutover: quiesce workers, let the old
streams drain to empty, then start Drover workers on the new prefix. Document
the drain check in the runbook.

## 4. Context-layer data migration (DuckDB + parquet)

The durable data is the moat — migrate it intact, do not regenerate.

**Current data** (on `/Volumes/M2 1/nexus`, via `~/.nexus`):
- `nexus.duckdb` — central metadata store (~173 MB).
- `harnessd.duckdb` (Mac) — the harnessd's own registry (separate DB; see the
  boot-order note — Mac harnessd MUST use its own DB to avoid a lock conflict
  with the central server).
- `parquet/` — agent_events, spans, pr_events, routing partitions.
- `incoming/` — shipper landing zone per host.
- `state/`, `redis/`, `backups/`, `staging/`, `api_token`, `config.toml`.

**Migration:**
1. Move/copy `/Volumes/M2 1/nexus/` → `/Volumes/M2 1/drover-data/` (runtime
   data lives **beside** the repo, not inside it — keep the repo clean).
   Rename `nexus.duckdb` → `drover.duckdb`, `harnessd.duckdb` unchanged in
   name is fine (it's host-local).
2. Point the new `~/.drover/config.toml` `[paths]` at
   `/Volumes/M2 1/drover-data/{duckdb,parquet,incoming}`.
3. The DuckDB schema is bootstrapped idempotently (`schema.py` uses
   `CREATE … IF NOT EXISTS`), so opening the migrated DB with the ported code
   is safe. No schema migration needed for the rename — table/column names do
   NOT contain "nexus" (verified: they're `harness_sessions`, `spans`,
   `tasks`, etc.). **Do not rename any DuckDB table/column** — only the file
   path and package change. The same rule applies to **span attribute keys**
   (`nexus.decision.*`, `nexus.tool.*` in `decisions.py`): they are data
   schema stored in existing parquet/DuckDB rows — renaming them breaks reads
   of migrated data. They keep the `nexus.` prefix until a deliberate
   dual-write migration (post-cutover, if ever).
4. Verify row counts before/after the copy (a `SELECT count(*)` sweep over the
   expected tables — `schema.py` has `EXPECTED_TABLES`/`EXPECTED_VIEWS`).
5. Keep the original `/Volumes/M2 1/nexus` intact until the Drover fleet is
   verified end-to-end; it is the rollback.

**Do NOT migrate:** `local_backup/`, root `parquet/` archive
(`/Volumes/M2 1/nexus-archive`) — those stay archived.

## 5. Device cutover plan

Order matters: **stand up Drover alongside Nexus, verify, then decommission
Nexus per device.** Never leave the fleet tokenless or double-bound to one
DuckDB.

### 5a. Mac Mini (this machine — central + a harnessd host)

> **STATUS: executed 2026-07-07.** Data migrated to
> `/Volumes/M2 1/drover-data` (22 tables / 310,408 rows verified identical);
> `~/.drover` and `~/.nexus` both symlink there; all seven `com.drover.*`
> units live (server, redis, harnessd — now a real plist, collect,
> ollama-tunnel, mac-ollama-embeddings, mac-ollama-idle-reaper); old
> `com.nexus.*`/`com.arnab.nexus-collect` plists renamed
> `*.bak-cutover-20260707` and booted out. Verified: healthz, 401/200 auth
> gate, UI redirect, both hosts online (NAS re-registered with same token),
> MCP drover_*+nexus_* dispatch, workers started, two-turn structured session
> lifecycle (turn→stream→complete). Outstanding: Local Network grant for the
> Drover python (see §6) before claude sessions can reach the AgentWeave
> proxy. `/Volumes/M2 1/nexus` untouched — rollback = flip symlinks back +
> restore .bak plists.

Runs today (launchd + one orphan process):
- `com.nexus.server` (central: metrics 7080, MCP 7077, OTLP 4317)
- `com.nexus.redis`, `com.arnab.nexus-collect`,
  `com.nexus.mac-ollama-embeddings`, `com.nexus.ollama-tunnel`,
  `com.nexus.mac-ollama-idle-reaper`
- Mac harnessd — **orphan process, no plist** — launched with
  `--config ~/.nexus/harnessd-config.toml` (its own `harnessd.duckdb`, to
  avoid the DuckDB lock conflict with central). **Recommendation for Drover:
  give the Mac harnessd a real launchd plist** (`com.drover.harnessd`) so it
  survives reboots instead of being a manual relaunch.
- `com.arnab.mount-m2` already auto-mounts `/Volumes/M2 1` at boot (good — the
  data volume dependency is handled).

Cutover:
1. Build Drover in `/Volumes/M2 1/drover` (fresh venv, `uv sync --all-extras`).
2. Migrate data (§4) → `/Volumes/M2 1/drover-data`; write `~/.drover/`
   (config.toml, api_token — reuse the SAME token so nothing needs re-auth;
   the token is not renamed, only its file path).
3. Author `com.drover.*` launchd plists pointing at the new venv binaries +
   `~/.drover` config. **Start central FIRST, then harnessd** (boot-order
   invariant: central holds the persistent DuckDB connection; a harnessd on
   the same DB would 409 — that's why the Mac harnessd has its own DB).
4. Verify: healthz 200, auth gate (401/200), UI redirect, both hosts online,
   a live structured session + two-turn conversation through central, MCP
   tools reachable, summarizer/embeddings workers running.
5. Only then `launchctl bootout` the `com.nexus.*` units and stop the orphan
   Nexus harnessd. Keep the plists (`.bak`) for rollback.

### 5b. NAS (192.168.1.70, NixOS — a harnessd host)

Runs today (systemd **user** units; see the NAS-ops runbook — ssh exec is
broken, pipe scripts via stdin to `/bin/sh`):
- `nexus-nas-harnessd.service` — repo at `/home/Arnab/dev/nexus-harness-prod`,
  own `harnessd.duckdb`, listens `0.0.0.0:7081`, central-url the Mac's IP.
- `nexus-tempo-relay.service` + `.timer`, `nexus-collect.timer`.
- k3s Prometheus ServiceMonitor scraping the Mac's `/metrics` (needs the
  bearer token via a k8s secret — already configured).

Cutover:
1. Clone/seed Drover to `/home/Arnab/dev/drover-harness-prod` (or repoint the
   existing checkout at the new repo remote once it exists). NixOS: keep the
   same venv/uv approach it uses today.
2. Write `~/.drover/api_token` (same token) on the NAS; keep `~/.nexus`
   symlink → `~/.drover` for the transition.
3. Author `drover-nas-harnessd.service` (+ tempo-relay, collect timer) as new
   systemd user units pointing at the new checkout. Update the central-url if
   the Mac's Drover central moves (it doesn't — same host, same ports).
4. **Ordering:** the harnessd host must have the token BEFORE central enforces
   auth (already true — same token). Start `drover-nas-harnessd`, verify it
   registers + heartbeats to central and answers `401` unauth / `200` bearer.
5. Stop the old `nexus-nas-harnessd` only after the Drover one is confirmed
   online in the central `/harness` host list.
6. Prometheus: the ServiceMonitor + `nexus-api-token` secret keep working
   (same token, same port); rename the k8s objects in a later tidy pass, not
   during cutover.

### 5c. GPU PC (arnab-ubuntu, 10.10.10.2, RTX 5090 — model host, not harnessd)

Ollama model host, woken via the NAS WoL relay (`http://192.168.1.70:9753/wake`,
cold-start 30–90 s). Not a harnessd host today. **No cutover needed** unless
Drover starts routing summarizer/embeddings there (`[summarizer] gpu_relay_url`
/ `gpu_ollama_url` are currently empty — local Mac Ollama on :11435 is used).
If enabled later, it's a config change, not a service migration.

## 6. Runbook invariants to carry forward (do not relearn the hard way)

These are current operational truths from running the Nexus fleet; the Drover
cutover must preserve them:

- **Boot order (same host):** start central before harnessd; the Mac harnessd
  uses its own DuckDB (`--config …/harnessd-config.toml`) or it 409s on the
  central DB lock.
- **Strict WS clients:** the server must send `HTTP/1.1` on WebSocket upgrades
  (URLSessionWebSocketTask rejects `HTTP/1.0 101`). Already fixed on Nexus
  main — carry it.
- **Central proxies session actions:** `POST /harness/sessions/<id>/turns |
  permission | interrupt` must be proxied by central to the owning host.
  Already fixed on Nexus main — carry it.
- **NAS shell quirk:** ssh command-arg exec produces no output / scp hangs;
  pipe scripts via stdin to `/bin/sh`; kubectl needs
  `KUBECONFIG=/home/Arnab/.kube/config`.
- **DuckDB concurrency:** the registry serializes connects per resolved DB path
  (module-level lock) — a per-connect-per-thread model corrupts under load.
  Already fixed on Nexus main — carry it.
- **Token hygiene:** never log the token; k8s secret must be newline-stripped
  or Prometheus sends an invalid Authorization header.
- **M2 dependency:** all runtime data is on `/Volumes/M2 1`; if it's unmounted
  at boot, services crash-loop until it mounts. `com.arnab.mount-m2` handles
  this on the Mac — ensure the Drover plists don't race it (add a mount check
  or launchd dependency).
- **launchd cannot exec from the M2 volume (learned during the 2026-07-07 Mac
  cutover):** macOS TCC denies `launchd` agents executing binaries/scripts on
  `/Volumes/M2 1` ("Operation not permitted"). Reads/writes of data there are
  fine. Hence the split: repo on `/Volumes/M2 1/drover`, but the **runtime
  venv lives at `~/.drover-venv`** (editable install of the repo) and the
  reaper script is copied to `~/.local/bin/`. All `com.drover.*` plists exec
  from home paths only.
- **macOS Local Network privacy denies LAN egress to launchd pythons (learned
  2026-07-07):** any launchd-spawned python (old Nexus venv AND new Drover
  venv — verified both) gets `EHOSTUNREACH` connecting to LAN IPs like the
  NAS (`192.168.1.70`); localhost and internet egress are unaffected. This is
  why the old Mac harnessd "worked" as a terminal-launched orphan: it
  inherited the terminal's Local Network grant, so its claude children could
  reach the AgentWeave proxy (`ANTHROPIC_BASE_URL=http://192.168.1.70:30400`
  in `~/.claude/settings.json`). Under the new `com.drover.harnessd` plist,
  claude sessions failed with `FailedToOpenSocket`. The System Settings →
  Local Network toggle for a bare `python3` binary does NOT stick (known
  macOS behavior for non-bundled executables) — do not waste time on it.
  **Resolution (2026-07-07):** Apple-signed `/usr/bin/ssh` is exempt from
  the gate, so `com.drover.agentweave-tunnel` forwards `127.0.0.1:30400 →
  192.168.1.70:30400` and `~/.claude/settings.json` now points
  `ANTHROPIC_BASE_URL`/`AGENTWEAVE_PROXY_URL` at `http://127.0.0.1:30400`
  (localhost is exempt). Verified end-to-end: structured claude session
  through central returned a model reply, and the interrupt action proxied
  correctly. Central→NAS session-action proxying remains gated (latent on
  the OLD fleet too) — address during the NAS cutover (advertise a
  tunneled/tailscale URL, or the same ssh-forward trick for :7081).

## 7. Suggested cutover sequence (one line each)

1. Create `arniesaha/drover` (private); seed the tree per §1; apply the rename
   map per §3; first commit.
2. Build + venv on the Mac; migrate the context layer per §4 to
   `/Volumes/M2 1/drover-data`; write `~/.drover`.
3. Stand up Drover central + Mac harnessd (new plists) alongside Nexus; verify
   the full matrix; then bootout Nexus on the Mac.
4. Stand up Drover harnessd on the NAS; verify it registers; stop the Nexus
   NAS units.
5. Repoint the iOS app's server URL (unchanged host/port — likely no change);
   confirm end-to-end from the phone.
6. Soak for a few days on the compat aliases; then remove `nexus-*` shims,
   `~/.nexus` symlink, and `NEXUS_API_TOKEN` acceptance in a cleanup commit.
7. (Separate spec) OSS sanitization + public repo flip (#177).

## 8. Open questions for the fresh session

- Runtime data path: `/Volumes/M2 1/drover-data` (beside repo) vs
  `~/.drover` symlinked to it — recommend the latter for path stability
  (matches how `~/.nexus` works today).
- Keep the same shared token through cutover (recommended — zero re-auth) or
  rotate at the same time (cleaner break, but every device + the k8s secret
  must update in lockstep). Recommend: keep during cutover, rotate in the
  post-cutover cleanup.
- Whether to give the Mac harnessd a real launchd plist now (recommended) or
  keep the orphan-process pattern (fragile across reboots).
