from dataclasses import replace
from types import SimpleNamespace

import pytest

from drover.server.harness.daemon import _structured_session_row_json
from drover.server.harness.models import HarnessSession
from drover.server.metrics import _harness_session_dict


@pytest.mark.parametrize("status", ["completed", "terminated", "errored", "failed"])
@pytest.mark.parametrize("awaiting", ["input", "approval", None])
def test_terminal_attention_is_projected_without_rewriting_history(status, awaiting):
    session = HarnessSession("s", "h", "codex", "codex", status, awaiting=awaiting)
    assert session.awaiting == awaiting
    assert session.effective_awaiting is None
    assert _harness_session_dict(session)["awaiting"] is None
    assert _structured_session_row_json(session)["awaiting"] is None
    assert replace(session, status="running").effective_awaiting == awaiting


def test_unsequenced_exit_and_restart_preserve_status_first_projection(tmp_path):
    from drover.schema import bootstrap
    from drover.server.harness.registry import HarnessRegistry

    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=tmp_path / "control.duckdb")
    registry = HarnessRegistry(tmp_path / "control.duckdb")
    session = registry.create_session(host_id="h", harness="codex", command="codex")
    registry.update_session_activity(session.session_id, awaiting="approval")
    registry.append_event(
        session_id=session.session_id, event_type="approval_prompt", seq=2
    )
    registry.update_session_status(session.session_id, "completed")
    exit_event = registry.append_event(
        session_id=session.session_id, event_type="session.exited"
    )
    assert exit_event.seq is None
    restarted = HarnessRegistry(tmp_path / "control.duckdb")
    row = restarted.get_session(session.session_id)
    assert row.awaiting == "approval"
    assert _harness_session_dict(row)["awaiting"] is None

    # Backfill triggers the ordered rebuild, which excludes the unsequenced
    # exit. Terminal status must still dominate the derived attention.
    restarted.ingest_structured_events(
        [
            {
                "event_id": "backfill",
                "session_id": session.session_id,
                "event_type": "tool_start",
                "payload": {},
                "seq": 1,
            }
        ]
    )
    assert (
        _harness_session_dict(restarted.get_session(session.session_id))["awaiting"]
        is None
    )


@pytest.mark.parametrize(
    "status", ["running", "completed", "terminated", "errored", "failed"]
)
def test_wire_projection_accepts_session_records_without_model_properties(status):
    session = SimpleNamespace(status=status, awaiting="approval")
    item = _harness_session_dict(session)
    assert item["awaiting"] == ("approval" if status == "running" else None)
    assert session.awaiting == "approval"
