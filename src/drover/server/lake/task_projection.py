"""Verified, atomic PG task generations. No legacy task table is consulted."""

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

from drover.server.db import control_plane_connection

from .fence import MutationFence
from .query_process import query
from .runtime import LakeError
from .serving import _SELECTION_LOCK, lake_spec, open_history, selected_config

PROJECTION_LOCK = 0x4452565441534B01
MAX_ROWS = 1000
MAX_BYTES = 1024 * 1024


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


@contextmanager
def projection_fence(path):
    from drover.server.control_store import is_postgres_control_store

    if selected_config(path).backend != "ducklake":
        raise LakeError("lake_backend_not_selected")
    if not is_postgres_control_store(path):
        raise LakeError("lake_serving_requires_postgres")
    # Dedicated connection, held through child computation and PG publication.
    with control_plane_connection(path) as con:
        if not con.execute(
            "SELECT pg_try_advisory_lock(?)", [PROJECTION_LOCK]
        ).fetchone()[0]:
            raise LakeError("analytics_task_projection_fenced")
        try:
            yield con
        finally:
            con.execute("SELECT pg_advisory_unlock(?)", [PROJECTION_LOCK])


def _capture(path, *, build=False):
    config = selected_config(path)
    if config.backend != "ducklake":
        raise LakeError("lake_backend_not_selected")
    with open_history(path) as history:
        identities = history.identities
        result = query(
            lake_spec(config),
            "SELECT 1",
            serving={
                "verification_sha256": config.verification_sha256,
                "identities": identities,
                "task_projection": "build" if build else "token",
            },
        )
    return {
        "epoch": config.epoch,
        "proof": config.verification_sha256,
        "root": str(lake_spec(config).data_root.resolve()),
        "snapshot": result["snapshot"],
        "identities": _hash(identities),
    }, result


