# Nexus Foundation + Server Skeleton + File-Watcher Ingest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land a working `nexus-server` daemon on the Mac Mini that ingests canonical `AgentEvent` JSONL files dropped into `~/nexus/incoming/<host>/` and MERGEs them into Parquet tables in a local DuckDB lakehouse, idempotently and with computed `task_id`.

**Architecture:** Single Python daemon with a `watchdog`-based file watcher and an ingest function that uses DuckDB's COPY/MERGE semantics over Hive-partitioned Parquet. New tables (`tasks`, `session_summaries`) are created at bootstrap. All identity logic (dedup_key, task_id) is extracted into pure-function modules so it can be re-used by the future `nexus-collect` collectors and the OTLP receiver.

**Tech Stack:** Python 3.11, DuckDB, PyArrow, Pydantic v2, watchdog, click. No GCP dependencies introduced (existing GCP deps stay until the cloud_function decommission plan).

**Spec reference:** `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md`

**This plan covers spec sections:** §3.1 nexus-server, §4 data model (schema bootstrap), §5.2 file watcher path, §11 OSS hygiene (license + config), partial §8 (steps 1, 5, 6 of migration path).

**Out of scope (future plans):**
- `nexus-collect` per-host shippers — Plan 2
- OTLP receiver / AgentWeave push integration — Plan 3
- MCP server + handoff tools — Plan 4
- Summarizer worker — Plan 5
- `nexus-hook` lifecycle integration — Plan 6
- GCP / cloud_function decommission — Plan 7

---

## File structure

**Created:**
- `LICENSE` — Apache-2.0 text
- `src/nexus/dedup.py` — `make_dedup_key()` extracted from `cloud_function/main.py`
- `src/nexus/task_id.py` — `compute_task_id()` and `parse_repo_url()` helpers
- `src/nexus/config.py` — Loads `~/.nexus/config.toml`, exposes `NexusConfig` dataclass with paths
- `src/nexus/schema.py` — DuckDB schema bootstrap (creates Parquet dirs, tables, views)
- `src/nexus/server/__init__.py` — Empty package marker
- `src/nexus/server/__main__.py` — `nexus-server` CLI entry point (click)
- `src/nexus/server/ingest.py` — `ingest_file(path)` reads canonical JSONL → MERGE into Parquet
- `src/nexus/server/watcher.py` — watchdog observer over `~/nexus/incoming/`, dispatches to ingest
- `tests/test_dedup.py`, `tests/test_task_id.py`, `tests/test_config.py`, `tests/test_schema.py`, `tests/test_ingest.py`, `tests/test_watcher.py`, `tests/test_server_cli.py`
- `tests/fixtures/incoming/sample_agent_events.jsonl` — A few canonical AgentEvents for fixtures
- `tests/fixtures/nexus_config.toml` — Sample config
- `scripts/launchd/com.nexus.server.plist` — launchd unit for Mac Mini

**Modified:**
- `pyproject.toml` — add deps (`duckdb`, `pyarrow`, `watchdog`, `tomli; python_version<'3.11'`), add `nexus-server` console script
- `.gitignore` — add `.nexus/`, `nexus.duckdb`, `*.duckdb-*`
- Repo root: untrack the existing committed `nexus.duckdb`

---

## Task 1: Repo hygiene — license, gitignore, untrack DB, deps

**Files:**
- Create: `LICENSE`
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Untrack (not delete from disk): `nexus.duckdb` at repo root

- [ ] **Step 1: Add Apache-2.0 LICENSE**

Create `LICENSE` with the standard Apache 2.0 text. Use the official template from https://www.apache.org/licenses/LICENSE-2.0.txt. Set `[yyyy]` to `2026` and `[name of copyright owner]` to `Arnab Saha`.

```bash
curl -sL https://www.apache.org/licenses/LICENSE-2.0.txt -o LICENSE
```

