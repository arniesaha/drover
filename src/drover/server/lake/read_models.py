"""Existing activity algorithms executed inside the admitted lake child."""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from drover.config import ControlStoreConfig
from drover.server.control_store import configure_control_store, control_store_config

from .query_process import query
from .runtime import LakeError
from .serving import _SELECTION_LOCK, lake_spec, open_history, selected_config


def read_model(path, operation, **options):
    with _SELECTION_LOCK:
        return _read_model(path, operation, **options)


def _read_model(path, operation, **options):
    if operation == "project_activity" and options.get("project_key") is not None:
        owner, sep, name = options["project_key"].partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError("project_key must be one <owner>/<name> pair")
    config = selected_config(path)
    if config.backend != "ducklake":
        raise LakeError("lake_backend_not_selected")
    control = control_store_config(path)
    if control is None or control.backend != "postgres":
        raise LakeError("lake_serving_requires_postgres")
    from .task_projection import _capture, _hash

    if operation in ("fleet", "contexts"):
        limit = options.get("limit", 1000)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise LakeError("analytics_row_limit_exceeded")
    binding, _ = _capture(path)
    # PG identity snapshot is bounded by the existing cursor path.
    with open_history(path) as history:
        if _hash(history.identities) != binding["identities"]:
            raise LakeError("analytics_identity_changed")
        result = query(
            lake_spec(config),
            "SELECT 1",
            serving={
                "verification_sha256": config.verification_sha256,
                "identities": history.identities,
                "operation": operation,
                "binding": binding,
                "options": options,
                "control_path": str(path),
                "control_store": asdict(control),
            },
        )
    if result["binding"] != binding or _capture(path)[0] != binding:
        raise LakeError("analytics_read_model_changed")
    return result["payload"]


def _control_snapshot(con, request):
    from drover.server.db import (
        _POSTGRES_ANALYTICS_SNAPSHOT_TABLES,
        control_plane_connection,
    )

    path = Path(request["control_path"])
    configure_control_store(path, ControlStoreConfig(**request["control_store"]))
    with control_plane_connection(path) as pg:
        pg.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            for table, columns in _POSTGRES_ANALYTICS_SNAPSHOT_TABLES.items():
                names = ",".join('"' + name + '"' for name, _ in columns)
                rows = pg.execute(
                    f'SELECT {names} FROM "{table}" LIMIT 10001'
                ).fetchall()
                if len(rows) > 10000:
                    raise LakeError("analytics_control_row_limit_exceeded")
                definitions = ",".join(f'"{name}" {kind}' for name, kind in columns)
                con.execute(f"CREATE TEMP TABLE {table} ({definitions})")
                if rows:
                    con.executemany(
                        f'INSERT INTO {table} VALUES ({",".join("?" for _ in columns)})',
                        rows,
                    )
            from .task_projection import _hash

            identities = pg.execute(
                "SELECT session_id,native_session_id,summary_session_id FROM harness_sessions ORDER BY session_id LIMIT 10001"
            ).fetchall()
            if _hash(identities) != request["binding"]["identities"]:
                raise LakeError("analytics_identity_changed")
            # Legacy PG rollup clocks are diagnostic only: they do not prove
            # coverage of this verified lake's native snapshot.
            source_at, rolled_at = pg.execute(
                "SELECT max(source_activity_at),max(rolled_at) FROM native_usage_partition_watermarks"
            ).fetchone()
            freshness = {
                "native_publication": {
                    "freshness": "unavailable",
                    "observed_at": None,
                    "reason": "native_publication_not_proven",
                },
                "native_usage": {
                    "freshness": "unavailable",
                    "observed_at": rolled_at,
                    "source_activity_at": source_at,
                    "reason": "lake_coverage_unverified",
                },
            }
            pg.execute("COMMIT")
            return freshness
        except BaseException:
            pg.execute("ROLLBACK")
            raise


