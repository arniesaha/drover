# Plan 2 — `nexus-collect` Per-Host Shippers

**Status:** In implementation
**Date:** 2026-05-09
**Spec:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §3.1, §5.2

---

## Goal

Replace the legacy `scripts/sync_*.sh` shippers with one `nexus-collect` CLI per host. It walks each configured source, parses new events since the last cursor checkpoint, writes canonical `AgentEvent` JSONL to a staging dir, then ships via `rsync` to `mac-mini:~/.nexus/incoming/<host>/`. Cadence driven by launchd / systemd timers (every 5 minutes).

The server side already handles ingest (Plan 1's `IncomingWatcher` + `ingest_file`); collectors are pure producers.

---

## Non-goals (deferred)

- Hosted/SaaS shipper variant (OSS pluggability lands later).
- Resumable mid-batch state (cursor advances atomically per source per run; partial-failure replays are idempotent on the server via `dedup_key`).
- Encryption beyond what `rsync -e ssh` provides.

---

## Module layout

```
src/nexus/collect/
  __init__.py
  cursor.py        # CursorStore: read/write per-source mtime watermark with flock
  sources.py       # Source ABC + 5 concrete sources (claude_code, claude_macmini,
                   #   hermes, pi_mono, openclaw); each yields AgentEvent rows
  shipper.py       # ship_staging(host, host_id, staging_dir, dest) — rsync wrapper
  __main__.py      # Click CLI: init, run, status

tests/
  test_cursor.py
  test_collect_sources.py
  test_shipper.py
  test_collect_cli.py
  fixtures/collect/...

scripts/launchd/
  com.nexus.collect.plist
  README-nexus-collect.md
```

`pyproject.toml` adds console script `nexus-collect = "nexus.collect.__main__:main"`.

---

## Tasks (TDD: red → green → commit)

### T1. CursorStore

**File:** `src/nexus/collect/cursor.py`
**Test:** `tests/test_cursor.py`

```python
@dataclass(frozen=True)
class CursorStore:
    state_dir: Path

    def read(self, source_id: str) -> dict:
        """Return {} if no cursor exists yet."""

    def write(self, source_id: str, payload: dict) -> None:
        """Atomic: write to .tmp + os.replace. flock the source-specific lockfile."""

    def lock(self, source_id: str) -> ContextManager[None]:
        """Advisory flock on <state>/<source>.lock; raises CursorLocked on EAGAIN."""
```

Cursor payload: `{"watermark_iso": "<rfc3339>", "last_run_iso": "<rfc3339>"}`. Watermark is the max event timestamp shipped so far.

Tests:
- read on missing file → `{}`
- write then read round-trips
- two concurrent locks: second raises `CursorLocked`
- write is atomic across crash (simulate by leaving `.tmp` and asserting read still returns last good)

### T2. Source ABC + claude_code source

**Files:** `src/nexus/collect/sources.py`
**Test:** `tests/test_collect_sources.py`

```python
class Source(Protocol):
    id: str
    def list_files_since(self, watermark: datetime | None) -> list[Path]: ...
    def parse(self, path: Path) -> Iterator[AgentEvent]: ...

class ClaudeCodeSource:  # ~/.claude/projects/**/*.jsonl
class ClaudeMacMiniSource:  # ~/Library/Application Support/Claude/local-agent-mode-sessions/
class HermesSource:  # ~/.hermes/profiles/jenny/sessions/*.json
class PiMonoSource:  # ~/max/data/task-journal.db (sqlite, single file → newer-than-cursor row select)
class OpenClawSource:  # ~/.openclaw/agents/main/sessions/*.jsonl
```

Each source delegates parsing to existing `nexus.parsers` functions and just iterates over file selection.

Tests use fixture files under `tests/fixtures/collect/`. Each source has a happy-path test (newer-than-cursor returns the right files) and an empty-cursor test (returns everything).

### T3. JSONL writer

**Files:** `src/nexus/collect/sources.py` (one helper)
**Test:** `tests/test_collect_sources.py`

```python
def write_events_jsonl(events: Iterable[AgentEvent], staging_dir: Path, run_id: str) -> Path:
    """Write JSONL to <staging>/<source>-<run_id>.jsonl.tmp, fsync, atomic rename to .jsonl."""
```

Tests: rename atomicity, fsync called, JSONL parses back via `pydantic.TypeAdapter[AgentEvent].validate_json`.

### T4. Shipper (rsync wrapper)

**Files:** `src/nexus/collect/shipper.py`
**Test:** `tests/test_shipper.py`

```python
def ship_staging(*, staging_dir: Path, host: str, host_id: str, remote_root: str = "~/.nexus/incoming",
                 rsync: str = "rsync", extra_args: list[str] | None = None,
                 _runner: Callable | None = None) -> ShipResult:
    """rsync -av --remove-source-files <staging>/*.jsonl host:<remote_root>/<host_id>/"""
```

`_runner` injection for tests (no real rsync). Default uses `subprocess.run`.

Tests:
- Constructed command args correct
- Non-zero return code → raises `ShipError` with stderr
- No `*.jsonl` in staging → returns `ShipResult(files=0)` without calling rsync

### T5. CLI: `nexus-collect init`

**Files:** `src/nexus/collect/__main__.py`
**Test:** `tests/test_collect_cli.py`

```bash
nexus-collect init [--config PATH]
```

Writes `~/.nexus/collect.toml` with sensible defaults derived from this host's home dir, host_id from `socket.gethostname().split('.')[0]`. Skips if file exists unless `--force`.

Config schema:
```toml
host_id = "macmini"
remote_host = "mac-mini.local"
remote_user = "arnabmac"
state_dir = "~/.nexus/state"
staging_dir = "~/.nexus/staging"

[sources.claude_code]
enabled = true
root = "~/.claude/projects"

# (... per source ...)
```

### T6. CLI: `nexus-collect run`

**Files:** `src/nexus/collect/__main__.py`
**Test:** `tests/test_collect_cli.py` (integration with monkeypatched rsync)

```bash
nexus-collect run [--source <id>] [--dry-run]
```

Per enabled source, in order:
1. Lock cursor.
2. Load watermark.
3. List files newer than watermark.
4. Parse → events.
5. Write JSONL to staging.
6. ship_staging (skipped if `--dry-run`).
7. On rsync success, advance watermark to max(event.timestamp).
8. Release lock.

Failures in one source are logged and don't stop other sources. Exit code 0 unless every source failed.

### T7. CLI: `nexus-collect status`

**Files:** `src/nexus/collect/__main__.py`
**Test:** `tests/test_collect_cli.py`

Prints config + per-source watermark + last_run + staging file count. Pure read.

### T8. launchd plist + README

**Files:**
- `scripts/launchd/com.nexus.collect.plist`
- `scripts/launchd/README-nexus-collect.md`

`StartInterval = 300` (5 min). Logs to `~/Library/Logs/nexus-collect.{out,err}.log`.

### T9. pyproject.toml + README pointer

Register `nexus-collect` console script. Add a README line under the "Status" section pointing at this plan.

---

## Acceptance

- All new tests pass (`pytest`).
- Existing 36 tests still pass (no regressions).
- `nexus-collect init` writes a config under a temp HOME and exits 0.
- `nexus-collect run --dry-run` against fixture sources produces JSONL in staging that the server's `ingest_file` accepts (round-trip e2e).
- launchd plist passes `plutil -lint`.