Then edit the bottom appendix-application stanza if present (the canonical text doesn't have placeholders to fill — no edit needed).

- [ ] **Step 2: Verify LICENSE**

Run: `head -5 LICENSE`
Expected: starts with `Apache License`, `Version 2.0, January 2004`.

- [ ] **Step 3: Untrack the committed `nexus.duckdb`**

The PR review flagged this — `*.duckdb` is in `.gitignore` but the file was committed before the ignore rule.

```bash
git rm --cached nexus.duckdb
```

- [ ] **Step 4: Extend .gitignore**

Replace the existing `.gitignore` lakehouse section to also exclude `~/.nexus` artifacts and IDE noise. Open `.gitignore` and add after the existing `*.duckdb` line (line 29):

```
*.duckdb-wal
*.duckdb-shm

# Nexus runtime state (per-host home dir, not in repo)
.nexus/

# IDE
.idea/
.vscode/
.claude/
```

- [ ] **Step 5: Update pyproject.toml dependencies**

Open `pyproject.toml` and replace the `dependencies` list with:

```toml
dependencies = [
    "pydantic>=2.0",
    "click>=8.1",
    "duckdb>=1.0",
    "pyarrow>=15.0",
    "watchdog>=4.0",
    # GCP deps retained until cloud_function/ is decommissioned in a later plan:
    "google-cloud-storage",
    "google-cloud-bigquery",
    "google-cloud-aiplatform",
    "sqlalchemy",
    "pg8000",
    "cloud-sql-python-connector[pg8000]",
    "requests",
]
```

Then add a `nexus-server` console script alongside the existing `nexus-cli`:

```toml
[project.scripts]
nexus-cli = "nexus.cli:main"
nexus-server = "nexus.server.__main__:main"
```

- [ ] **Step 6: Install + verify**

```bash
uv pip install -e ".[dev]"  # or: pip install -e ".[dev]"
python -c "import duckdb, pyarrow, watchdog; print('ok')"
```

Expected: `ok`.

- [ ] **Step 7: Commit**

```bash
git add LICENSE .gitignore pyproject.toml
git rm --cached nexus.duckdb 2>/dev/null || true   # already staged in step 3
git commit -m "chore: add Apache-2.0 LICENSE, drop committed duckdb, add server deps"
```

---

## Task 2: Extract `make_dedup_key` into a reusable module

**Files:**
- Create: `src/nexus/dedup.py`
- Create: `tests/test_dedup.py`

The current `_make_dedup_key` lives in `src/nexus/cloud_function/main.py:77` and is private. The new server, the future OTLP receiver, and the future `nexus-collect` shippers all need this same function. Extract it as a public module.

- [ ] **Step 1: Write failing test**

Create `tests/test_dedup.py`:

```python
"""Tests for src/nexus/dedup.py."""
from nexus.dedup import make_dedup_key


def test_same_inputs_produce_same_key():
    a = make_dedup_key(
        timestamp_iso="2026-05-08T10:00:00Z",
        agent_id="macmini-claude",
        session_id="abc-123",
        event_type="user_message",
        content="hello world",
    )
    b = make_dedup_key(
        timestamp_iso="2026-05-08T10:00:00Z",
        agent_id="macmini-claude",
        session_id="abc-123",
        event_type="user_message",
        content="hello world",
    )
    assert a == b


def test_different_timestamps_produce_different_keys():
    a = make_dedup_key("2026-05-08T10:00:00Z", "x", "y", "z", "c")
    b = make_dedup_key("2026-05-08T10:00:01Z", "x", "y", "z", "c")
    assert a != b


def test_content_truncated_at_200_chars():
    short = "x" * 200
    long = "x" * 500
    # Same first 200 chars → same key
    assert make_dedup_key("t", "a", "s", "e", short) == make_dedup_key(
        "t", "a", "s", "e", long
    )


def test_handles_none_inputs():
    """Cloud Function calls this with None when fields missing."""
    key = make_dedup_key(None, None, None, None, None)
    assert isinstance(key, str)
    assert len(key) == 64  # sha256 hex


def test_returns_64_char_hex():
    key = make_dedup_key("t", "a", "s", "e", "c")
    assert len(key) == 64
    int(key, 16)  # raises if not valid hex
```

- [ ] **Step 2: Run test, confirm failure**

```bash
pytest tests/test_dedup.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.dedup'`.

- [ ] **Step 3: Implement**

Create `src/nexus/dedup.py`:

```python
"""Deterministic deduplication key for AgentEvent rows.

Used by the file-watcher ingest path, the OTLP receiver, and the future
nexus-collect shippers so that re-delivering the same event always produces
the same key — letting the lakehouse MERGE on dedup_key be a no-op on retry.

Fingerprint fields: timestamp | agent_id | session_id | event_type | content[:200]
"""
import hashlib
from typing import Optional


def make_dedup_key(
    timestamp_iso: Optional[str],
    agent_id: Optional[str],
    session_id: Optional[str],
    event_type: Optional[str],
    content: Optional[str],
) -> str:
    """Return SHA-256 hex of the stable business-field fingerprint."""
    fingerprint = "|".join(
        [
            timestamp_iso or "",
            agent_id or "",
            session_id or "",
            event_type or "",
            (content or "")[:200],
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test, confirm pass**

```bash
pytest tests/test_dedup.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/dedup.py tests/test_dedup.py
git commit -m "feat(dedup): extract make_dedup_key into reusable module"
```

---

## Task 3: Task-ID derivation module

**Files:**
- Create: `src/nexus/task_id.py`
- Create: `tests/test_task_id.py`

Per spec §4.1, `task_id = sha256(coalesce($NEXUS_TASK_ID, repo_owner||"/"||repo_name||"@"||branch))[:16]`.

- [ ] **Step 1: Write failing test**

Create `tests/test_task_id.py`:

```python
"""Tests for src/nexus/task_id.py."""
import os
from nexus.task_id import compute_task_id, parse_repo_url


def test_branch_default_derivation():
    a = compute_task_id(env_task_id=None, repo_owner="arniesaha", repo_name="nexus", branch="main")
    b = compute_task_id(env_task_id=None, repo_owner="arniesaha", repo_name="nexus", branch="main")
    assert a == b
    assert len(a) == 16


def test_env_override_wins():
    a = compute_task_id(env_task_id="my-task-001", repo_owner="x", repo_name="y", branch="z")
    b = compute_task_id(env_task_id="my-task-001", repo_owner="diff", repo_name="diff", branch="diff")
    assert a == b


def test_different_branches_produce_different_ids():
    a = compute_task_id(None, "arniesaha", "nexus", "main")
    b = compute_task_id(None, "arniesaha", "nexus", "feature/foo")
    assert a != b


def test_branch_none_falls_back_to_HEAD():
    # Non-git context: still produces a stable id (uses literal "HEAD")
    a = compute_task_id(None, "arniesaha", "nexus", None)
    b = compute_task_id(None, "arniesaha", "nexus", None)
    assert a == b


def test_parse_repo_url_ssh():
    owner, name = parse_repo_url("git@github.com:arniesaha/nexus.git")
    assert owner == "arniesaha"
    assert name == "nexus"


def test_parse_repo_url_https():
    owner, name = parse_repo_url("https://github.com/arniesaha/nexus.git")
    assert owner == "arniesaha"
    assert name == "nexus"


def test_parse_repo_url_no_dot_git():
    owner, name = parse_repo_url("https://github.com/arniesaha/nexus")
    assert owner == "arniesaha"
    assert name == "nexus"


def test_parse_repo_url_returns_none_none_on_garbage():
    owner, name = parse_repo_url("not-a-url")
    assert owner is None
    assert name is None
```

- [ ] **Step 2: Run test, confirm failure**

```bash
pytest tests/test_task_id.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.task_id'`.

- [ ] **Step 3: Implement**

Create `src/nexus/task_id.py`:

```python
"""Task-ID derivation per spec §4.1.

task_id = sha256(coalesce($NEXUS_TASK_ID, repo_owner/repo_name@branch))[:16]

Used everywhere a row is written so multi-session work on the same
(repo, branch) joins back together regardless of which agent ran it.
"""
import hashlib
import re
from typing import Optional, Tuple


_REPO_URL_RE = re.compile(
    r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def compute_task_id(
    env_task_id: Optional[str],
    repo_owner: Optional[str],
    repo_name: Optional[str],
    branch: Optional[str],
) -> str:
    """Return a 16-char hex task ID."""
    if env_task_id:
        raw = env_task_id
    else:
        owner = repo_owner or "unknown"
        name = repo_name or "unknown"
        br = branch or "HEAD"
        raw = f"{owner}/{name}@{br}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_repo_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a git remote URL into (owner, repo_name).

    Handles both SSH (git@host:owner/repo.git) and HTTPS forms.
    Returns (None, None) on anything unparseable.
    """
    if not url:
        return None, None
    m = _REPO_URL_RE.match(url.strip())
    if not m:
        return None, None
    _host, owner, name = m.groups()
    return owner, name
```

- [ ] **Step 4: Run test, confirm pass**

```bash
pytest tests/test_task_id.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/task_id.py tests/test_task_id.py
git commit -m "feat(task-id): deterministic compute_task_id with env override"
```

---

## Task 4: Config module

**Files:**
- Create: `src/nexus/config.py`
- Create: `tests/test_config.py`
- Create: `tests/fixtures/nexus_config.toml`

Per spec §11, no hardcoded paths. Config lives at `~/.nexus/config.toml`.

- [ ] **Step 1: Create fixture config**

Create `tests/fixtures/nexus_config.toml`:

```toml
[paths]
incoming_dir = "/tmp/nexus-test/incoming"
parquet_dir  = "/tmp/nexus-test/parquet"
duckdb_path  = "/tmp/nexus-test/nexus.duckdb"
processed_retention_days = 7

[server]
otlp_grpc_port = 4317
mcp_http_port  = 7077

[agent]
agent_id     = "test-agent"
principal_id = "test-user"
```

- [ ] **Step 2: Write failing test**

Create `tests/test_config.py`:

```python
"""Tests for src/nexus/config.py."""
from pathlib import Path
import pytest
from nexus.config import NexusConfig, load_config, default_config


FIXTURE = Path(__file__).parent / "fixtures" / "nexus_config.toml"


def test_load_from_path():
    cfg = load_config(FIXTURE)
    assert cfg.incoming_dir == Path("/tmp/nexus-test/incoming")
    assert cfg.parquet_dir == Path("/tmp/nexus-test/parquet")
    assert cfg.duckdb_path == Path("/tmp/nexus-test/nexus.duckdb")
    assert cfg.otlp_grpc_port == 4317
    assert cfg.mcp_http_port == 7077
    assert cfg.agent_id == "test-agent"
    assert cfg.principal_id == "test-user"
    assert cfg.processed_retention_days == 7


def test_default_config_uses_home_dir():
    cfg = default_config()
    assert cfg.incoming_dir.is_absolute()
    assert ".nexus" in str(cfg.incoming_dir)
    assert cfg.otlp_grpc_port == 4317
    assert cfg.mcp_http_port == 7077


def test_missing_optional_field_uses_default(tmp_path):
    cfg_file = tmp_path / "minimal.toml"
    cfg_file.write_text("[paths]\nincoming_dir = '/tmp/x'\n")
    cfg = load_config(cfg_file)
    assert cfg.incoming_dir == Path("/tmp/x")
    # All other fields fall back to defaults
    assert cfg.otlp_grpc_port == 4317
    assert cfg.processed_retention_days == 7


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")
```

- [ ] **Step 3: Run test, confirm failure**

```bash
pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

Create `src/nexus/config.py`:

```python
"""Nexus runtime configuration.

Single source of truth: ~/.nexus/config.toml.  Falls back to sensible
defaults for any missing field so a brand-new install Just Works after
`nexus-server init` writes the default file.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import os

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def _home_nexus() -> Path:
    return Path(os.path.expanduser("~/.nexus"))


@dataclass(frozen=True)
class NexusConfig:
    incoming_dir: Path
    parquet_dir: Path
    duckdb_path: Path
    processed_retention_days: int
    otlp_grpc_port: int
    mcp_http_port: int
    agent_id: str
    principal_id: str


_DEFAULTS = {
    "paths": {
        "incoming_dir": str(_home_nexus() / "incoming"),
        "parquet_dir": str(_home_nexus() / "parquet"),
        "duckdb_path": str(_home_nexus() / "nexus.duckdb"),
        "processed_retention_days": 7,
    },
    "server": {
        "otlp_grpc_port": 4317,
        "mcp_http_port": 7077,
    },
    "agent": {
        "agent_id": "unknown-agent",
        "principal_id": "unknown",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = {k: dict(v) for k, v in base.items()}
    for section, values in override.items():
        out.setdefault(section, {}).update(values)
    return out


def _from_dict(d: dict) -> NexusConfig:
    return NexusConfig(
        incoming_dir=Path(d["paths"]["incoming_dir"]),
        parquet_dir=Path(d["paths"]["parquet_dir"]),
        duckdb_path=Path(d["paths"]["duckdb_path"]),
        processed_retention_days=int(d["paths"]["processed_retention_days"]),
        otlp_grpc_port=int(d["server"]["otlp_grpc_port"]),
        mcp_http_port=int(d["server"]["mcp_http_port"]),
        agent_id=d["agent"]["agent_id"],
        principal_id=d["agent"]["principal_id"],
    )


def default_config() -> NexusConfig:
    return _from_dict(_DEFAULTS)


def load_config(path: Path) -> NexusConfig:
    """Load config from a TOML file, falling back to defaults for missing keys."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("rb") as f:
        loaded = tomllib.load(f)
    merged = _merge(_DEFAULTS, loaded)
    return _from_dict(merged)
```

- [ ] **Step 5: Run test, confirm pass**

```bash
pytest tests/test_config.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/config.py tests/test_config.py tests/fixtures/nexus_config.toml
git commit -m "feat(config): NexusConfig dataclass loaded from ~/.nexus/config.toml"
```

---

## Task 5: DuckDB schema bootstrap

**Files:**
- Create: `src/nexus/schema.py`
- Create: `tests/test_schema.py`

Per spec §4. Bootstrap creates the Parquet directory layout, registers DuckDB views over Parquet, and creates the new SQL tables (`tasks`, `session_summaries`). Idempotent — re-running is a no-op.

- [ ] **Step 1: Write failing test**

Create `tests/test_schema.py`:

```python
"""Tests for src/nexus/schema.py."""
from pathlib import Path
import duckdb
import pytest
from nexus.schema import bootstrap, EXPECTED_TABLES, EXPECTED_VIEWS


@pytest.fixture
def tmp_lakehouse(tmp_path):
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "nexus.duckdb"
    return parquet_dir, db_path


def test_bootstrap_creates_parquet_dirs(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    for sub in ["agent_events", "spans", "pr_events", "routing"]:
        assert (parquet_dir / sub).is_dir(), f"missing {sub} dir"


def test_bootstrap_creates_expected_tables(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    tables = {row[0] for row in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE'"
    ).fetchall()}
    for t in EXPECTED_TABLES:
        assert t in tables, f"missing table {t}"


def test_bootstrap_is_idempotent(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    # Second call must not raise
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)


def test_tasks_table_has_expected_columns(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    cols = {row[0] for row in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
    ).fetchall()}
    expected = {
        "task_id", "repo_owner", "repo_name", "branch", "explicit_task_id",
        "principal_id", "status", "title", "created_at", "last_activity_at",
        "session_count", "total_cost_usd",
    }
    assert expected.issubset(cols), f"tasks missing: {expected - cols}"


def test_session_summaries_table_has_expected_columns(tmp_lakehouse):
    parquet_dir, db_path = tmp_lakehouse
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    cols = {row[0] for row in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'session_summaries'"
    ).fetchall()}
    expected = {
        "session_id", "task_id", "agent_id", "ended_at", "summary_md",
        "files_touched", "tools_used", "last_user_prompt", "last_assistant",
        "next_steps_md", "open_questions", "status", "generator_model", "generated_at",
    }
    assert expected.issubset(cols), f"session_summaries missing: {expected - cols}"
```

- [ ] **Step 2: Run test, confirm failure**

```bash
pytest tests/test_schema.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.schema'`.

- [ ] **Step 3: Implement**

Create `src/nexus/schema.py`:

```python
"""DuckDB schema bootstrap for the Nexus lakehouse.

Idempotent: every CREATE uses IF NOT EXISTS or OR REPLACE.

Layout:
  parquet_dir/
    agent_events/date=YYYY-MM-DD/agent_id=<id>/part-*.parquet
    spans/date=YYYY-MM-DD/part-*.parquet
    pr_events/part-*.parquet
    routing/part-*.parquet
  nexus.duckdb
    Tables: tasks, session_summaries, summarize_jobs
    Views:  agent_events, spans, pr_events, routing, sessions, active_sessions
"""
from __future__ import annotations
from pathlib import Path
import duckdb


PARQUET_SUBDIRS = ("agent_events", "spans", "pr_events", "routing")

EXPECTED_TABLES = ("tasks", "session_summaries", "summarize_jobs")
EXPECTED_VIEWS = ("agent_events", "spans", "pr_events", "routing", "sessions", "active_sessions")


_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id           VARCHAR PRIMARY KEY,
  repo_owner        VARCHAR,
  repo_name         VARCHAR,
  branch            VARCHAR,
  explicit_task_id  VARCHAR,
  principal_id      VARCHAR,
  status            VARCHAR,
  title             VARCHAR,
  created_at        TIMESTAMP,
  last_activity_at  TIMESTAMP,
  session_count     INTEGER,
  total_cost_usd    DOUBLE
);
"""

_SESSION_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS session_summaries (
  session_id        VARCHAR PRIMARY KEY,
  task_id           VARCHAR,
  agent_id          VARCHAR,
  ended_at          TIMESTAMP,
  summary_md        VARCHAR,
  files_touched     VARCHAR[],
  tools_used        MAP(VARCHAR, INTEGER),
  last_user_prompt  VARCHAR,
  last_assistant    VARCHAR,
  next_steps_md     VARCHAR,
  open_questions    VARCHAR[],
  status            VARCHAR,
  generator_model   VARCHAR,
  generated_at      TIMESTAMP
);
"""

_SUMMARIZE_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS summarize_jobs (
  session_id  VARCHAR PRIMARY KEY,
  status      VARCHAR,           -- 'pending' | 'running' | 'done' | 'errored'
  attempts    INTEGER DEFAULT 0,
  last_error  VARCHAR,
  enqueued_at TIMESTAMP DEFAULT now(),
  updated_at  TIMESTAMP
);
"""


def _agent_events_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW agent_events AS
SELECT * FROM read_parquet(
  '{parquet_dir}/agent_events/**/*.parquet',
  hive_partitioning=true,
  union_by_name=true
);
"""


def _spans_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW spans AS
SELECT * FROM read_parquet(
  '{parquet_dir}/spans/**/*.parquet',
  hive_partitioning=true,
  union_by_name=true
);
"""


def _pr_events_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW pr_events AS
SELECT * FROM read_parquet(
  '{parquet_dir}/pr_events/**/*.parquet',
  union_by_name=true
);
"""


def _routing_view(parquet_dir: Path) -> str:
    return f"""
CREATE OR REPLACE VIEW routing AS
SELECT * FROM read_parquet(
  '{parquet_dir}/routing/**/*.parquet',
  union_by_name=true
);
"""


_SESSIONS_VIEW = """
CREATE OR REPLACE VIEW sessions AS
SELECT
  e.session_id,
  any_value(e.agent_id) AS agent_id,
  any_value(e.task_id)  AS task_id,
  min(e.timestamp)      AS started_at,
  max(e.timestamp)      AS ended_at,
  count(*)              AS event_count,
  ss.summary_md,
  ss.next_steps_md
FROM agent_events e
LEFT JOIN session_summaries ss USING (session_id)
GROUP BY e.session_id, ss.summary_md, ss.next_steps_md;
"""


_ACTIVE_SESSIONS_VIEW = """
CREATE OR REPLACE VIEW active_sessions AS
SELECT
  e.session_id,
  any_value(e.agent_id) AS agent_id,
  any_value(e.task_id)  AS task_id,
  any_value(t.repo_owner) AS repo_owner,
  any_value(t.repo_name)  AS repo_name,
  any_value(t.branch)     AS branch,
  min(e.timestamp)        AS started_at,
  max(e.timestamp)        AS last_event_at,
  count(*)                AS event_count
FROM agent_events e
LEFT JOIN tasks t USING (task_id)
WHERE NOT EXISTS (SELECT 1 FROM session_summaries ss WHERE ss.session_id = e.session_id)
  AND e.timestamp > now() - INTERVAL 30 MINUTE
GROUP BY e.session_id;
"""


def _ensure_seed_parquet(parquet_dir: Path) -> None:
    """DuckDB's read_parquet errors on an empty glob.  Drop a tiny empty
    parquet file in each subdir so views can be created at bootstrap time."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    empty_schema = pa.schema([("__placeholder", pa.string())])
    empty = pa.table({"__placeholder": []}, schema=empty_schema)
    for sub in PARQUET_SUBDIRS:
        seed_dir = parquet_dir / sub / "_seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        seed_file = seed_dir / "empty.parquet"
        if not seed_file.exists():
            pq.write_table(empty, seed_file)


def bootstrap(*, parquet_dir: Path, duckdb_path: Path) -> None:
    """Create directories, tables, and views.  Idempotent."""
    parquet_dir = Path(parquet_dir)
    duckdb_path = Path(duckdb_path)
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    for sub in PARQUET_SUBDIRS:
        (parquet_dir / sub).mkdir(parents=True, exist_ok=True)

    _ensure_seed_parquet(parquet_dir)

    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(_TASKS_DDL)
        con.execute(_SESSION_SUMMARIES_DDL)
        con.execute(_SUMMARIZE_JOBS_DDL)
        con.execute(_agent_events_view(parquet_dir))
        con.execute(_spans_view(parquet_dir))
        con.execute(_pr_events_view(parquet_dir))
        con.execute(_routing_view(parquet_dir))
        con.execute(_SESSIONS_VIEW)
        con.execute(_ACTIVE_SESSIONS_VIEW)
    finally:
        con.close()
```

- [ ] **Step 4: Run test, confirm pass**

```bash
pytest tests/test_schema.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/schema.py tests/test_schema.py
git commit -m "feat(schema): idempotent DuckDB bootstrap with tasks + session_summaries"
```

---

## Task 6: Ingest function — JSONL → AgentEvent → Parquet MERGE

**Files:**
- Create: `src/nexus/server/__init__.py`
- Create: `src/nexus/server/ingest.py`
- Create: `tests/test_ingest.py`
- Create: `tests/fixtures/incoming/sample_agent_events.jsonl`

This is the load-bearing ingest primitive. Reads a JSONL file where each line is a serialized `AgentEvent`, augments with `task_id` + `dedup_key`, and MERGEs into `agent_events` Parquet partitioned by `(date, agent_id)`. Idempotent via dedup_key.

- [ ] **Step 1: Create empty package marker**

Create `src/nexus/server/__init__.py`:

```python
"""Nexus server: file watcher, OTLP receiver, MCP server, summarizer."""
```

- [ ] **Step 2: Create fixture JSONL**

Create `tests/fixtures/incoming/sample_agent_events.jsonl` with three valid AgentEvents:

```json
{"id":"evt-001","session_id":"sess-A","timestamp":"2026-05-08T10:00:00Z","agent_id":"macmini-claude","event_type":"user_message","message":{"role":"user","content":"hello"},"raw_data":{"cwd":"/Users/arnab/jenny/nexus","gitBranch":"main","_repo_owner":"arniesaha","_repo_name":"nexus"}}
{"id":"evt-002","session_id":"sess-A","timestamp":"2026-05-08T10:00:05Z","agent_id":"macmini-claude","event_type":"assistant_message","message":{"role":"assistant","content":"hi there"},"raw_data":{"cwd":"/Users/arnab/jenny/nexus","gitBranch":"main","_repo_owner":"arniesaha","_repo_name":"nexus"}}
{"id":"evt-003","session_id":"sess-B","timestamp":"2026-05-08T10:01:00Z","agent_id":"nas-openclaw","event_type":"user_message","message":{"role":"user","content":"x"},"raw_data":{"cwd":"/home/Arnab/nexus","gitBranch":"main","_repo_owner":"arniesaha","_repo_name":"nexus"}}
```

(One line per event; do not pretty-print. The `_repo_owner`/`_repo_name`/`gitBranch` fields in `raw_data` are how `nexus-collect` will pre-resolve repo info — the ingest reads them as already-normalized.)

- [ ] **Step 3: Write failing test**

Create `tests/test_ingest.py`:

```python
"""Tests for src/nexus/server/ingest.py."""
from pathlib import Path
import duckdb
import pytest
from nexus.schema import bootstrap
from nexus.server.ingest import ingest_file, IngestStats

FIXTURE = Path(__file__).parent / "fixtures" / "incoming" / "sample_agent_events.jsonl"


@pytest.fixture
def tmp_lh(tmp_path):
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    return parquet_dir, db_path


def test_ingest_writes_three_rows(tmp_lh):
    parquet_dir, db_path = tmp_lh
    stats = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats.read == 3
    assert stats.inserted == 3
    assert stats.skipped_dupes == 0

    con = duckdb.connect(str(db_path))
    n = con.execute("SELECT count(*) FROM agent_events WHERE id LIKE 'evt-%'").fetchone()[0]
    assert n == 3


def test_ingest_partitions_by_date_and_agent(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    # macmini-claude should have 2 rows on 2026-05-08, nas-openclaw 1
    macmini = parquet_dir / "agent_events" / "date=2026-05-08" / "agent_id=macmini-claude"
    nas = parquet_dir / "agent_events" / "date=2026-05-08" / "agent_id=nas-openclaw"
    assert macmini.is_dir()
    assert nas.is_dir()
    assert any(p.suffix == ".parquet" for p in macmini.iterdir())
    assert any(p.suffix == ".parquet" for p in nas.iterdir())


def test_ingest_is_idempotent(tmp_lh):
    parquet_dir, db_path = tmp_lh
    s1 = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    s2 = ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert s1.inserted == 3
    assert s2.inserted == 0
    assert s2.skipped_dupes == 3

    con = duckdb.connect(str(db_path))
    n = con.execute("SELECT count(*) FROM agent_events WHERE id LIKE 'evt-%'").fetchone()[0]
    assert n == 3


def test_ingest_computes_task_id(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    rows = con.execute(
        "SELECT DISTINCT task_id FROM agent_events WHERE id LIKE 'evt-%'"
    ).fetchall()
    # All three events are arniesaha/nexus@main → same task_id
    assert len(rows) == 1
    task_id = rows[0][0]
    assert isinstance(task_id, str)
    assert len(task_id) == 16


def test_ingest_upserts_tasks_row(tmp_lh):
    parquet_dir, db_path = tmp_lh
    ingest_file(FIXTURE, parquet_dir=parquet_dir, duckdb_path=db_path)
    con = duckdb.connect(str(db_path))
    rows = con.execute(
        "SELECT repo_owner, repo_name, branch FROM tasks"
    ).fetchall()
    assert ("arniesaha", "nexus", "main") in rows


def test_ingest_skips_malformed_lines(tmp_lh, tmp_path):
    parquet_dir, db_path = tmp_lh
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id":"good","session_id":"s","timestamp":"2026-05-08T10:00:00Z",'
        '"agent_id":"a","event_type":"user_message","raw_data":{}}\n'
        'NOT JSON\n'
        '{"missing":"required-fields"}\n'
    )
    stats = ingest_file(bad, parquet_dir=parquet_dir, duckdb_path=db_path)
    assert stats.read == 3
    assert stats.inserted == 1
    assert stats.errors == 2
```

- [ ] **Step 4: Run test, confirm failure**

```bash
pytest tests/test_ingest.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.server.ingest'`.

- [ ] **Step 5: Implement**

Create `src/nexus/server/ingest.py`:

```python
"""Ingest a JSONL file of canonical AgentEvents into the lakehouse.

For each event:
  1. Parse + validate via AgentEvent.
  2. Compute dedup_key from (timestamp, agent_id, session_id, event_type, content[:200]).
  3. Compute task_id from raw_data._repo_owner / _repo_name / gitBranch (or env).
  4. Append to a date=YYYY-MM-DD/agent_id=<id> Parquet file with a unique part name.
  5. Drop rows whose dedup_key already appears in the existing partition.
  6. Upsert tasks rows.

Idempotent: re-ingesting the same file produces zero new rows.
"""
from __future__ import annotations
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from nexus.dedup import make_dedup_key
from nexus.task_id import compute_task_id
from nexus.models import AgentEvent

log = logging.getLogger("nexus.ingest")


@dataclass
class IngestStats:
    read: int = 0
    inserted: int = 0
    skipped_dupes: int = 0
    errors: int = 0


def _extract_content(ev: AgentEvent) -> str:
    if ev.message and isinstance(ev.message.content, str):
        return ev.message.content
    if ev.message and isinstance(ev.message.content, list):
        # Concatenate text blocks for fingerprinting
        parts = []
        for block in ev.message.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _row_from_event(ev: AgentEvent, env_task_id: Optional[str]) -> dict:
    rd = ev.raw_data or {}
    repo_owner = rd.get("_repo_owner")
    repo_name = rd.get("_repo_name")
    branch = rd.get("gitBranch") or rd.get("git_branch")
    content = _extract_content(ev)

    return {
        "id": ev.id,
        "session_id": ev.session_id,
        "timestamp": ev.timestamp,
        "date": ev.timestamp.strftime("%Y-%m-%d"),
        "agent_id": ev.agent_id,
        "event_type": ev.event_type,
        "role": ev.message.role if ev.message else None,
        "content": content,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "branch": branch,
        "task_id": compute_task_id(env_task_id, repo_owner, repo_name, branch),
        "principal_id": rd.get("_principal_id"),
        "dedup_key": make_dedup_key(
            ev.timestamp.isoformat(),
            ev.agent_id,
            ev.session_id,
            ev.event_type,
            content,
        ),
        "raw_data": json.dumps(rd, default=str),
    }


def _iter_events(path: Path, env_task_id: Optional[str]) -> Iterator[tuple[Optional[dict], Optional[str]]]:
    """Yield (row_dict, error_msg) — exactly one of the two is None per yield."""
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                ev = AgentEvent.model_validate(obj)
                yield _row_from_event(ev, env_task_id), None
            except Exception as e:
                yield None, f"line {lineno}: {e!r}"


def _existing_dedup_keys(con, parquet_dir: Path) -> set:
    """Read existing dedup_keys from the agent_events view.  Empty set on cold start."""
    try:
        rows = con.execute("SELECT dedup_key FROM agent_events WHERE dedup_key IS NOT NULL").fetchall()
        return {r[0] for r in rows}
    except duckdb.Error:
        return set()


def _write_partition(rows: list[dict], parquet_dir: Path) -> None:
    """Group rows by (date, agent_id) and write one parquet file per partition."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        grouped.setdefault((r["date"], r["agent_id"]), []).append(r)

    for (date, agent_id), part_rows in grouped.items():
        out_dir = parquet_dir / "agent_events" / f"date={date}" / f"agent_id={agent_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"part-{uuid.uuid4().hex[:12]}.parquet"
        # Drop the partition columns from the row payload — Hive partitioning encodes them in path
        payload = [
            {k: v for k, v in r.items() if k not in ("date", "agent_id")}
            for r in part_rows
        ]
        table = pa.Table.from_pylist(payload)
        pq.write_table(table, out_path, compression="zstd")


def _upsert_tasks(con, rows: list[dict]) -> None:
    """Insert any new (task_id) into tasks; update last_activity_at + session_count for existing."""
    seen: dict[str, dict] = {}
    for r in rows:
        tid = r["task_id"]
        if tid not in seen:
            seen[tid] = {
                "task_id": tid,
                "repo_owner": r.get("repo_owner"),
                "repo_name": r.get("repo_name"),
                "branch": r.get("branch"),
                "principal_id": r.get("principal_id"),
                "last_activity_at": r["timestamp"],
            }
        else:
            if r["timestamp"] > seen[tid]["last_activity_at"]:
                seen[tid]["last_activity_at"] = r["timestamp"]

    for tid, t in seen.items():
        con.execute(
            """
            INSERT INTO tasks (task_id, repo_owner, repo_name, branch, principal_id,
                               status, created_at, last_activity_at, session_count, total_cost_usd)
            VALUES (?, ?, ?, ?, ?, 'open', now(), ?, 0, 0.0)
            ON CONFLICT (task_id) DO UPDATE SET
              last_activity_at = greatest(tasks.last_activity_at, EXCLUDED.last_activity_at)
            """,
            [t["task_id"], t["repo_owner"], t["repo_name"], t["branch"],
             t["principal_id"], t["last_activity_at"]],
        )


def ingest_file(
    path: Path,
    *,
    parquet_dir: Path,
    duckdb_path: Path,
    env_task_id: Optional[str] = None,
) -> IngestStats:
    """Ingest one JSONL file.  Returns IngestStats."""
    path = Path(path)
    parquet_dir = Path(parquet_dir)
    duckdb_path = Path(duckdb_path)

    stats = IngestStats()
    new_rows: list[dict] = []

    con = duckdb.connect(str(duckdb_path))
    try:
        existing = _existing_dedup_keys(con, parquet_dir)

        for row, err in _iter_events(path, env_task_id):
            stats.read += 1
            if err:
                stats.errors += 1
                log.warning("ingest %s: %s", path, err)
                continue
            assert row is not None
            if row["dedup_key"] in existing:
                stats.skipped_dupes += 1
                continue
            new_rows.append(row)
            existing.add(row["dedup_key"])

        if new_rows:
            _write_partition(new_rows, parquet_dir)
            _upsert_tasks(con, new_rows)
            stats.inserted = len(new_rows)
    finally:
        con.close()

    return stats
```

- [ ] **Step 6: Run test, confirm pass**

```bash
pytest tests/test_ingest.py -v
```

Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/server/__init__.py src/nexus/server/ingest.py tests/test_ingest.py tests/fixtures/incoming/sample_agent_events.jsonl
git commit -m "feat(server): ingest_file MERGEs JSONL into Parquet via dedup_key"
```

---

## Task 7: File watcher — pick up dropped files and dispatch to ingest

**Files:**
- Create: `src/nexus/server/watcher.py`
- Create: `tests/test_watcher.py`

Server-side watchdog observer. Watches `~/nexus/incoming/<host>/` (recursively) for `.jsonl` files. The atomic rename pattern (collector writes `.jsonl.tmp`, then renames to `.jsonl`) means we only act on files whose final name is `.jsonl`.

After successful ingest, file moves to `incoming/<host>/.processed/<filename>`.

- [ ] **Step 1: Write failing test**

Create `tests/test_watcher.py`:

```python
"""Tests for src/nexus/server/watcher.py."""
import json
import shutil
import time
from pathlib import Path
import duckdb
import pytest
from nexus.schema import bootstrap
from nexus.server.watcher import IncomingWatcher


@pytest.fixture
def lh(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "nexus.duckdb"
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)
    return incoming, parquet_dir, db_path


def _write_event(jsonl_path: Path, event_id: str) -> None:
    line = json.dumps({
        "id": event_id,
        "session_id": "sess-x",
        "timestamp": "2026-05-08T10:00:00Z",
        "agent_id": "test-agent",
        "event_type": "user_message",
        "message": {"role": "user", "content": "hi"},
        "raw_data": {"_repo_owner": "arniesaha", "_repo_name": "nexus", "gitBranch": "main"},
    })
    jsonl_path.write_text(line + "\n")


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_watcher_picks_up_dropped_file(lh):
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path)
    w.start()
    try:
        target = host_dir / "batch-001.jsonl"
        # Atomic-rename pattern: write to .tmp, then rename
        tmp = target.with_suffix(".jsonl.tmp")
        _write_event(tmp, "watcher-001")
        tmp.rename(target)

        def has_row():
            con = duckdb.connect(str(db_path))
            try:
                return con.execute(
                    "SELECT count(*) FROM agent_events WHERE id = 'watcher-001'"
                ).fetchone()[0] == 1
            finally:
                con.close()

        assert _wait_for(has_row), "row never landed in agent_events"
    finally:
        w.stop()


