"""Explicit authoritative source revisions and immutable certified generations.

Provisioning/publication are operator or producer APIs, never serving side effects.
A native manifest is supplied independently; reading the lake cannot declare its
own publication complete. Certification derives usage under the bound identities.
"""

import hashlib
import json
import math
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from uuid import uuid4

from drover.context_containers import normalize_context_type
from drover.server.db import control_plane_connection

from .query_process import query
from .rebuild import EVENT_SCHEMA, row_hash_expression
from .runtime import LakeError
from .serving import _SELECTION_LOCK, lake_spec, open_history, selected_config
from .task_projection import (
    PROJECTION_LOCK,
    _capture,
    _encoded,
    _hash,
    _lake_fence,
    projection_fence,
)

MAX_ROWS = 1000
MAX_BYTES = 1024 * 1024
MAX_AGE_SECONDS = 300
CONTEXT_COLUMNS = "context_id container_type label source_harness confidence evidence last_touched_at next_action open_loop session_ids task_ids repo_owner repo_name branch summary_md redaction_policy created_at updated_at".split()
TOKEN_COLUMNS = "input_tokens output_tokens cache_read_tokens cache_write_tokens reasoning_tokens".split()


def bounded(value):
    if len(_encoded(value).encode()) > MAX_BYTES:
        raise LakeError("analytics_byte_limit_exceeded")
    if isinstance(value, list) and len(value) > MAX_ROWS:
        raise LakeError("analytics_row_limit_exceeded")
    return value


