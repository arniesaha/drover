"""PostgreSQL contract for Studio rollout's read-only queue settlement."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from drover.server.control_store import control_store_config
from drover.server.work_queue.provider import ItemRef
from drover.server.work_queue.store import WorkStore

SCRIPT = Path(__file__).parents[1] / "scripts" / "studio_rollout.py"
SPEC = importlib.util.spec_from_file_location("studio_rollout_postgres", SCRIPT)
assert SPEC and SPEC.loader
ROLLOUT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROLLOUT
SPEC.loader.exec_module(ROLLOUT)


def test_local_queue_settlement_reads_postgres_links(pg_control_path, postgres_dsn):
    workspace_id = "studio-rollout-test"
    store = WorkStore(pg_control_path, workspace_id)
    ref = ItemRef(workspace_id, "github", "arniesaha/drover", 873, "https://test/873")

    fresh = store.intent("control:test", ref, "goal", "fresh", "work", 0, "goal:studio")
    stale = store.intent("control:test", ref, "goal", "stale", "work", 0, "goal:studio")
    store.intent("control:test", ref, "goal", "systemic", "work", 0, "goal:studio")
    with store.connection() as connection:
        connection.execute(
            "UPDATE execution_links SET created_at = now() - interval '11 minutes', "
            "updated_at = now() - interval '11 minutes' WHERE id = ?",
            [stale["id"]],
        )
    store.failure("systemic", 3)

    config = control_store_config(pg_control_path)
    assert config is not None
    state = ROLLOUT.local_work_queue_state(
        postgres_dsn,
        schema=config.schema,
        workspace_id=workspace_id,
        orphan_intent_minutes=10,
        systemic_attempts={"goal": 3},
    )

    assert state["active"] == 1
    assert {row["run_key"] for row in state["stuck_intents"]} == {
        "stale",
        "systemic",
    }
    assert fresh["run_key"] == "fresh"