def test_watcher_moves_file_to_processed(lh):
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path)
    w.start()
    try:
        target = host_dir / "batch-002.jsonl"
        tmp = target.with_suffix(".jsonl.tmp")
        _write_event(tmp, "watcher-002")
        tmp.rename(target)

        def is_moved():
            return (host_dir / ".processed" / "batch-002.jsonl").exists()

        assert _wait_for(is_moved), "file never moved to .processed/"
        assert not target.exists(), "original file should have been removed"
    finally:
        w.stop()


def test_watcher_ignores_tmp_files(lh):
    incoming, parquet_dir, db_path = lh
    host_dir = incoming / "macmini"
    host_dir.mkdir()

    w = IncomingWatcher(incoming_dir=incoming, parquet_dir=parquet_dir, duckdb_path=db_path)
    w.start()
    try:
        tmp = host_dir / "batch-003.jsonl.tmp"
        _write_event(tmp, "watcher-003")
        time.sleep(0.5)  # give the watcher a chance to (incorrectly) act

        con = duckdb.connect(str(db_path))
        try:
            n = con.execute(
                "SELECT count(*) FROM agent_events WHERE id = 'watcher-003'"
            ).fetchone()[0]
        finally:
            con.close()
        assert n == 0, "watcher should not process .tmp files"
        assert tmp.exists(), ".tmp file should still be in place"
    finally:
        w.stop()
