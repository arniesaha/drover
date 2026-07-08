"""End-to-end smoke test: watcher started in-process, dropped file → DuckDB row."""

import json
import time
from pathlib import Path

import duckdb
import pytest

from drover.schema import bootstrap
from drover.server.watcher import IncomingWatcher


def test_e2e_drop_file_appears_in_duckdb(tmp_path):
    incoming = tmp_path / "incoming"
    parquet_dir = tmp_path / "parquet"
    db_path = tmp_path / "drover.duckdb"
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
        line = json.dumps(
            {
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
            }
        )
        tmp.write_text(line + "\n")
        tmp.rename(target)

        processed_file = host_dir / ".processed" / "e2e-batch.jsonl"
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
            if n and processed_file.exists() and not target.exists():
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
        assert processed_file.exists()
        assert not target.exists()
    finally:
        watcher.stop()
