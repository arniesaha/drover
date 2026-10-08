"""Container production uses only disposable analytical and memory stores."""

from datetime import datetime, timedelta, timezone

import pytest
from memory_helpers import put_brief, put_summary

from drover.config import default_config, load_config
from drover.schema import bootstrap
from drover.server.context_writer import ContextContainerWriter, _key
from drover.server.db import open_duckdb_connection
from drover.server.mcp import tools

STAMP = datetime(2026, 10, 1, tzinfo=timezone.utc)


def seed(path, tmp_path):
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    put_summary(
        path,
        "code",
        project_key="o/r",
        summary_md="Fix recall",
        next_steps_md="Run review",
        open_questions=("Ship next week?",),
        generated_at=STAMP,
        ended_at=STAMP - timedelta(hours=1),
    )
    put_summary(
        path,
        "general",
        summary_md="Weekly planning",
        next_steps_md="Confirm calendar",
        generated_at=STAMP,
    )
    put_brief(
        path,
        "o/r",
        brief_md="Recall project",
        next_steps_md="Review release",
        source_session_id="code",
        generated_at=STAMP,
        last_activity_at=STAMP - timedelta(hours=1),
    )


def test_writer_populates_all_four_readers_and_is_idempotent(pg_control_path, tmp_path):
    path = pg_control_path
    seed(path, tmp_path)
    writer = ContextContainerWriter(path)
    assert writer.run_once() == {
        "mode": "dry-run",
        "sources": 3,
        "created": 3,
        "updated": 0,
        "unchanged": 0,
        "applied": 0,
    }
    assert tools.drover_recent_contexts(duckdb_path=path)["contexts"] == []
    assert writer.run_once(apply=True)["applied"] == 3
    recent = tools.drover_recent_contexts(duckdb_path=path)["contexts"]
    assert len(recent) == 3
    general = tools.drover_context_brief(
        duckdb_path=path, context_id=_key("session", "general")
    )
    assert general["container_type"] == "general_activity"
    assert general["confidence"] == 0.5
    assert general["repo_owner"] is None
    assert general["label"] == "Weekly planning"
    assert "drover://session/general" in general["evidence"]
    assert general["data_watermark"]["basis"] != "unknown"
    loops = tools.drover_open_loops(duckdb_path=path, project_key="o/r")["open_loops"]
    assert len(loops) == 2
    resume = tools.drover_resume_context(
        duckdb_path=path, context_id=_key("project", "o/r")
    )
    assert resume["context"]["summary_md"] == "Recall project"
    assert [s["session_id"] for s in resume["session_summaries"]] == ["code"]
    assert writer.run_once(apply=True)["unchanged"] == 3
    assert tools.drover_recent_contexts(duckdb_path=path)["contexts"] == recent
    put_summary(
        path,
        "code",
        project_key="o/r",
        summary_md="Review complete",
        generated_at=STAMP + timedelta(hours=1),
    )
    assert writer.run_once(apply=True)["updated"] == 1
    updated = tools.drover_context_brief(
        duckdb_path=path, context_id=_key("session", "code")
    )
    assert updated["summary_md"] == "Review complete"
    assert datetime.fromisoformat(updated["created_at"]) == STAMP
    assert datetime.fromisoformat(updated["updated_at"]) == STAMP + timedelta(hours=1)