```

- [ ] **Step 2: Run test, confirm failure**

```bash
pytest tests/test_watcher.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.server.watcher'`.

- [ ] **Step 3: Implement**

Create `src/nexus/server/watcher.py`:

```python
"""File-system watcher that ingests dropped AgentEvent JSONL files.

Layout watched:
  <incoming_dir>/<host>/<batch>.jsonl       <- act on this
  <incoming_dir>/<host>/<batch>.jsonl.tmp   <- ignore (in-flight)

After ingest, file is moved to:
  <incoming_dir>/<host>/.processed/<batch>.jsonl
"""
from __future__ import annotations
import logging
import shutil
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from nexus.server.ingest import ingest_file

log = logging.getLogger("nexus.watcher")


class _Handler(FileSystemEventHandler):
    def __init__(self, parquet_dir: Path, duckdb_path: Path):
        self._parquet_dir = parquet_dir
        self._duckdb_path = duckdb_path
        self._lock = threading.Lock()  # serialize ingests; DuckDB single-writer

    def _maybe_ingest(self, path: Path) -> None:
        if not path.is_file():
            return
        if path.suffix != ".jsonl":  # ignore .tmp and anything else
            return
        if ".processed" in path.parts:  # ignore our own moves
            return

        with self._lock:
            try:
                stats = ingest_file(
                    path,
                    parquet_dir=self._parquet_dir,
                    duckdb_path=self._duckdb_path,
                )
                log.info(
                    "ingested %s read=%d inserted=%d dupes=%d errors=%d",
                    path, stats.read, stats.inserted, stats.skipped_dupes, stats.errors,
                )
                # Move to .processed/ for audit
                processed = path.parent / ".processed"
                processed.mkdir(exist_ok=True)
                target = processed / path.name
                shutil.move(str(path), str(target))
            except Exception:
                log.exception("ingest failed for %s; leaving file in place", path)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._maybe_ingest(Path(event.src_path))

    def on_moved(self, event) -> None:
        # Triggered by collector's atomic rename .jsonl.tmp → .jsonl
        if event.is_directory:
            return
        self._maybe_ingest(Path(event.dest_path))


class IncomingWatcher:
    """Run a watchdog observer over <incoming_dir> and ingest JSONL files."""

    def __init__(self, *, incoming_dir: Path, parquet_dir: Path, duckdb_path: Path):
        self._incoming = Path(incoming_dir)
        self._parquet_dir = Path(parquet_dir)
        self._duckdb_path = Path(duckdb_path)
        self._observer: Observer | None = None
        self._handler = _Handler(self._parquet_dir, self._duckdb_path)

    def start(self) -> None:
        self._incoming.mkdir(parents=True, exist_ok=True)
        # Pick up files already present at start time
        for jsonl in self._incoming.rglob("*.jsonl"):
            self._handler._maybe_ingest(jsonl)
        observer = Observer()
        observer.schedule(self._handler, str(self._incoming), recursive=True)
        observer.start()
        self._observer = observer
        log.info("watcher started on %s", self._incoming)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            log.info("watcher stopped")
```

