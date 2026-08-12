"""Tests for summarizer auth diagnostics and retry helpers."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import duckdb

from drover.schema import bootstrap
from drover.server.summarizer.diagnostics import summarize_backend_auth
from drover.server.summarizer.retry import (
    classify_retryable_error,
    classify_summarize_error,
    retry_errored_jobs,
)


def _write_creds(path, token: str, *, expires_at_ms: int | None = None) -> None:
    payload = {
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "rt",
            "expiresAt": (
                expires_at_ms
                if expires_at_ms is not None
                else int((time.time() + 3600) * 1000)
            ),
        }
    }
    path.write_text(json.dumps(payload))


def test_summarizer_auth_diagnostics_reports_file_token_without_llm_call(
    tmp_path, monkeypatch
):
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-live")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    report = summarize_backend_auth(api_model="claude-test", backend_policy="hybrid")

    assert report["backend_policy"] == "hybrid"
    assert report["anthropic_ready"] is True
    assert report["anthropic_configured"] is True
    assert report["auth_sources"]["claude_credentials"]["token_present"] is True
    assert report["auth_sources"]["claude_credentials"]["expired"] is False
    assert report["effective_auth"] == "claude_credentials"
    assert report["api_model"] == "claude-test"


def test_summarizer_auth_diagnostics_flags_expired_credentials(tmp_path, monkeypatch):
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-stale", expires_at_ms=1)
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    report = summarize_backend_auth(backend_policy="hybrid")

    assert report["anthropic_ready"] is False
    assert report["auth_sources"]["claude_credentials"]["exists"] is True
    assert report["auth_sources"]["claude_credentials"]["expired"] is True
    assert "no Anthropic" in " ".join(report["warnings"])


def test_summarizer_auth_diagnostics_reports_local_ollama_without_relay(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "no-creds.json")
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    report = summarize_backend_auth(local_ollama_url="http://127.0.0.1:11435")

    assert report["anthropic_ready"] is False
    assert report["local_configured"] is True
    assert report["local_ollama"] == {"url_present": True, "wake_relay": False}
    assert report["gpu"] == {"relay_url_present": False, "ollama_url_present": False}


def test_summarizer_auth_diagnostics_cloud_policy_blocks_local_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "no-creds.json")
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    report = summarize_backend_auth(
        backend_policy="cloud",
        local_ollama_url="http://127.0.0.1:11435",
    )

    assert report["backend_policy"] == "cloud"
    assert report["anthropic_ready"] is False
    assert report["local_configured"] is True
    assert report["harness_ready"] is False
    assert "backend_policy=cloud" in " ".join(report["warnings"])


def test_summarizer_auth_diagnostics_reports_the_harness_backend(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "no-creds.json")
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    with patch(
        "drover.server.harness.structured.claude.resolve_binary",
        return_value="/usr/local/bin/claude",
    ):
        report = summarize_backend_auth()

    assert report["backend_policy"] == "harness"
    assert report["harness_ready"] is True
    assert report["harness_model"] == "haiku"
    assert report["anthropic_ready"] is False
    assert report["warnings"] == []


def test_summarizer_auth_diagnostics_warns_when_the_cli_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "DROVER_CLAUDE_CREDENTIALS_PATH", str(tmp_path / "no-creds.json")
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    with patch(
        "drover.server.harness.structured.claude.resolve_binary", return_value=None
    ):
        report = summarize_backend_auth()

    assert report["harness_ready"] is False
    assert "claude-code CLI" in " ".join(report["warnings"])


def test_summarizer_auth_diagnostics_reports_local_policy_as_retired(
    tmp_path, monkeypatch
):
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "tok-live")
    monkeypatch.setenv("DROVER_CLAUDE_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)

    with patch(
        "drover.server.harness.structured.claude.resolve_binary",
        return_value="/usr/local/bin/claude",
    ):
        report = summarize_backend_auth(
            backend_policy="local",
            local_ollama_url="http://127.0.0.1:11435",
        )

    assert report["backend_policy"] == "harness"
    assert report["anthropic_ready"] is False
    assert report["harness_ready"] is True
    assert "retired" in " ".join(report["warnings"])


def test_retry_clears_the_dead_letter_streak_so_capped_jobs_can_run_again(tmp_path):
    db = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "INSERT INTO summarize_jobs "
            "(session_id, status, attempts, last_error, dead_letter_streak) "
            "VALUES ('capped', 'dead_lettered', 5, "
            "'claude-code readiness: CLI not found on PATH', 3)"
        )
    finally:
        con.close()

    applied = retry_errored_jobs(db, apply=True)

    assert applied["updated"] == ["capped"]
    con = duckdb.connect(str(db))
    try:
        assert con.execute(
            "SELECT status, dead_letter_streak FROM summarize_jobs "
            "WHERE session_id='capped'"
        ).fetchone() == ("pending", 0)
    finally:
        con.close()


def test_classify_retryable_auth_and_rate_limit_errors_only_by_default():
    assert (
        classify_retryable_error(
            "anthropic: Error code: 401 - invalid authentication credentials"
        )
        is True
    )
    assert classify_retryable_error("429 rate_limit_error: retry later") is True
    assert (
        classify_retryable_error("backend selection failed: no backend configured")
        is True
    )
    assert classify_retryable_error("ollama: empty response field") is True
    assert (
        classify_retryable_error("Out of Memory Error: failed to allocate data") is True
    )
    assert classify_retryable_error("missing required keys: next_steps_md") is False
    assert classify_retryable_error("invalid JSON from model response") is False
    assert (
        classify_retryable_error("missing required keys", include_validation=True)
        is True
    )


def test_classify_summarize_error_groups_runtime_auth_and_validation_failures():
    assert classify_summarize_error(
        "HTTPConnectionPool(host='nas'): Failed to establish a new connection: [Errno 65] No route to host"
    ) == {"category": "runtime", "retryable": True}
    assert classify_summarize_error(
        "GPU WoL relay unreachable: connection refused"
    ) == {"category": "runtime", "retryable": True}
    assert classify_summarize_error(
        "Out of Memory Error: could not allocate block of size 256.0 KiB"
    ) == {"category": "runtime", "retryable": True}
    assert classify_summarize_error(
        "Error code: 401 - invalid x-api-key / stale Anthropic credentials"
    ) == {"category": "auth", "retryable": True}
    assert classify_summarize_error("429 rate limit exceeded") == {
        "category": "rate_limit",
        "retryable": True,
    }
    assert classify_summarize_error("LLM response missing required keys") == {
        "category": "validation",
        "retryable": False,
    }
    assert classify_summarize_error(
        "anthropic: LLM response next_steps_md must be a string",
        include_validation=True,
    ) == {"category": "validation", "retryable": True}
    assert classify_summarize_error(
        "LLM response missing required keys", include_validation=True
    ) == {"category": "validation", "retryable": True}


def test_retry_errored_jobs_dry_run_and_apply_skip_validation_errors(tmp_path):
    db = tmp_path / "drover.duckdb"
    parquet = tmp_path / "parquet"
    bootstrap(parquet_dir=parquet, duckdb_path=db)
    con = duckdb.connect(str(db))
    try:
        con.executemany(
            "INSERT INTO summarize_jobs (session_id, status, attempts, last_error) VALUES (?, 'errored', ?, ?)",
            [
                ("auth", 2, "401 invalid authentication credentials"),
                ("rate", 1, "429 rate limit exceeded"),
                ("schema", 1, "missing required keys: summary_md"),
                ("done", 1, "401 invalid authentication credentials"),
            ],
        )
        con.execute("UPDATE summarize_jobs SET status='done' WHERE session_id='done'")
    finally:
        con.close()

    dry = retry_errored_jobs(db, apply=False)
    assert dry["matched"] == ["auth", "rate"]
    assert dry["updated"] == []

    applied = retry_errored_jobs(db, apply=True)
    assert applied["updated"] == ["auth", "rate"]

    con = duckdb.connect(str(db))
    try:
        rows = dict(
            con.execute("SELECT session_id, status FROM summarize_jobs").fetchall()
        )
    finally:
        con.close()
    assert rows["auth"] == "pending"
    assert rows["rate"] == "pending"
    assert rows["schema"] == "errored"
    assert rows["done"] == "done"