def run_model(con, request, limits):
    native_freshness = _control_snapshot(con, request)
    # These are ephemeral query relations, never catalog tables or cached files.
    con.execute(
        "CREATE TEMP VIEW agent_event_partitions AS SELECT DISTINCT date FROM agent_events"
    )
    con.execute(
        "CREATE TEMP MACRO agent_events_for_date(day) AS TABLE SELECT * FROM agent_events WHERE date=day"
    )
    # Optional/legacy spans are excluded; only an empty typed binder is needed
    # by the shared SQL, without attaching or migrating any span store.
    strings = "session_id agent_id date repo_owner repo_name harness llm_provider llm_model agent_model".split()
    numbers = "total_tokens prompt_tokens completion_tokens cost_usd cache_read_tokens cache_write_tokens duration_ms".split()
    fields = (
        [f"NULL::VARCHAR AS {name}" for name in strings]
        + [f"NULL::DOUBLE AS {name}" for name in numbers]
        + ["NULL::TIMESTAMPTZ AS start_time", "NULL::TIMESTAMPTZ AS end_time"]
    )
    con.execute(
        "CREATE TEMP MACRO spans_for_date(day) AS TABLE SELECT "
        + ",".join(fields)
        + " WHERE FALSE"
    )
    options = request["options"]
    operation = request["operation"]
    binding = {
        **request["binding"],
        "snapshot": con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0],
    }
    for value in native_freshness.values():
        value.update(generation=None, coverage_binding=binding)
    if operation == "contexts":
        payload = {
            "status": "unavailable",
            "analytics_backend": "ducklake",
            "analytics_epoch": binding["epoch"],
            "reason": "analytics_context_projection_unavailable",
        }
    elif operation == "fleet":
        cursor = con.execute(
            """SELECT s.session_id,s.host_id AS agent_id,
            s.repo_owner,s.repo_name,s.branch,s.started_at,
            coalesce(s.last_activity,s.updated_at) AS last_event_at,s.status,s.harness
            FROM harness_sessions s JOIN harness_hosts h ON s.host_id=h.host_id
            WHERE h.retired_at IS NULL AND s.status IN ('running','awaiting')
            AND s.ended_at IS NULL
            ORDER BY last_event_at DESC NULLS LAST,s.session_id LIMIT ?""",
            [options.get("limit", 1000) + 1],
        )
        names = [field[0] for field in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        if len(rows) > options.get("limit", 1000):
            raise LakeError("analytics_row_limit_exceeded")
        for row in rows:
            for name in ("started_at", "last_event_at"):
                if row[name] is not None:
                    row[name] = row[name].isoformat()
            row.update(task_id=None, event_count=None, latest_user_message=None)
        payload = {
            "active_sessions": rows,
            "count": len(rows),
            "status_source": "postgres_registry",
            "event_coverage": "unavailable",
        }
    elif operation == "cockpit":
        from drover.server.cockpit.analytics import (
            AnalyticsCursorCodec,
            AnalyticsFilters,
            activity_analytics,
        )

        result = activity_analytics(
            con,
            AnalyticsFilters(**options["filters"]),
            cursor_codec=AnalyticsCursorCodec(bytes.fromhex(options["cursor_secret"])),
            spans_enabled=False,
        )
        payload = asdict(result)
        payload["metadata"].update(native_freshness)
    elif operation == "project_activity":
        from drover.server.project_activity import project_activity

        options = dict(options)
        if options.get("now"):
            options["now"] = datetime.fromisoformat(options["now"])
        payload = project_activity(
            con, memory_store_path=Path(request["control_path"]), **options
        )
    else:
        raise LakeError("analytics_read_model_unknown")

    if operation in ("fleet", "contexts"):
        payload["metadata"] = dict(native_freshness)
    if "metadata" in payload:
        payload["metadata"]["binding"] = binding

    def list_rows(value):
        if isinstance(value, list):
            return len(value) + sum(list_rows(item) for item in value)
        if isinstance(value, dict):
            return sum(list_rows(item) for item in value.values())
        return 0

    if list_rows(payload) > limits.rows:
        raise LakeError("analytics_row_limit_exceeded")
    return {"payload": payload, "binding": binding, "rows": []}
