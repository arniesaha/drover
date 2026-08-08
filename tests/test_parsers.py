import json
import pytest
from datetime import datetime
from pathlib import Path
from drover.models import AgentEvent
from drover.parsers import (
    parse_claude_audit_log,
    parse_hermes_sessions,
    parse_task_journal,
    parse_openclaw_sessions,
)


@pytest.fixture
def mock_claude_log(tmp_path):
    f = tmp_path / "claude.jsonl"
    data = {
        "type": "message",
        "session_id": "test-session",
        "_audit_timestamp": "2026-04-25T12:00:00Z",
        "uuid": "test-uuid",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "bash", "input": {"command": "ls"}}
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    f.write_text(json.dumps(data))
    return str(f)


@pytest.fixture
def mock_hermes_session(tmp_path):
    f = tmp_path / "hermes.json"
    data = {
        "session_id": "hermes-123",
        "messages": [
            {"role": "user", "content": "Hello", "timestamp": "2026-04-25T12:00:00Z"},
            {"role": "assistant", "content": "Hi there"},
        ],
    }
    f.write_text(json.dumps(data))
    return str(f)


@pytest.fixture
def mock_openclaw_log(tmp_path):
    f = tmp_path / "openclaw.jsonl"
    lines = [
        json.dumps(
            {"type": "session", "id": "claw-abc", "timestamp": "2026-04-25T12:00:00Z"}
        ),
        json.dumps(
            {
                "type": "message",
                "id": "msg-1",
                "message": {"role": "user", "content": "Run tests"},
            }
        ),
    ]
    f.write_text("\n".join(lines))
    return str(f)


def test_parse_claude(mock_claude_log):
    events = parse_claude_audit_log(mock_claude_log)
    assert len(events) == 1
    assert events[0].session_id == "test-session"
    assert events[0].agent_id == "claude-code"
    assert events[0].token_usage["input_tokens"] == 10
    assert len(events[0].tool_calls) == 1
    assert events[0].tool_calls[0].tool_name == "bash"


def test_parse_claude_preserves_cwd_in_raw_data(tmp_path):
    """Regression for #48: every Claude Code JSONL turn carries ``cwd`` and we
    must forward it intact into ``AgentEvent.raw_data`` so downstream
    attribution can pin events to a repo. Historically the
    ``work-macbook-claude`` source landed 178k rows with ``raw_data.cwd=None``
    because something between the parser and the lakehouse was filtering keys;
    this test locks the parser side of that contract."""
    f = tmp_path / "claude.jsonl"
    event_in = {
        "parentUuid": None,
        "isSidechain": False,
        "type": "user",
        "message": {"role": "user", "content": "hello"},
        "uuid": "evt-cwd-1",
        "timestamp": "2026-05-19T12:00:00Z",
        "sessionId": "sess-cwd",
        "cwd": "/Users/foo/work/myproject",
        "gitBranch": "main",
        "version": "1.0",
        "userType": "external",
    }
    f.write_text(json.dumps(event_in))

    events = parse_claude_audit_log(str(f), agent_id="work-macbook-claude")
    assert len(events) == 1
    event = events[0]
    assert event.agent_id == "work-macbook-claude"
    assert event.raw_data.get("cwd") == "/Users/foo/work/myproject"
    # The full source dict should land in raw_data, not a hand-picked subset:
    # any key the harness emits today (or tomorrow) must round-trip so we don't
    # silently lose attribution signals again.
    for key in ("uuid", "sessionId", "gitBranch", "version", "userType"):
        assert event.raw_data.get(key) == event_in[key]