- [ ] **Step 4: Run test, confirm pass**

```bash
pytest tests/test_watcher.py -v
```

Expected: 3 passed. (Tests poll up to 5s for inotify-equivalent events to fire on macOS via FSEvents.)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/watcher.py tests/test_watcher.py
git commit -m "feat(server): IncomingWatcher dispatches dropped JSONL to ingest"
```

---

## Task 8: `nexus-server` CLI entry point

**Files:**
- Create: `src/nexus/server/__main__.py`
- Create: `tests/test_server_cli.py`

CLI surface: `nexus-server init` (writes default config), `nexus-server run` (starts watcher), `nexus-server status` (prints config + table counts).

- [ ] **Step 1: Write failing test**

Create `tests/test_server_cli.py`:

```python
"""Tests for src/nexus/server/__main__.py CLI."""
from pathlib import Path
import textwrap
from click.testing import CliRunner
from nexus.server.__main__ import main


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(f"""\
        [paths]
        incoming_dir = "{tmp_path / 'incoming'}"
        parquet_dir  = "{tmp_path / 'parquet'}"
        duckdb_path  = "{tmp_path / 'nexus.duckdb'}"
        processed_retention_days = 7

        [server]
        otlp_grpc_port = 14317
        mcp_http_port  = 17077

        [agent]
        agent_id     = "test"
        principal_id = "test"
    """))
    return cfg


