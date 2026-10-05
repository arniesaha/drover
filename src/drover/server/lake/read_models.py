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
            raise LakeError("invalid_project_key")
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
    from .coverage import heads

    source_heads = heads(path)
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
    if (
        result["binding"] != binding
        or _capture(path)[0] != binding
        or heads(path) != source_heads
    ):
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
            op = request.get("operation")
            opts = request.get("options", {})
            for table, columns in _POSTGRES_ANALYTICS_SNAPSHOT_TABLES.items():
                where = ""
                params = []
                if op in ("cockpit", "project_activity") and table in (
                    "harness_sessions",
                    "session_usage",
                ):
                    if op == "cockpit":
                        filters = opts.get("filters", {})
                        days = filters.get("days", 7)
                        project = filters.get("project")
                    else:
                        days = opts.get("days", 7)
                        project = opts.get("project_key")

                    try:
                        days = int(days)
                    except (TypeError, ValueError):
                        raise LakeError("invalid_analytics_interval")
                    if not (1 <= days <= 366):
                        raise LakeError("invalid_analytics_interval")

                    cutoff = "(current_timestamp - interval '1 day' * ?)"
                    params.append(days + 2)

                    harness_where = f"command IS DISTINCT FROM 'collector' AND (coalesce(last_activity, updated_at, started_at) >= {cutoff})"
                    if project:
                        parts = project.split("/")
                        if len(parts) != 2:
                            raise LakeError("invalid_project_key")
                        harness_where += " AND repo_owner = ? AND repo_name = ?"
                        params.extend(parts)

                    if table == "harness_sessions":
                        where = f"WHERE {harness_where}"
                    else:
                        where = f"WHERE session_id IN (SELECT session_id FROM harness_sessions WHERE {harness_where})"

                if table == "harness_sessions" and not where:
                    where = "WHERE command IS DISTINCT FROM 'collector'"
                if table == "harness_hosts":
                    where = "WHERE kind IS DISTINCT FROM 'collector'"
                names = ",".join('"' + name + '"' for name, _ in columns)
                rows = pg.execute(
                    f'SELECT {names} FROM "{table}" {where} LIMIT 10001', params
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
            from .serving import IDENTITY_QUERY
            from .task_projection import _hash

            # Exactly open_history's rows, or every binding mismatches.
            identities = pg.execute(IDENTITY_QUERY).fetchall()
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
            from .coverage import certified

            contexts = None
            context_metadata = None
            context_error = None
            if request["operation"] == "contexts":
                try:
                    contexts, context_metadata = certified(
                        pg, request["binding"], "contexts"
                    )
                except LakeError as exc:
                    context_error = exc.code
            # Never expose legacy native rollups as certified usage. Control
            # event usage remains PG-authoritative; certified native facts are
            # computed from the identity-normalized lake view instead.
            con.execute("DELETE FROM session_usage WHERE source='native_agent_events'")
            try:
                native, proof = certified(pg, request["binding"], "native")
                freshness = {name: dict(proof) for name in freshness}
                for row in native["usage"]:
                    con.execute(
                        """INSERT INTO session_usage
                        (session_id,input_tokens,output_tokens,cache_read_tokens,
                         cache_write_tokens,reasoning_tokens,turn_count,exact,source,
                         source_event_count,observed_at)
                        SELECT ?,?,?,?,?,?,?,TRUE,'native_agent_events',?,?
                        WHERE NOT EXISTS(SELECT 1 FROM session_usage WHERE session_id=?)""",
                        [
                            row["session_id"],
                            row["input_tokens"],
                            row["output_tokens"],
                            row["cache_read_tokens"],
                            row["cache_write_tokens"],
                            row["reasoning_tokens"],
                            row["turn_count"],
                            row["source_event_count"],
                            proof["observed_at"],
                            row["session_id"],
                        ],
                    )
            except LakeError as exc:
                for value in freshness.values():
                    if exc.code != "lake_coverage_unverified":
                        value["reason"] = exc.code
            pg.execute("COMMIT")
            return freshness, contexts, context_metadata, context_error
        except BaseException:
            pg.execute("ROLLBACK")
            raise


def run_model(con, request, limits):
    native_freshness, contexts, context_metadata, context_error = _control_snapshot(
        con, request
    )
    operation = request["operation"]
    # These are ephemeral query relations, never catalog tables or cached files.
    if operation == "cockpit":
        # Do not bind a global raw-event relation simply to discover dates.
        # The rollup is small, partitioned by UTC day, and maintained in the
        # same snapshot as outbox exports.
        con.execute(
            "CREATE TEMP VIEW agent_event_partitions AS SELECT DISTINCT date FROM lake.activity_daily"
        )
    else:
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
    binding = {
        **request["binding"],
        "snapshot": con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0],
    }
    for value in native_freshness.values():
        value.setdefault("generation", None)
        value["coverage_binding"] = binding
    if operation == "contexts":
        if context_error:
            payload = {
                "status": "unavailable",
                "analytics_backend": "ducklake",
                "analytics_epoch": binding["epoch"],
                "reason": context_error,
            }
        else:
            from .coverage import context_result

            payload = context_result(contexts["contexts"], options)
    elif operation == "coverage":
        payload = {}
    elif operation == "fleet":
        cursor = con.execute(
            """SELECT s.session_id,s.host_id AS agent_id,
            s.repo_owner,s.repo_name,s.branch,s.started_at,
            coalesce(s.last_activity,s.updated_at) AS last_event_at,s.status,s.harness
            FROM harness_sessions s JOIN harness_hosts h ON s.host_id=h.host_id
            WHERE h.retired_at IS NULL AND h.kind IS DISTINCT FROM 'collector'
            AND s.command IS DISTINCT FROM 'collector' AND s.status IN ('running','awaiting')
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

    if operation in ("fleet", "contexts", "coverage"):
        payload["metadata"] = dict(native_freshness)
        if operation == "contexts" and context_metadata is not None:
            payload["metadata"]["contexts"] = context_metadata
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