def test_parse_claude_populates_missing_cwd_from_project_directory(
    tmp_path: Path, monkeypatch
):
    """Claude Code project files encode cwd in the parent directory name.

    Some producer-side rows such as ai-title/permission-mode/queue-operation do
    not carry a per-row cwd even though the surrounding JSONL file is scoped to
    a project. The parser should recover that cwd from the project directory so
    downstream attribution does not report these as missing producer metadata.
    """
    monkeypatch.setenv(
        "DROVER_CLAUDE_CWD_MAP",
        '{"-srv-projects-example": "/srv/projects/example"}',
    )
    monkeypatch.setenv(
        "DROVER_REPO_ROOTS_JSON",
        '{"/srv/projects/example": "acme/example"}',
    )
    project_dir = tmp_path / "-srv-projects-example"
    project_dir.mkdir()
    f = project_dir / "session.jsonl"
    event_types = ["ai-title", "permission-mode", "queue-operation", "file-history"]
    f.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": event_type,
                    "uuid": f"evt-{event_type}",
                    "timestamp": "2026-05-19T12:00:00Z",
                    "sessionId": "sess-title",
                }
            )
            for event_type in event_types
        )
    )

    events = parse_claude_audit_log(str(f), agent_id="laptop-claude")

    assert len(events) == len(event_types)
    for event in events:
        raw = event.raw_data
        assert raw["cwd"] == "/srv/projects/example"
        assert raw["_repo_owner"] == "acme"
        assert raw["_repo_name"] == "example"


def test_parse_claude_does_not_override_explicit_cwd_with_project_directory(
    tmp_path: Path,
):
    project_dir = tmp_path / "-Users-arnabmac-jenny-nexus"
    project_dir.mkdir()
    f = project_dir / "session.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "evt-cwd-explicit",
                "timestamp": "2026-05-19T12:00:00Z",
                "sessionId": "sess-explicit",
                "cwd": "/tmp/not-a-repo",
            }
        )
    )

    events = parse_claude_audit_log(str(f), agent_id="macmini-claude")

    assert events[0].raw_data["cwd"] == "/tmp/not-a-repo"
    assert "_repo_owner" not in events[0].raw_data
    assert "_repo_name" not in events[0].raw_data


def test_parse_claude_home_and_observer_project_dirs_stay_general_context(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv(
        "DROVER_CLAUDE_CWD_MAP",
        '{"-srv-operator": "/srv/operator", "-srv-operator--agent-memory": "/srv/operator/.agent-memory"}',
    )
    monkeypatch.setenv(
        "DROVER_GENERAL_WORKSPACE_ROOTS",
        "/srv/operator:/srv/operator/.agent-memory",
    )
    for dirname, expected_cwd in {
        "-srv-operator": "/srv/operator",
        "-srv-operator--agent-memory": "/srv/operator/.agent-memory",
    }.items():
        project_dir = tmp_path / dirname
        project_dir.mkdir()
        f = project_dir / "session.jsonl"
        f.write_text(
            json.dumps(
                {
                    "type": "permission-mode",
                    "uuid": f"evt-{dirname}",
                    "timestamp": "2026-05-19T12:00:00Z",
                    "sessionId": "sess-general",
                }
            )
        )

        events = parse_claude_audit_log(str(f), agent_id="macmini-claude")
        raw = events[0].raw_data

        assert raw["cwd"] == expected_cwd
        assert "_repo_owner" not in raw
        assert "_repo_name" not in raw
        assert raw["_nexus_activity_type"] == "general_workspace"


def test_parse_claude_decodes_configured_hyphenated_project_directories(
    tmp_path: Path, monkeypatch
):
    """Known live project-dir names include hyphenated repo segments.

    A generic hyphen-to-slash decode would turn `agent-max` into `agent/max`;
    known prefixes preserve the actual cwd so the existing git/known-root
    enrichment can attribute project sessions correctly.
    """
    monkeypatch.setenv(
        "DROVER_CLAUDE_CWD_MAP",
        '{"-srv-projects-agent-tools": "/srv/projects/agent-tools"}',
    )
    for dirname, expected_cwd in {
        "-srv-projects-agent-tools": "/srv/projects/agent-tools",
    }.items():
        project_dir = tmp_path / dirname
        project_dir.mkdir()
        f = project_dir / "session.jsonl"
        f.write_text(
            json.dumps(
                {
                    "type": "ai-title",
                    "uuid": f"evt-{dirname}",
                    "timestamp": "2026-05-19T12:00:00Z",
                    "sessionId": "sess-hyphenated-project",
                }
            )
        )

        events = parse_claude_audit_log(str(f), agent_id="macmini-claude")

        assert events[0].raw_data["cwd"] == expected_cwd


def test_parse_hermes(mock_hermes_session):
    events = parse_hermes_sessions(mock_hermes_session)
    assert len(events) == 2
    assert events[0].message.role == "user"
    assert events[0].message.content == "Hello"
    assert events[1].message.role == "assistant"
    assert events[1].message.content == "Hi there"


def test_parse_openclaw(mock_openclaw_log):
    events = parse_openclaw_sessions(mock_openclaw_log)
    assert len(events) == 1  # session event is skipped in current parser
    assert events[0].session_id == "claw-abc"
    assert events[0].agent_id == "openclaw"
    assert events[0].message.role == "user"
    assert events[0].message.content == "Run tests"


def test_parse_openclaw_hoists_session_cwd(tmp_path):
    """The session header's cwd should propagate to each subsequent event so
    enrich_raw_repo_attribution can resolve a repo."""
    import subprocess

    repo = tmp_path / "fork"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:arniesaha/openclaw.git"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True
    )

    f = tmp_path / "session.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "id": "sess-1",
                "timestamp": "2026-04-25T12:00:00Z",
                "cwd": str(repo),
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "model_change",
                "id": "evt-1",
                "parentId": None,
                "timestamp": "2026-04-25T12:00:01Z",
                "provider": "anthropic",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "evt-2",
                "parentId": "evt-1",
                "timestamp": "2026-04-25T12:00:02Z",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 2
    for e in events:
        assert e.raw_data["cwd"] == str(repo)
        assert e.raw_data["_repo_owner"] == "arniesaha"
        assert e.raw_data["_repo_name"] == "openclaw"
        assert e.raw_data["gitBranch"] == "main"