def test_cli_init_writes_default_config(tmp_path):
    runner = CliRunner()
    target = tmp_path / "myconf.toml"
    res = runner.invoke(main, ["--config", str(target), "init"])
    assert res.exit_code == 0, res.output
    assert target.exists()
    assert "[paths]" in target.read_text()


def test_cli_init_does_not_overwrite_existing(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "init"])
    assert res.exit_code != 0
    assert "already exists" in res.output.lower()


def test_cli_status_shows_config_and_counts(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    res = runner.invoke(main, ["--config", str(cfg), "status"])
    assert res.exit_code == 0, res.output
    assert "incoming_dir" in res.output
    assert "tasks" in res.output
    assert "session_summaries" in res.output


def test_cli_help_lists_subcommands():
    runner = CliRunner()
    res = runner.invoke(main, ["--help"])
    assert res.exit_code == 0
    for sub in ("init", "run", "status"):
        assert sub in res.output
```

- [ ] **Step 2: Run test, confirm failure**

```bash
pytest tests/test_server_cli.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.server.__main__'`.

- [ ] **Step 3: Implement**

Create `src/nexus/server/__main__.py`:

```python
"""nexus-server CLI."""
from __future__ import annotations
import logging
import os
import signal
import sys
import textwrap
import threading
from pathlib import Path
from typing import Optional

import click
import duckdb

from nexus.config import NexusConfig, default_config, load_config
from nexus.schema import bootstrap, EXPECTED_TABLES
from nexus.server.watcher import IncomingWatcher

log = logging.getLogger("nexus.server")


_DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.nexus/config.toml"))

_DEFAULT_CONFIG_TEMPLATE = """\
# Nexus runtime config — see docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md

[paths]
incoming_dir = "{home}/.nexus/incoming"
parquet_dir  = "{home}/.nexus/parquet"
duckdb_path  = "{home}/.nexus/nexus.duckdb"
processed_retention_days = 7

[server]
otlp_grpc_port = 4317
mcp_http_port  = 7077

[agent]
agent_id     = "{default_agent_id}"
principal_id = "arnab"
"""


def _resolve_config(path: Optional[str]) -> NexusConfig:
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    if p.exists():
        return load_config(p)
    return default_config()


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config TOML")
@click.option("-v", "--verbose", is_flag=True, help="Enable DEBUG logging")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str], verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Write a default config file at --config (defaults to ~/.nexus/config.toml)."""
    p = Path(ctx.obj["config_path"]) if ctx.obj["config_path"] else _DEFAULT_CONFIG_PATH
    if p.exists():
        click.echo(f"config already exists: {p}", err=True)
        sys.exit(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    home = os.path.expanduser("~")
    default_agent_id = os.uname().nodename.split(".")[0] + "-claude"
    p.write_text(_DEFAULT_CONFIG_TEMPLATE.format(home=home, default_agent_id=default_agent_id))
    click.echo(f"wrote {p}")


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print effective config and table row counts."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)

    click.echo(textwrap.dedent(f"""\
        nexus-server status
        ===================
        incoming_dir : {cfg.incoming_dir}
        parquet_dir  : {cfg.parquet_dir}
        duckdb_path  : {cfg.duckdb_path}
        otlp_grpc_port : {cfg.otlp_grpc_port}
        mcp_http_port  : {cfg.mcp_http_port}
        agent_id     : {cfg.agent_id}
        principal_id : {cfg.principal_id}
    """))

    con = duckdb.connect(str(cfg.duckdb_path))
    try:
        for t in EXPECTED_TABLES:
            try:
                n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except duckdb.Error as e:
                n = f"error: {e}"
            click.echo(f"  {t:20s} {n}")
        for v in ("agent_events", "spans", "pr_events", "routing"):
            try:
                n = con.execute(
                    f"SELECT count(*) FROM {v} WHERE __placeholder IS NULL OR true"
                ).fetchone()[0]
            except duckdb.Error as e:
                n = f"error: {e}"
            click.echo(f"  {v:20s} {n}")
    finally:
        con.close()


@main.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run the file watcher (foreground).  Ctrl-C to stop."""
    cfg = _resolve_config(ctx.obj["config_path"])
    bootstrap(parquet_dir=cfg.parquet_dir, duckdb_path=cfg.duckdb_path)

    watcher = IncomingWatcher(
        incoming_dir=cfg.incoming_dir,
        parquet_dir=cfg.parquet_dir,
        duckdb_path=cfg.duckdb_path,
    )
    watcher.start()

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log.info("received signal %d; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        stop.wait()
    finally:
        watcher.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run test, confirm pass**

```bash
pytest tests/test_server_cli.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Reinstall to register new console script**

```bash
uv pip install -e . || pip install -e .
```

- [ ] **Step 6: Smoke test the CLI**

```bash
TMPDIR=$(mktemp -d)
nexus-server --config $TMPDIR/c.toml init
nexus-server --config $TMPDIR/c.toml status
```

Expected: `init` writes config, `status` prints config block and zero counts.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/server/__main__.py tests/test_server_cli.py
git commit -m "feat(server): nexus-server CLI with init/run/status"
```

---

## Task 9: End-to-end smoke test (no watcher mock)

**Files:**
- Create: `tests/test_server_e2e.py`

This is the single test that proves the slice works: spawn the server `run` in a thread, drop a JSONL via the atomic-rename pattern, query DuckDB, assert row.

- [ ] **Step 1: Write the test**

Create `tests/test_server_e2e.py`:

```python
"""End-to-end smoke test: server run + dropped file → DuckDB row."""
import json
import textwrap
import threading
import time
from pathlib import Path
import duckdb
import pytest
from click.testing import CliRunner
from nexus.config import load_config
from nexus.schema import bootstrap
from nexus.server.watcher import IncomingWatcher


def test_e2e_drop_file_appears_in_duckdb(tmp_path):
    incoming = tmp_path / "incoming"
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "nexus.duckdb"
    incoming.mkdir()
    bootstrap(parquet_dir=parquet_dir, duckdb_path=db_path)

    watcher = IncomingWatcher(
        incoming_dir=incoming,
        parquet_dir=parquet_dir,
        duckdb_path=db_path,
    )
    watcher.start()
    try:
        host_dir = incoming / "macmini"
        host_dir.mkdir()
        target = host_dir / "e2e-batch.jsonl"
        tmp = target.with_suffix(".jsonl.tmp")
        line = json.dumps({
            "id": "e2e-001",
            "session_id": "e2e-sess",
            "timestamp": "2026-05-08T11:00:00Z",
            "agent_id": "macmini-claude",
            "event_type": "user_message",
            "message": {"role": "user", "content": "smoke test"},
            "raw_data": {
                "_repo_owner": "arniesaha",
                "_repo_name": "nexus",
                "gitBranch": "docs/local-lakehouse-migration",
                "_principal_id": "arnab",
            },
        })
        tmp.write_text(line + "\n")
        tmp.rename(target)

        deadline = time.monotonic() + 5
        n = 0
        while time.monotonic() < deadline:
            con = duckdb.connect(str(db_path))
            try:
                n = con.execute(
                    "SELECT count(*) FROM agent_events WHERE id = 'e2e-001'"
                ).fetchone()[0]
            finally:
                con.close()
            if n:
                break
            time.sleep(0.1)

        assert n == 1, "event never landed"

        con = duckdb.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT task_id, repo_owner, repo_name, branch, principal_id "
                "FROM agent_events WHERE id = 'e2e-001'"
            ).fetchone()
        finally:
            con.close()
        task_id, owner, name, branch, principal = row
        assert owner == "arniesaha"
        assert name == "nexus"
        assert branch == "docs/local-lakehouse-migration"
        assert principal == "arnab"
        assert len(task_id) == 16

        # tasks row was upserted
        con = duckdb.connect(str(db_path))
        try:
            t = con.execute(
                "SELECT repo_owner, repo_name, branch FROM tasks WHERE task_id = ?",
                [task_id],
            ).fetchone()
        finally:
            con.close()
        assert t == ("arniesaha", "nexus", "docs/local-lakehouse-migration")

        # File was moved to .processed/
        assert (host_dir / ".processed" / "e2e-batch.jsonl").exists()
        assert not target.exists()
    finally:
        watcher.stop()