def provision_task_projections(path):
    """Explicit staging/provisioning only. Runtime never creates these tables."""
    with _SELECTION_LOCK, projection_fence(path) as con:
        _capture(path)
        con.execute("BEGIN")
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS lake_task_generations (
              generation UUID PRIMARY KEY, receipt_seq BIGSERIAL UNIQUE,
              binding TEXT NOT NULL, manifest TEXT NOT NULL,
              task_count INTEGER NOT NULL, session_count INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS lake_task_rows (
              generation UUID NOT NULL REFERENCES lake_task_generations(generation) DEFERRABLE INITIALLY DEFERRED,
              task_id TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(generation,task_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS lake_task_sessions (
              generation UUID NOT NULL REFERENCES lake_task_generations(generation) DEFERRABLE INITIALLY DEFERRED,
              session_id TEXT NOT NULL, task_id TEXT NOT NULL,
              PRIMARY KEY(generation,session_id))""")
            con.execute(
                "CREATE INDEX IF NOT EXISTS lake_task_generation_binding ON lake_task_generations(binding,receipt_seq DESC)"
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def _before_receipt(con):
    """Fault-injection boundary: rows exist, but the completed receipt does not."""


@contextmanager
def _lake_fence(path, existing):
    if existing is None:
        with MutationFence(lake_spec(selected_config(path)).dsn()) as fence:
            yield fence
    else:
        existing.check()
        # The owner connection must address the selected catalog too.
        from .serving_proof import catalog_identity

        identity = existing.connection.execute(
            "SELECT schema_uuid FROM public.ducklake_schema WHERE schema_name='main' AND end_snapshot IS NULL"
        ).fetchone()
        if identity is None or str(identity[0]) != catalog_identity(
            lake_spec(selected_config(path))
        ):
            raise LakeError("analytics_task_projection_catalog_mismatch")
        yield existing


def refresh_task_projections(path, *, lake_fence=None):
    with (
        _SELECTION_LOCK,
        projection_fence(path) as con,
        _lake_fence(path, lake_fence) as fence,
    ):
        binding, result = _capture(path, build=True)
        tasks, sessions = result["tasks"], result["sessions"]
        if len(tasks) + len(sessions) > MAX_ROWS:
            raise LakeError("analytics_row_limit_exceeded")
        generation = str(uuid4())
        manifest = {
            "tasks": {row["task_id"]: _hash(row) for row in tasks},
            "sessions": dict(sessions),
        }
        if len(manifest["tasks"]) != len(tasks) or len(manifest["sessions"]) != len(
            sessions
        ):
            raise LakeError("analytics_task_projection_duplicate")
        # Detect export/identity/epoch changes during computation; the generation
        # remains snapshot-bound even if a later export races the PG commit.
        if _capture(path)[0] != binding:
            raise LakeError("analytics_task_projection_changed")
        con.execute("BEGIN")
        try:
            for row in tasks:
                con.execute(
                    "INSERT INTO lake_task_rows VALUES (?,?,?)",
                    [generation, row["task_id"], _encoded(row)],
                )
            for sid, task in sessions:
                con.execute(
                    "INSERT INTO lake_task_sessions VALUES (?,?,?)",
                    [generation, sid, task],
                )
            _before_receipt(con)
            fence.check()
            held = con.execute(
                """SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                AND pid=pg_backend_pid() AND classid=? AND objid=? AND objsubid=1
                AND mode='ExclusiveLock' AND granted)""",
                [PROJECTION_LOCK >> 32, PROJECTION_LOCK & 0xFFFFFFFF],
            ).fetchone()[0]
            if not held:
                raise LakeError("analytics_task_projection_fence_lost")
            con.execute(
                "INSERT INTO lake_task_generations(generation,binding,manifest,task_count,session_count) VALUES (?,?,?,?,?)",
                [
                    generation,
                    _encoded(binding),
                    _encoded(manifest),
                    len(tasks),
                    len(sessions),
                ],
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return generation


def task_status(path, *, task_id=None, session_id=None):
    try:
        with _SELECTION_LOCK:
            binding, _ = _capture(path)
            with control_plane_connection(path) as con:
                con.execute(
                    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                try:
                    receipt = con.execute(
                        "SELECT generation,manifest,task_count,session_count FROM lake_task_generations WHERE binding=? AND octet_length(manifest)<=1048576 ORDER BY receipt_seq DESC LIMIT 1",
                        [_encoded(binding)],
                    ).fetchone()
                    if not receipt:
                        raise LakeError("analytics_task_projection_unavailable")
                    generation, encoded, task_count, session_count = receipt
                    if (
                        len(encoded.encode()) > MAX_BYTES
                        or task_count + session_count > MAX_ROWS
                    ):
                        raise LakeError("analytics_task_projection_incomplete")
                    manifest = json.loads(encoded)
                    counts = con.execute(
                        "SELECT (SELECT count(*) FROM lake_task_rows WHERE generation=?),(SELECT count(*) FROM lake_task_sessions WHERE generation=?)",
                        [generation, generation],
                    ).fetchone()
                    if (
                        tuple(counts) != (task_count, session_count)
                        or len(manifest["tasks"]) != task_count
                        or len(manifest["sessions"]) != session_count
                    ):
                        raise LakeError("analytics_task_projection_incomplete")
                    # Certify every PG row, not just the requested leaf. No event
                    # history is read. Bound byte size before fetching any text.
                    sizes = con.execute(
                        "SELECT (SELECT coalesce(sum(octet_length(payload)+octet_length(task_id)),0) FROM lake_task_rows WHERE generation=?)+(SELECT coalesce(sum(octet_length(session_id)+octet_length(task_id)),0) FROM lake_task_sessions WHERE generation=?)",
                        [generation, generation],
                    ).fetchone()[0]
                    if sizes > MAX_BYTES:
                        raise LakeError("analytics_task_projection_incomplete")
                    hashes = dict(
                        con.execute(
                            "SELECT task_id,encode(sha256(convert_to(payload,'UTF8')),'hex') FROM lake_task_rows WHERE generation=? LIMIT 1001",
                            [generation],
                        ).fetchall()
                    )
                    mappings = dict(
                        con.execute(
                            "SELECT session_id,task_id FROM lake_task_sessions WHERE generation=? LIMIT 1001",
                            [generation],
                        ).fetchall()
                    )
                    if hashes != manifest["tasks"] or mappings != manifest["sessions"]:
                        raise LakeError("analytics_task_projection_incomplete")
                    if session_id:
                        sid = session_id
                        # Identity aliases belong to the certified identity snapshot.
                        with open_history(path) as history:
                            matches = {
                                harness
                                for harness, native, summary in history.identities
                                if sid in (harness, native, summary)
                            }
                            if len(matches) > 1:
                                raise LakeError("analytics_task_identity_ambiguous")
                            if matches:
                                sid = next(iter(matches))
                        mapping = con.execute(
                            "SELECT task_id FROM lake_task_sessions WHERE generation=? AND session_id=?",
                            [generation, sid],
                        ).fetchone()
                        if mapping and manifest["sessions"].get(sid) != mapping[0]:
                            raise LakeError("analytics_task_projection_incomplete")
                        if not mapping and sid in manifest["sessions"]:
                            raise LakeError("analytics_task_projection_incomplete")
                        task_id = mapping[0] if mapping else None
                    row = con.execute(
                        "SELECT payload FROM lake_task_rows WHERE generation=? AND task_id=?",
                        [generation, task_id],
                    ).fetchone()
                    if row:
                        if len(row[0].encode()) > MAX_BYTES:
                            raise LakeError("analytics_task_projection_incomplete")
                        payload = json.loads(row[0])
                        if _hash(payload) != manifest["tasks"].get(task_id):
                            raise LakeError("analytics_task_projection_incomplete")
                    else:
                        if task_id in manifest["tasks"]:
                            raise LakeError("analytics_task_projection_incomplete")
                        payload = {"status": "unknown", "task_id": task_id}
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            # Reads never refresh from history. Reject changes during the PG read.
            if _capture(path)[0] != binding:
                raise LakeError("analytics_task_projection_changed")
            return payload
    except LakeError:
        raise
    except Exception:
        raise LakeError("analytics_task_projection_unavailable") from None


def refresh_if_provisioned(path, *, lake_fence=None):
    from drover.server.control_store import is_postgres_control_store

    if selected_config(path).backend != "ducklake" or not is_postgres_control_store(
        path
    ):
        return
    with control_plane_connection(path) as con:
        present = con.execute("SELECT to_regclass('lake_task_generations')").fetchone()[
            0
        ]
    if present:
        refresh_task_projections(path, lake_fence=lake_fence)


def build_in_child(con, *, build):
    snapshot = con.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[
        0
    ]
    result = {"snapshot": snapshot, "rows": []}
    if not build:
        return result
    cursor = con.execute(
        """SELECT task_id,
      arg_max(repo_owner,struct_pack(ts:=timestamp,id:=id,repo:=repo_owner,name:=repo_name,branch:=branch,principal:=principal_id)) AS repo_owner,
      arg_max(repo_name,struct_pack(ts:=timestamp,id:=id,repo:=repo_owner,name:=repo_name,branch:=branch,principal:=principal_id)) AS repo_name,
      arg_max(branch,struct_pack(ts:=timestamp,id:=id,repo:=repo_owner,name:=repo_name,branch:=branch,principal:=principal_id)) AS branch,
      arg_max(principal_id,struct_pack(ts:=timestamp,id:=id,repo:=repo_owner,name:=repo_name,branch:=branch,principal:=principal_id)) AS principal_id,
      min(timestamp) AS created_at,max(timestamp) AS last_activity_at,
      count(DISTINCT session_id) AS session_count,count(DISTINCT agent_id) AS agent_count
      FROM agent_events WHERE task_id IS NOT NULL GROUP BY task_id ORDER BY task_id LIMIT 1001"""
    )
    names = [field[0] for field in cursor.description]
    tasks = [dict(zip(names, row)) for row in cursor.fetchall()]
    for task in tasks:
        for key in ("created_at", "last_activity_at"):
            if task[key] is not None:
                task[key] = task[key].isoformat()
        task.update(
            status="observed",
            total_cost_usd=None,
            status_source="canonical_events",
            cost_coverage="unavailable",
        )
    sessions = con.execute("""SELECT session_id,task_id FROM agent_events
      WHERE task_id IS NOT NULL AND session_id IS NOT NULL
      QUALIFY row_number() OVER(PARTITION BY session_id ORDER BY timestamp DESC NULLS LAST,id DESC NULLS LAST,task_id DESC)=1
      ORDER BY session_id LIMIT 1001""").fetchall()
    if len(tasks) + len(sessions) > MAX_ROWS:
        raise LakeError("analytics_row_limit_exceeded")
    return {**result, "tasks": tasks, "sessions": sessions}