def test_parse_openclaw_hoists_session_workspacedir(tmp_path):
    """OpenClaw session headers may use ``workspaceDir`` instead of ``cwd``;
    the parser must still hoist it onto subsequent events."""
    import subprocess

    repo = tmp_path / "fork"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:arniesaha/openclaw.git"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True
    )

    f = tmp_path / "session.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "id": "sess-1",
                "timestamp": "2026-04-25T12:00:00Z",
                "workspaceDir": str(repo),
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "evt-1",
                "parentId": None,
                "timestamp": "2026-04-25T12:00:01Z",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 1
    assert events[0].raw_data["cwd"] == str(repo)
    assert events[0].raw_data["_repo_owner"] == "arniesaha"
    assert events[0].raw_data["_repo_name"] == "openclaw"
    assert events[0].raw_data["gitBranch"] == "main"


def test_parse_openclaw_event_cwd_wins_over_session_cwd(tmp_path):
    """If an event already carries its own cwd, don't overwrite it."""
    f = tmp_path / "session.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "id": "sess-1",
                "timestamp": "2026-04-25T12:00:00Z",
                "cwd": "/session/dir",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "custom",
                "id": "evt-1",
                "parentId": None,
                "timestamp": "2026-04-25T12:00:01Z",
                "cwd": "/event/dir",
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 1
    assert events[0].raw_data["cwd"] == "/event/dir"


def _openclaw_contract_events():
    fixture = Path(__file__).parent / "fixtures" / "openclaw_session_contract.jsonl"
    return parse_openclaw_sessions(str(fixture))


def test_parse_openclaw_contract_preserves_session_uuid_and_key():
    events = _openclaw_contract_events()
    assert len(events) == 4

    for event in events:
        assert event.session_id == "018f-openclaw-main-0001"
        assert event.session_id != "agent:main:main"
        assert event.raw_data["session_uuid"] == "018f-openclaw-main-0001"
        assert event.raw_data["session_key"] == "agent:main:main"

    tool_event = next(e for e in events if e.id == "evt-tool-1")
    assert tool_event.raw_data["event_name"] == "tool.result"
    assert tool_event.event_type == "tool_result"