```

- [ ] **Step 2: Run test**

```bash
pytest tests/test_server_e2e.py -v
```

Expected: 1 passed.

- [ ] **Step 3: Run the full suite**

```bash
pytest -v
```

Expected: all new tests + existing tests still pass. (Existing `tests/test_parsers.py`, `test_cli.py`, etc. should be untouched by this plan.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_server_e2e.py
git commit -m "test: e2e smoke — drop file, ingest, assert DuckDB row"
```

---

## Task 10: launchd plist for Mac Mini

**Files:**
- Create: `scripts/launchd/com.nexus.server.plist`

The Mac Mini hosts the server; `launchctl load` runs it under the user's session and restarts it on crash.

- [ ] **Step 1: Create the plist**

Create `scripts/launchd/com.nexus.server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nexus.server</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/arnabmac/jenny/nexus/.venv/bin/nexus-server</string>
        <string>run</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/arnabmac/jenny/nexus/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/arnabmac/Library/Logs/nexus-server.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/arnabmac/Library/Logs/nexus-server.err.log</string>

    <key>WorkingDirectory</key>
    <string>/Users/arnabmac</string>
</dict>
</plist>
```

- [ ] **Step 2: Document install/uninstall in the plist directory**

Create `scripts/launchd/README-nexus-server.md`:

