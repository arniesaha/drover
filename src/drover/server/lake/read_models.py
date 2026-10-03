"""Existing activity algorithms executed inside the admitted lake child."""

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from drover.config import ControlStoreConfig
from drover.server.control_store import configure_control_store, control_store_config

from .query_process import query
from .runtime import LakeError
from .serving import lake_spec, open_history, selected_config


def read_model(path, operation, **options):
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
    # PG identity snapshot is bounded by the existing cursor path.
    with open_history(path) as history:
        result = query(
            lake_spec(config),
            "SELECT 1",
            serving={
                "verification_sha256": config.verification_sha256,
                "identities": history.identities,
                "operation": operation,
                "options": options,
                "control_path": str(path),
                "control_store": asdict(control),
            },
        )
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
            pg.execute("COMMIT")
        except BaseException:
            pg.execute("ROLLBACK")
            raise


def run_model(con, request, limits):
    _control_snapshot(con, request)
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
    if operation == "cockpit":
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

    def list_rows(value):
        if isinstance(value, list):
            return len(value) + sum(list_rows(item) for item in value)
        if isinstance(value, dict):
            return sum(list_rows(item) for item in value.values())
        return 0

    if list_rows(payload) > limits.rows:
        raise LakeError("analytics_row_limit_exceeded")
    return {"payload": payload, "rows": []}