def provision_coverage(path):
    """Explicit staging/provisioning only; runtime never creates proof tables."""
    with _SELECTION_LOCK, projection_fence(path) as pg:
        _capture(path)
        pg.execute("BEGIN")
        try:
            pg.execute("""CREATE TABLE IF NOT EXISTS lake_coverage_sources (
                revision UUID PRIMARY KEY, source_seq BIGSERIAL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('contexts','native')),
                publisher TEXT NOT NULL, watermark TEXT NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")
            pg.execute("""CREATE TABLE IF NOT EXISTS lake_coverage_generations (
                generation UUID PRIMARY KEY, receipt_seq BIGSERIAL UNIQUE,
                kind TEXT NOT NULL, revision UUID NOT NULL REFERENCES lake_coverage_sources(revision),
                source_sha256 TEXT NOT NULL, binding TEXT NOT NULL,
                payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                certified_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            pg.execute(
                "CREATE INDEX IF NOT EXISTS lake_coverage_source_head ON lake_coverage_sources(kind,source_seq DESC)"
            )
            pg.execute(
                "CREATE INDEX IF NOT EXISTS lake_coverage_generation_head ON lake_coverage_generations(kind,receipt_seq DESC)"
            )
            pg.execute("COMMIT")
        except BaseException:
            pg.execute("ROLLBACK")
            raise


def _contexts(rows):
    if not isinstance(rows, list):
        raise LakeError("analytics_context_source_invalid")
    bounded(rows)
    result = []
    keys = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - set(CONTEXT_COLUMNS):
            raise LakeError("analytics_context_source_invalid")
        item = {name: row.get(name) for name in CONTEXT_COLUMNS}
        key = item["context_id"]
        if not isinstance(key, str) or not key or key in keys:
            raise LakeError("analytics_context_source_invalid")
        keys.add(key)
        item["container_type"] = normalize_context_type(item["container_type"])
        for name in set(CONTEXT_COLUMNS) - {"confidence", "session_ids", "task_ids"}:
            if item[name] is not None and not isinstance(item[name], (str, datetime)):
                raise LakeError("analytics_context_source_invalid")
        confidence = item["confidence"]
        if confidence is not None and (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise LakeError("analytics_context_source_invalid")
        for name in ("session_ids", "task_ids"):
            values = item[name] or []
            if (
                not isinstance(values, list)
                or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)
            ):
                raise LakeError("analytics_context_source_invalid")
            bounded(values)
            item[name] = values
        for name in ("last_touched_at", "created_at", "updated_at"):
            if item[name] is not None:
                if isinstance(item[name], datetime):
                    item[name] = item[name].isoformat()
                stamp = datetime.fromisoformat(item[name])
                if stamp.tzinfo is None:
                    raise LakeError("analytics_context_source_invalid")
        result.append(item)
    return bounded(result)


def publish_source(path, kind, payload, *, publisher, watermark, observed_at, _pg=None):
    """Publish a full authoritative revision, never import legacy state.

    Native payload is a canonical typed-row inventory produced independently of
    serving: version, rows, sha256. Watermarks are publisher-local labels.
    Internal ``_pg`` reuses the producer's held projection fence so reading
    policies and publishing their replacement cannot race another publisher.
    """
    if kind not in ("contexts", "native") or any(
        not isinstance(v, str) or not v or len(v.encode()) > 4096
        for v in (publisher, watermark)
    ):
        raise LakeError("analytics_coverage_source_invalid")
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise LakeError("analytics_coverage_source_invalid")
    if kind == "contexts":
        payload = _contexts(payload)
    elif (
        not isinstance(payload, dict)
        or set(payload) != {"version", "rows", "sha256"}
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or type(payload["rows"]) is not int
        or payload["rows"] < 0
        or not isinstance(payload["sha256"], str)
        or len(payload["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in payload["sha256"])
    ):
        raise LakeError("analytics_coverage_source_invalid")
    bounded(payload)
    with (
        _SELECTION_LOCK,
        nullcontext(_pg) if _pg is not None else projection_fence(path) as pg,
    ):
        _capture(path)
        revision = str(uuid4())
        pg.execute(
            "INSERT INTO lake_coverage_sources(revision,kind,publisher,watermark,observed_at,payload,payload_sha256) VALUES (?,?,?,?,?,?,?)",
            [
                revision,
                kind,
                publisher,
                watermark,
                observed_at,
                _encoded(payload),
                _hash(payload),
            ],
        )
        return revision


def _source(pg, kind):
    row = pg.execute(
        """SELECT revision,
        CASE WHEN octet_length(publisher)<=4096 THEN publisher END,
        CASE WHEN octet_length(watermark)<=4096 THEN watermark END,observed_at,
        CASE WHEN octet_length(payload)<=1048576 THEN payload END,payload_sha256
        FROM lake_coverage_sources WHERE kind=? ORDER BY source_seq DESC LIMIT 1""",
        [kind],
    ).fetchone()
    if not row:
        raise LakeError("analytics_coverage_absent")
    revision, publisher, watermark, observed_at, encoded, sha = row
    if (
        publisher is None
        or watermark is None
        or encoded is None
        or len(encoded.encode()) > MAX_BYTES
    ):
        raise LakeError("analytics_byte_limit_exceeded")
    try:
        payload = json.loads(encoded)
        if _hash(payload) != sha:
            raise ValueError()
        if kind == "contexts":
            _contexts(payload)
    except (ValueError, TypeError, KeyError):
        raise LakeError("analytics_coverage_incomplete") from None
    return str(revision), publisher, watermark, observed_at, payload, sha


def _before_receipt(pg):
    """Fault-injection boundary before the immutable completion receipt."""


def certify(path, kind, *, _pg=None):
    if kind not in ("contexts", "native"):
        raise LakeError("analytics_coverage_source_invalid")
    with (
        _SELECTION_LOCK,
        nullcontext(_pg) if _pg is not None else projection_fence(path) as pg,
        _lake_fence(path, None) as fence,
    ):
        binding, _ = _capture(path)
        source = _source(pg, kind)
        revision, _, _, _, payload, source_sha = source
        if kind == "native":
            config = selected_config(path)
            with open_history(path) as history:
                result = query(
                    lake_spec(config),
                    "SELECT 1",
                    serving={
                        "verification_sha256": config.verification_sha256,
                        "identities": history.identities,
                        "coverage_build": True,
                    },
                )
            if (
                result["snapshot"] != binding["snapshot"]
                or result["inventory"] != payload
            ):
                raise LakeError("analytics_native_publication_incomplete")
            payload = {
                "inventory": payload,
                "usage": bounded(result["usage"]),
                "usage_events": result["usage_events"],
            }
            _usage(payload)
        else:
            payload = {"contexts": _contexts(payload)}
        bounded(payload)
        if _capture(path)[0] != binding or _source(pg, kind) != source:
            raise LakeError("analytics_coverage_changed")
        pg.execute("BEGIN")
        try:
            _before_receipt(pg)
            fence.check()
            held = pg.execute(
                """SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                AND pid=pg_backend_pid() AND classid=? AND objid=? AND objsubid=1
                AND mode='ExclusiveLock' AND granted)""",
                [PROJECTION_LOCK >> 32, PROJECTION_LOCK & 0xFFFFFFFF],
            ).fetchone()[0]
            if not held or _capture(path)[0] != binding or _source(pg, kind) != source:
                raise LakeError("analytics_coverage_changed")
            generation = str(uuid4())
            pg.execute(
                "INSERT INTO lake_coverage_generations(generation,kind,revision,source_sha256,binding,payload,payload_sha256) VALUES (?,?,?,?,?,?,?)",
                [
                    generation,
                    kind,
                    revision,
                    source_sha,
                    _encoded(binding),
                    _encoded(payload),
                    _hash(payload),
                ],
            )
            pg.execute("COMMIT")
        except BaseException:
            pg.execute("ROLLBACK")
            raise
        return generation


def certified(pg, binding, kind):
    """Caller owns a repeatable-read snapshot. Newest receipt only, no fallback."""
    if (
        pg.execute("SELECT to_regclass('lake_coverage_generations')").fetchone()[0]
        is None
    ):
        raise LakeError(
            "analytics_context_projection_unavailable"
            if kind == "contexts"
            else "lake_coverage_unverified"
        )
    source = _source(pg, kind)
    revision, publisher, watermark, observed_at, _, source_sha = source
    row = pg.execute(
        """SELECT generation,revision,source_sha256,
        CASE WHEN octet_length(binding)<=1048576 THEN binding END,
        CASE WHEN octet_length(payload)<=1048576 THEN payload END,payload_sha256,certified_at
        FROM lake_coverage_generations WHERE kind=? ORDER BY receipt_seq DESC LIMIT 1""",
        [kind],
    ).fetchone()
    if not row:
        raise LakeError("analytics_coverage_absent")
    generation, rev, sha, encoded_binding, encoded, checksum, certified_at = row
    if encoded_binding is None or encoded is None or len(encoded.encode()) > MAX_BYTES:
        raise LakeError("analytics_byte_limit_exceeded")
    try:
        payload = json.loads(encoded)
        if _hash(payload) != checksum:
            raise ValueError()
        if (
            json.loads(encoded_binding) != binding
            or str(rev) != revision
            or sha != source_sha
        ):
            raise LakeError("analytics_coverage_stale")
        bounded(payload)
        if kind == "contexts":
            _contexts(payload["contexts"])
            if _hash(payload["contexts"]) != source_sha:
                raise ValueError()
        else:
            _usage(payload)
            if _hash(payload["inventory"]) != source_sha:
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise LakeError("analytics_coverage_incomplete") from None
    now = datetime.now(timezone.utc)
    if any(
        not 0 <= (now - stamp).total_seconds() <= MAX_AGE_SECONDS
        for stamp in (observed_at, certified_at)
    ):
        raise LakeError("analytics_coverage_stale")
    metadata = {
        "freshness": "fresh",
        "observed_at": observed_at.isoformat(),
        "generation": str(generation),
        "coverage_binding": binding,
        "publisher": publisher,
        "source_revision": revision,
        "watermark": watermark,
        "freshness_basis": "registered_source_revision",
        "certified_at": certified_at.isoformat(),
    }
    if kind == "native":
        metadata.update(
            covered_events=payload["inventory"]["rows"],
            usage_events=payload["usage_events"],
            usage_sessions=len(payload["usage"]),
            usage_basis="typed_canonical_native_events",
            publication_scope="canonical_native_agent_events",
        )
    return payload, metadata


def native_inventory(con, relation):
    """Version-1 typed canonical inventory; producer invokes on independent input."""
    digest = hashlib.sha256()
    count = 0
    con.execute("SET TimeZone='UTC'")
    fields = ",".join(
        f'CAST("{name}" AS {kind}) AS "{name}"' for name, kind in EVENT_SCHEMA.items()
    )
    cursor = con.execute(
        f"SELECT {row_hash_expression(EVENT_SCHEMA)} AS leaf FROM (SELECT {fields} FROM {relation}) typed ORDER BY leaf"
    )
    while rows := cursor.fetchmany(1000):
        for (leaf,) in rows:
            digest.update(leaf.encode("ascii") + b"\n")
            count += 1
    return {"version": 1, "rows": count, "sha256": digest.hexdigest()}


def build_in_child(con):
    con.execute(
        """CREATE TEMP VIEW coverage_native AS SELECT * FROM lake.agent_events
        WHERE coalesce(dedup_key_source,'')<>'outbox' AND
        coalesce(CASE WHEN json_valid(raw_data) THEN json_extract_string(raw_data,'$.source') END,'')<>'control'"""
    )
    inventory = native_inventory(con, "coverage_native")
    valid = " OR ".join(f"{name} IS NOT NULL" for name in TOKEN_COLUMNS)
    invalid = " OR ".join(f"{name}<0" for name in TOKEN_COLUMNS)
    if con.execute(
        f"SELECT count(*) FROM agent_events WHERE source='native' AND ({valid}) AND (session_id IS NULL OR {invalid})"
    ).fetchone()[0]:
        raise LakeError("analytics_native_usage_incomplete")
    sums = ",".join(f"sum({name})::BIGINT AS {name}" for name in TOKEN_COLUMNS)
    cursor = con.execute(
        f"SELECT session_id,{sums},count(*) AS turn_count,count(*) AS source_event_count FROM agent_events WHERE source='native' AND ({valid}) GROUP BY session_id ORDER BY session_id LIMIT {MAX_ROWS+1}"
    )
    names = [field[0] for field in cursor.description]
    usage = bounded([dict(zip(names, row)) for row in cursor.fetchall()])
    usage_events = con.execute(
        f"SELECT count(*) FROM agent_events WHERE source='native' AND ({valid})"
    ).fetchone()[0]
    return {
        "inventory": inventory,
        "usage": usage,
        "usage_events": usage_events,
        "snapshot": con.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0],
        "rows": [],
    }


def context_result(rows, options):
    """Bounded existing context API over a certified full source snapshot."""
    mode = options.get("mode", "recent")

    def stamp(value):
        return (
            datetime.fromisoformat(value).astimezone(timezone.utc)
            if value
            else datetime.min.replace(tzinfo=timezone.utc)
        )

    selected = []
    for row in rows:
        if options.get("container_type") and row[
            "container_type"
        ] != normalize_context_type(options["container_type"]):
            continue
        if (
            options.get("source_harness")
            and row["source_harness"] != options["source_harness"]
        ):
            continue
        if options.get("project_key") is not None:
            if f'{row["repo_owner"]}/{row["repo_name"]}' != options["project_key"]:
                continue
        if mode == "brief":
            if options.get("context_id"):
                if row["context_id"] != options["context_id"]:
                    continue
            elif row["label"] != options.get("label"):
                continue
        if mode == "loops" and not (row["next_action"] or row["open_loop"]):
            continue
        selected.append(row)
    selected.sort(
        key=lambda row: (
            stamp(row["last_touched_at"]),
            stamp(row["updated_at"]),
            row["context_id"],
        ),
        reverse=True,
    )
    limit = options.get("limit", 1000)
    if mode == "brief":
        return {"context": selected[0] if selected else None}
    return {
        "open_loops" if mode == "loops" else "contexts": selected[:limit],
        "limit": limit,
    }


def heads(path):
    """Small source/receipt head token; no payload or unbounded map is fetched."""
    with control_plane_connection(path) as pg:
        pg.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            present = pg.execute(
                "SELECT to_regclass('lake_coverage_generations')"
            ).fetchone()[0]
            result = {}
            if present:
                for kind in ("contexts", "native"):
                    source = pg.execute(
                        "SELECT revision FROM lake_coverage_sources WHERE kind=? ORDER BY source_seq DESC LIMIT 1",
                        [kind],
                    ).fetchone()
                    generation = pg.execute(
                        "SELECT generation FROM lake_coverage_generations WHERE kind=? ORDER BY receipt_seq DESC LIMIT 1",
                        [kind],
                    ).fetchone()
                    result[kind] = [
                        str(source[0]) if source else None,
                        str(generation[0]) if generation else None,
                    ]
            pg.execute("COMMIT")
            return result
        except BaseException:
            pg.execute("ROLLBACK")
            raise


@contextmanager
def read_fence(path):
    """Fence composite context + PG summary reads against renewal/publication."""
    with _SELECTION_LOCK:
        if selected_config(path).backend == "legacy":
            yield
            return
        binding, _ = _capture(path)
        before = heads(path)
        yield
        if _capture(path)[0] != binding or heads(path) != before:
            raise LakeError("analytics_coverage_changed")


def _usage(payload):
    rows = bounded(payload["usage"])
    if not isinstance(rows, list):
        raise LakeError("analytics_coverage_incomplete")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "session_id",
            "turn_count",
            "source_event_count",
            *TOKEN_COLUMNS,
        }:
            raise LakeError("analytics_coverage_incomplete")
        sid = row["session_id"]
        if not isinstance(sid, str) or not sid or sid in seen:
            raise LakeError("analytics_coverage_incomplete")
        seen.add(sid)
        for name in TOKEN_COLUMNS:
            if row[name] is not None and (type(row[name]) is not int or row[name] < 0):
                raise LakeError("analytics_coverage_incomplete")
        if (
            all(row[name] is None for name in TOKEN_COLUMNS)
            or type(row["turn_count"]) is not int
            or row["turn_count"] <= 0
            or row["source_event_count"] != row["turn_count"]
        ):
            raise LakeError("analytics_coverage_incomplete")
    total = sum(row["source_event_count"] for row in rows)
    if (
        type(payload["usage_events"]) is not int
        or total != payload["usage_events"]
        or not 0 <= total <= payload["inventory"]["rows"]
    ):
        raise LakeError("analytics_coverage_incomplete")