def test_parse_openclaw_contract_sets_harness_and_normalized_type():
    events = _openclaw_contract_events()
    event_types = {event.id: event.event_type for event in events}
    assert event_types == {
        "evt-user-1": "user_turn",
        "evt-assistant-1": "assistant_turn",
        "evt-tool-1": "tool_result",
        "evt-child-1": "lifecycle",
    }

    first = events[0]
    assert first.raw_data["harness"] == "openclaw"
    assert first.raw_data["event_name"] == "message.queued"
    assert first.raw_data["harness_version"] == "0.7.1"
    assert first.raw_data["runtime_id"] == "openclaw:demo-install"
    assert first.raw_data["runtime_api"] == "diagnostic-events/v1"
    assert first.raw_data["cwd"] == "/tmp/nexus-demo"
    assert first.raw_data["workspace_dir"] == "/tmp/nexus-demo"
    assert first.raw_data["repository"] == "https://github.com/example/nexus-demo.git"
    assert first.raw_data["project"] == "example/nexus-demo"
    assert first.raw_data["topic"] == "Implement parser contract"
    assert first.raw_data["redaction"] == {
        "level": "preview",
        "fields": ["message.content"],
        "preview_bytes": 2000,
    }
    assert first.raw_data["provenance"] == {
        "trace_id": "11111111111111111111111111111111",
        "span_id": "2222222222222222",
        "parent_span_id": "0000000000000000",
        "source": "agentweave",
    }


def test_parse_openclaw_contract_preserves_parent_child_links():
    events = _openclaw_contract_events()
    child_event = next(e for e in events if e.id == "evt-child-1")

    assert child_event.raw_data["parent_session_uuid"] == "018f-openclaw-main-0001"
    assert child_event.raw_data["parent_session_key"] == "agent:main:main"
    assert child_event.raw_data["child_session_uuid"] == "018f-openclaw-child-0002"
    assert child_event.raw_data["child_session_key"] == "agent:main:subagent:reviewer"
    assert child_event.raw_data["agent_id"] == "reviewer"
    assert child_event.raw_data["agent_type"] == "subagent"