```markdown
# nexus-server launchd unit (Mac Mini)

## Prereqs
- `nexus-server init` has been run; `~/.nexus/config.toml` exists.
- `nexus-server` is on PATH inside the venv referenced in the plist.

## Install
    cp scripts/launchd/com.nexus.server.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.nexus.server.plist

## Verify
    launchctl list | grep com.nexus.server
    tail -f ~/Library/Logs/nexus-server.out.log

## Uninstall
    launchctl unload ~/Library/LaunchAgents/com.nexus.server.plist
    rm ~/Library/LaunchAgents/com.nexus.server.plist
```

- [ ] **Step 3: Commit**

```bash
git add scripts/launchd/com.nexus.server.plist scripts/launchd/README-nexus-server.md
git commit -m "ops(launchd): nexus-server plist + install README"
```

---

## Task 11: Wire the new spec/plan into README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Status & Direction" callout near the top of the README**

Open `README.md`. After line 3 (the one-paragraph project summary), insert:

```markdown
## Status: GCP Exited, Local Lakehouse Underway

As of 2026-05-08, the GCP-hosted backend (Cloud SQL, BigQuery, Cloud Functions) has
been retired. The active design is a local DuckDB + Parquet lakehouse on the Mac
Mini, fed by AgentWeave OTLP and per-host shippers.

- **Current architecture spec**: [`docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md`](docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md)
- **Implementation plans**: [`docs/superpowers/plans/`](docs/superpowers/plans/)
- **Migration record**: [`docs/local-lakehouse-spec.md`](docs/local-lakehouse-spec.md)

The "Implementation Status" section below documents the GCP-era system and is kept
for historical context — see the spec above for the steady-state design.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): point to the redesign spec + implementation plans"
```

---

## Self-review

**Spec coverage check:**
- §3.1 nexus-server skeleton ✓ (Tasks 8, 10)
- §4.1 tasks table ✓ (Task 5)
- §4.2 session_summaries table ✓ (Task 5)
- §4.3 active_sessions view ✓ (Task 5)
- §4.4 schema additions to existing tables ✓ (Task 5 — done as views over Parquet that include task_id from the rows we write in Task 6)
- §5.2 file shipper path / server-side ingest ✓ (Tasks 6, 7)
- §5.3 idempotency via dedup_key ✓ (Task 2 + Task 6)
- §11 OSS hygiene (LICENSE, ~/.nexus config, no hardcoded paths) ✓ (Tasks 1, 4)
- Spec sections deferred to later plans:
  - §5.1 OTLP receiver — Plan 3
  - §6 handoff flow + MCP tools — Plan 4
  - §7 awareness flow (active_sessions view exists in Task 5 but no MCP tool) — Plan 4
  - §8.5 run migrate_to_duckdb.py + reconcile — Plan 7 (needs Plans 1-6 first)
  - §8.7-8 nexus-hook — Plan 6
  - §8.9 GCP decommission — Plan 7

**Placeholder scan:** No TODO/TBD strings in the plan. Every code block is complete. Every test has assertions.

**Type consistency:**
- `IngestStats` defined in Task 6, used only in Task 6's tests — consistent.
- `IncomingWatcher` constructor in Task 7 is `(incoming_dir, parquet_dir, duckdb_path)` — same signature used in Task 8 (CLI `run`) and Task 9 (e2e test).
- `bootstrap(parquet_dir, duckdb_path)` keyword-only signature is used identically across Tasks 5, 6, 7, 8, 9.
- `compute_task_id(env_task_id, repo_owner, repo_name, branch)` positional signature is used identically in Task 3 tests and Task 6 ingest.
- `make_dedup_key(timestamp_iso, agent_id, session_id, event_type, content)` — same signature used in Task 2 tests and Task 6 ingest.

**Scope check:** This plan produces a working, testable system on its own. End-to-end deliverable: drop a JSONL into `~/nexus/incoming/<host>/`, see the row in `agent_events` and the upserted row in `tasks`. No external dependencies (no AgentWeave, no shippers, no MCP) needed for this slice to be valuable. ✓

---

## Plan complete

This plan covers Tasks 1–11 (~50 atomic steps) and produces:

- Apache-2.0 licensed repo with the committed `nexus.duckdb` removed
- Reusable `make_dedup_key`, `compute_task_id`, `parse_repo_url`, and `NexusConfig` modules
- Idempotent DuckDB schema bootstrap (4 Parquet tables, 3 SQL tables, 6 views)
- `nexus-server` daemon with `init` / `run` / `status` subcommands
- File watcher that ingests dropped JSONL via dedup_key MERGE and partitions by `(date, agent_id)`
- launchd plist + install README for Mac Mini deployment
- 7 new test files (~30 unit + 1 e2e test) all green
- README pointer to the new spec and plans directory

Next plans, in dependency order:
- **Plan 2** — `nexus-collect`: per-host JSONL/SQLite tailing + atomic-rename + rsync to Mac Mini
- **Plan 3** — OTLP receiver: gRPC `:4317` + AgentWeave push integration
- **Plan 4** — MCP server + handoff tools: the agent-facing query surface
- **Plan 5** — Summarizer worker: claude-haiku turning sessions into `session_summaries`
- **Plan 6** — `nexus-hook`: per-harness session lifecycle hooks
- **Plan 7** — historical seed (run `migrate_to_duckdb.py` + reconcile) + GCP/cloud_function decommission