def test_redaction_and_existing_policies(pg_control_path, tmp_path):
    path = pg_control_path
    seed(path, tmp_path)
    writer = ContextContainerWriter(path)
    writer.run_once(apply=True)
    with open_duckdb_connection(path) as con:
        con.execute(
            "UPDATE context_containers SET redaction_policy='metadata-only' WHERE context_id=?",
            [_key("session", "general")],
        )
        con.execute(
            "UPDATE context_containers SET redaction_policy='private-policy' WHERE context_id=?",
            [_key("project", "o/r")],
        )
    secret = "ghp_abcdefgh12345678"
    put_summary(
        path,
        "general",
        summary_md=f"password: {secret}",
        generated_at=STAMP + timedelta(hours=1),
    )
    put_summary(
        path,
        "code",
        project_key="o/r",
        summary_md=f"token={secret}",
        next_steps_md=f"Bearer {secret}",
        open_questions=(f"api_key: {secret}",),
        generated_at=STAMP + timedelta(hours=1),
    )
    put_brief(
        path,
        "o/r",
        brief_md=f"password={secret}",
        generated_at=STAMP + timedelta(hours=1),
    )
    writer.run_once(apply=True)
    import json

    containers = tools.drover_recent_contexts(duckdb_path=path)["contexts"]
    assert secret not in json.dumps(containers)
    general = tools.drover_context_brief(
        duckdb_path=path, context_id=_key("session", "general")
    )
    assert general["summary_md"] is None
    assert general["redaction_policy"] == "metadata-only"
    assert (
        tools.drover_resume_context(duckdb_path=path, context_id=general["context_id"])[
            "session_summaries"
        ]
        == []
    )
    project = tools.drover_context_brief(
        duckdb_path=path, context_id=_key("project", "o/r")
    )
    assert project["summary_md"] == "Recall project"
    assert project["redaction_policy"] == "private-policy"
    resumed = tools.drover_resume_context(
        duckdb_path=path, context_id=_key("session", "code")
    )
    assert secret not in json.dumps(resumed)


def test_hub_only_and_no_partial_snapshot(pg_control_path, tmp_path):
    with pytest.raises(ValueError, match="hub PostgreSQL"):
        ContextContainerWriter(tmp_path / "local")
    seed(pg_control_path, tmp_path)
    with pytest.raises(ValueError, match="no partial snapshot"):
        ContextContainerWriter(pg_control_path, max_containers=2).run_once(apply=True)
    assert tools.drover_recent_contexts(duckdb_path=pg_control_path)["contexts"] == []


def test_config_flag_defaults_off_and_validates(tmp_path):
    assert default_config().context_containers_enabled is False
    path = tmp_path / "config.toml"
    path.write_text(
        '[control_store]\nbackend = "postgres"\ndsn_env = "DROVER_TEST_POSTGRES_DSN"\n[context_containers]\nenabled = true\n'
    )
    assert load_config(path).context_containers_enabled is True
    path.write_text('[context_containers]\nenabled = "false"\n')
    with pytest.raises(ValueError, match="must be boolean"):
        load_config(path)


def test_source_harness_is_not_the_producer_host(pg_control_path, tmp_path):
    from drover.server.harness.registry import HarnessRegistry

    path = pg_control_path
    seed(path, tmp_path)
    registry = HarnessRegistry(path)
    registry.register_host(host_id="host-a", display_name="Test", kind="test")
    registry.create_session(
        host_id="host-a", harness="codex", command="codex", session_id="code"
    )
    ContextContainerWriter(path).run_once(apply=True)
    context = tools.drover_context_brief(
        duckdb_path=path, context_id=_key("session", "code")
    )
    assert context["source_harness"] == "codex"


def test_old_sources_do_not_overwrite_newer_containers(pg_control_path, tmp_path):
    seed(pg_control_path, tmp_path)
    writer = ContextContainerWriter(pg_control_path)
    writer.run_once(apply=True)
    put_summary(
        pg_control_path,
        "general",
        summary_md="Older version",
        generated_at=STAMP - timedelta(hours=1),
    )
    assert writer.run_once(apply=True)["unchanged"] == 3
    assert (
        tools.drover_context_brief(
            duckdb_path=pg_control_path, context_id=_key("session", "general")
        )["summary_md"]
        == "Weekly planning"
    )


def test_dry_run_does_not_create_an_analytical_database(pg_control_path):
    put_summary(pg_control_path, "s", generated_at=STAMP)
    assert not pg_control_path.exists()
    assert ContextContainerWriter(pg_control_path).run_once()["created"] == 1
    assert not pg_control_path.exists()


def test_large_source_text_is_omitted_before_redaction(pg_control_path, tmp_path):
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=pg_control_path)
    put_summary(
        pg_control_path,
        "s",
        summary_md="x" * 1000000 + " password: opaque-value",
        generated_at=STAMP,
    )
    ContextContainerWriter(pg_control_path).run_once(apply=True)
    resumed = tools.drover_resume_context(
        duckdb_path=pg_control_path, context_id=_key("session", "s")
    )
    assert "source exceeds redaction budget" in resumed["context"]["summary_md"]
    assert "opaque-value" not in str(resumed)
    assert (
        "source exceeds redaction budget"
        in resumed["session_summaries"][0]["summary_md"]
    )