def test_parse_openclaw_contract_derives_repo_from_repository_without_cwd(tmp_path):
    f = tmp_path / "repository-only-openclaw.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "session_uuid": "66666666-2222-4333-8444-555555555555",
                "sessionKey": "agent:repo-only:main",
                "repository": "git@github.com:example/repository-only.git",
                "project": "example/repository-only",
                "topic": "repo metadata without cwd",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message.queued",
                "id": "evt-repository-only",
                "timestamp": "2026-04-25T12:00:01Z",
                "message": {"role": "user", "content": "attribute this session"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 1
    event = events[0]
    assert event.raw_data["repository"] == "git@github.com:example/repository-only.git"
    assert event.raw_data["project"] == "example/repository-only"
    assert event.raw_data["_repo_owner"] == "example"
    assert event.raw_data["_repo_name"] == "repository-only"
    assert "cwd" not in event.raw_data


def test_parse_openclaw_contract_backwards_compatible_missing_uuid(tmp_path):
    f = tmp_path / "legacy-openclaw.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "id": "legacy-route-key",
                "sessionKey": "agent:legacy:main",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "legacy-msg",
                "message": {"role": "user", "content": "Run tests"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 1
    assert events[0].session_id == "legacy-route-key"
    assert events[0].event_type == "message"
    assert events[0].message.role == "user"
    assert events[0].message.content == "Run tests"
    assert events[0].raw_data["harness"] == "openclaw"
    assert events[0].raw_data["session_uuid_missing"] is True
    assert "session_uuid" not in events[0].raw_data
    assert events[0].raw_data["session_key"] == "agent:legacy:main"


def test_parse_openclaw_session_id_uuid_is_canonical_with_separate_key(tmp_path):
    f = tmp_path / "session-id-uuid.jsonl"
    canonical_uuid = "11111111-2222-4333-8444-555555555555"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "id": canonical_uuid,
                "sessionKey": "agent:uuid-session:main",
                "timestamp": "2026-04-25T12:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "evt-uuid-id",
                "timestamp": "2026-04-25T12:00:01Z",
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 1
    assert events[0].session_id == canonical_uuid
    assert events[0].raw_data["session_uuid"] == canonical_uuid
    assert events[0].raw_data["session_key"] == "agent:uuid-session:main"
    assert "session_uuid_missing" not in events[0].raw_data


def test_parse_openclaw_new_session_uuid_does_not_inherit_prior_session_state(tmp_path):
    f = tmp_path / "session-state-scope.jsonl"
    first_uuid = "22222222-2222-4333-8444-555555555555"
    second_uuid = "33333333-2222-4333-8444-555555555555"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "session_uuid": first_uuid,
                "sessionKey": "agent:first:main",
                "cwd": "/first/cwd",
                "repository": "https://github.com/example/first.git",
                "project": "example/first",
                "topic": "first topic",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "evt-second-session",
                "session_uuid": second_uuid,
                "timestamp": "2026-04-25T12:00:01Z",
                "message": {"role": "user", "content": "new session"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 1
    event = events[0]
    assert event.session_id == second_uuid
    assert event.raw_data["session_uuid"] == second_uuid
    assert "session_key" not in event.raw_data
    assert "cwd" not in event.raw_data
    assert "repository" not in event.raw_data
    assert "project" not in event.raw_data
    assert "topic" not in event.raw_data


def test_parse_openclaw_returning_session_uuid_preserves_session_state(tmp_path):
    f = tmp_path / "interleaved-session-state.jsonl"
    first_uuid = "44444444-2222-4333-8444-555555555555"
    second_uuid = "55555555-2222-4333-8444-555555555555"
    f.write_text(
        json.dumps(
            {
                "type": "session",
                "session_uuid": first_uuid,
                "sessionKey": "agent:first:main",
                "cwd": "/first/cwd",
                "repository": "https://github.com/example/first.git",
                "project": "example/first",
                "topic": "first topic",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "session",
                "session_uuid": second_uuid,
                "sessionKey": "agent:second:main",
                "cwd": "/second/cwd",
                "repository": "https://github.com/example/second.git",
                "project": "example/second",
                "topic": "second topic",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message.queued",
                "id": "evt-first-return",
                "session_uuid": first_uuid,
                "timestamp": "2026-04-25T12:00:01Z",
                "message": {"role": "user", "content": "back to first"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message.queued",
                "id": "evt-first-override",
                "session_uuid": first_uuid,
                "timestamp": "2026-04-25T12:00:02Z",
                "cwd": "/event/cwd",
                "project": "example/event-project",
                "message": {"role": "user", "content": "override metadata"},
            }
        )
        + "\n"
    )

    events = parse_openclaw_sessions(str(f))
    assert len(events) == 2

    returned = events[0]
    assert returned.session_id == first_uuid
    assert returned.raw_data["session_uuid"] == first_uuid
    assert returned.raw_data["session_key"] == "agent:first:main"
    assert returned.raw_data["cwd"] == "/first/cwd"
    assert returned.raw_data["repository"] == "https://github.com/example/first.git"
    assert returned.raw_data["project"] == "example/first"
    assert returned.raw_data["topic"] == "first topic"

    overridden = events[1]
    assert overridden.session_id == first_uuid
    assert overridden.raw_data["session_key"] == "agent:first:main"
    assert overridden.raw_data["cwd"] == "/event/cwd"
    assert overridden.raw_data["project"] == "example/event-project"
    assert overridden.raw_data["repository"] == "https://github.com/example/first.git"


import sqlite3


@pytest.fixture
def mock_pimono_db(tmp_path):
    db_path = tmp_path / "task-journal.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            created_at INTEGER,
            type TEXT,
            source TEXT,
            payload TEXT,
            status TEXT,
            result TEXT
        )
    """)
    cursor.execute("""
        INSERT INTO tasks (created_at, type, source, payload, status, result)
        VALUES (1777000000, "chat", "mac-mini", '{"message": "Test task"}', "completed", "done")
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def test_parse_pimono(mock_pimono_db):
    events = parse_task_journal(mock_pimono_db)
    assert len(events) == 1
    assert events[0].agent_id == "max-pimono"
    assert events[0].message.role == "user"
    assert events[0].message.content == "Test task"


# NOTE: The former `test_cloud_function_parsers_is_identical_to_src` enforced
# byte-equality between src/nexus/parsers.py and the bundled copy under
# src/nexus/cloud_function/nexus/parsers.py. Both the cloud function and that
# bundled copy were retired with the GCP teardown (2026-05-09). The cloud
# function source now lives under legacy/src/cloud_function/ for reference.
