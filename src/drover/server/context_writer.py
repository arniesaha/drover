"""Opt-in hub producer for resumable containers from derived memory only."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from drover.context_containers import DEFAULT_CONTEXT_REDACTION_POLICY
from drover.server.advisory.redaction import redact_content
from drover.server.control_store import is_postgres_control_store
from drover.server.db import control_plane_connection, open_duckdb_connection
from drover.server.memory_store import MemoryRepository

log = logging.getLogger(__name__)
COLUMNS = "context_id container_type label source_harness confidence evidence last_touched_at next_action open_loop session_ids task_ids repo_owner repo_name branch summary_md redaction_policy created_at updated_at".split()
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:api[_-]?key|password|passwd|secret|credential|token)\s*:\s*)[^\s,;]+"
)


def redact_context_text(value, *, limit=4096):
    if not value:
        return None
    if len(str(value)) > 16_384:
        # Do not feed unbounded source text into credential regexes, and never
        # truncate raw secrets into a prefix that could evade matching.
        return "[Content omitted: source exceeds redaction budget]"
    # Redact before truncation, so a truncated private key cannot evade matching.
    return _ASSIGNED_SECRET.sub(r"\1[REDACTED]", redact_content(str(value)))[:limit]


def _key(kind, identity):
    return f"ctx-{kind}-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _identity(value):
    """Reject credential-shaped identities rather than break their evidence links."""
    if value is not None and redact_context_text(value, limit=len(value)) != value:
        raise ValueError("unsafe context source identity")
    return value


def build_containers(summaries, briefs, *, harnesses=None):
    """Deterministic, conservative classification; no raw prompts or transcripts."""
    harnesses = harnesses or {}
    result = []
    for kind, sources in (("session", summaries), ("project", briefs)):
        for source in sources:
            is_session = kind == "session"
            identity = _identity(
                source.session_id if is_session else source.project_key
            )
            project = _identity(source.project_key)
            owner, sep, name = (project or "").partition("/")
            code = bool(owner and sep and name and "/" not in name)
            stamp = _utc(source.generated_at)
            if stamp is None:
                raise ValueError("context source requires a generation timestamp")
            linked = (
                [identity]
                if is_session
                else [s.session_id for s in summaries if s.project_key == project]
            )
            if (
                not is_session
                and source.source_session_id
                and source.source_session_id not in linked
            ):
                linked.append(_identity(source.source_session_id))
            content = redact_context_text(
                source.summary_md if is_session else source.brief_md
            )
            label = (
                (content or "Session activity").splitlines()[0].strip("# ")
                if is_session
                else project
            )
            result.append(
                dict(
                    zip(
                        COLUMNS,
                        [
                            _key(kind, identity),
                            "code_project" if code else "general_activity",
                            redact_context_text(label, limit=160),
                            _identity(harnesses.get(identity)) if is_session else None,
                            0.95 if code else 0.5,
                            json.dumps(
                                {
                                    "classification_basis": (
                                        "explicit_project_key"
                                        if code
                                        else "unclassified_session"
                                    ),
                                    "source": f"drover://{kind}/{identity}",
                                    "generated_at": stamp.isoformat(),
                                },
                                sort_keys=True,
                            ),
                            _utc(
                                source.ended_at
                                if is_session
                                else source.last_activity_at
                            )
                            or stamp,
                            redact_context_text(source.next_steps_md),
                            redact_context_text("\n".join(source.open_questions)),
                            sorted(linked),
                            (
                                [_identity(source.task_id)]
                                if is_session and source.task_id
                                else []
                            ),
                            owner if code else None,
                            name if code else None,
                            None,
                            content,
                            DEFAULT_CONTEXT_REDACTION_POLICY,
                            stamp,
                            stamp,
                        ],
                    )
                )
            )
    return result


def _apply_policy(row, previous):
    if previous is None:
        return row
    policy = previous.get("redaction_policy")
    # Unknown/restrictive policies are owned by their author, never weakened.
    if policy not in {DEFAULT_CONTEXT_REDACTION_POLICY, "metadata-only"}:
        return previous
    row = {**row, "redaction_policy": policy, "created_at": previous["created_at"]}
    if policy == "metadata-only":
        row.update(
            label=(
                "Session activity"
                if row["context_id"].startswith("ctx-session-")
                else "Project activity"
            ),
            summary_md=None,
            next_action=None,
            open_loop=None,
        )
    if previous["updated_at"] is not None and _utc(row["updated_at"]) < _utc(
        previous["updated_at"]
    ):
        return previous
    return row


def _same(left, right):
    def encode(row):
        return json.dumps(row, default=lambda v: _utc(v).isoformat(), sort_keys=True)

    # Lake rows have serialized dates; compare normalized dates consistently.
    def normalize(row):
        return {
            k: _utc(v) if k in {"created_at", "updated_at", "last_touched_at"} else v
            for k, v in row.items()
        }

    return encode(normalize(left)) == encode(normalize(right))


class ContextContainerWriter:
    """Bounded, idempotent source snapshot and upserts, restricted to the hub."""

    def __init__(self, path: Path, *, max_containers=1000):
        if not is_postgres_control_store(path):
            raise ValueError(
                "context container production requires the hub PostgreSQL store"
            )
        if type(max_containers) is not int or max_containers < 1:
            raise ValueError("max_containers must be a positive integer")
        self.path = Path(path)
        self.max_containers = max_containers

    def _sources(self):
        repo = MemoryRepository(self.path)
        summaries = repo.recent_summaries(limit=self.max_containers + 1)
        briefs = repo.briefs(limit=self.max_containers + 1)
        if len(summaries) + len(briefs) > self.max_containers:
            raise ValueError(
                "context source exceeds max_containers; no partial snapshot written"
            )
        with control_plane_connection(self.path) as pg:
            rows = pg.execute(
                "SELECT session_id, native_session_id, harness FROM harness_sessions WHERE session_id = ANY(?) OR native_session_id = ANY(?)",
                [[s.session_id for s in summaries]] * 2,
            ).fetchall()
        harnesses = {}
        for session_id, native_id, harness in rows:
            harnesses[session_id] = harness
            if native_id:
                harnesses[native_id] = harness
        return build_containers(summaries, briefs, harnesses=harnesses)

    def run_once(self, *, apply=False):
        from drover.server.lake.serving import selected_config

        proposed = self._sources()
        if selected_config(self.path).backend == "ducklake":
            return self._lake(proposed, apply=apply)
        if not self.path.is_file():
            if apply:
                raise ValueError(
                    "initialize the analytical context table before applying"
                )
            _, stats = self._changes(proposed, {})
            return {"mode": "dry-run", **stats, "applied": 0}
        con = open_duckdb_connection(self.path)
        try:
            # A single transaction prevents policy changes racing the upsert.
            con.execute("BEGIN")
            existing = {
                row[0]: dict(zip(COLUMNS, row))
                for row in con.execute(
                    f"SELECT {', '.join(COLUMNS)} FROM context_containers LIMIT {self.max_containers + 1}"
                ).fetchall()
            }
            if (
                len(set(existing) | {r["context_id"] for r in proposed})
                > self.max_containers
            ):
                raise ValueError(
                    "context snapshot exceeds max_containers; no partial snapshot written"
                )
            changes, stats = self._changes(proposed, existing)
            if apply:
                for row in changes:
                    con.execute(
                        f"""INSERT INTO context_containers ({', '.join(COLUMNS)})
                        VALUES ({', '.join('?' for _ in COLUMNS)})
                        ON CONFLICT (context_id) DO UPDATE SET
                        {', '.join(f'{c}=EXCLUDED.{c}' for c in COLUMNS if c not in {'context_id', 'created_at'})}""",
                        [row[c] for c in COLUMNS],
                    )
                con.execute("COMMIT")
            else:
                con.execute("ROLLBACK")
            return {
                "mode": "apply" if apply else "dry-run",
                **stats,
                "applied": len(changes) if apply else 0,
            }
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _changes(self, proposed, existing):
        changes = []
        stats = {"sources": len(proposed), "created": 0, "updated": 0, "unchanged": 0}
        for row in proposed:
            previous = existing.get(row["context_id"])
            row = _apply_policy(row, previous)
            if previous is not None and _same(row, previous):
                stats["unchanged"] += 1
            else:
                stats["created" if previous is None else "updated"] += 1
                changes.append(row)
        return changes, stats

    def _lake(self, proposed, *, apply):
        from drover.server.lake import coverage
        from drover.server.lake.runtime import LakeError
        from drover.server.lake.serving import _SELECTION_LOCK
        from drover.server.lake.task_projection import projection_fence

        # Use the authoritative source, never a legacy table or serving fallback.
        with _SELECTION_LOCK, projection_fence(self.path) as pg:
            source = None
            if pg.execute("SELECT to_regclass('lake_coverage_sources')").fetchone()[0]:
                try:
                    source = coverage._source(pg, "contexts")
                except LakeError as exc:
                    if exc.code != "analytics_coverage_absent":
                        raise
            existing = {r["context_id"]: r for r in source[4]} if source else {}
            changes, stats = self._changes(proposed, existing)
            rows = {**existing, **{r["context_id"]: r for r in changes}}
            payload = coverage._contexts(list(rows.values()))
            if apply:
                now = datetime.now(timezone.utc)
                # Refresh the observation even when data is unchanged; retain
                # source generation times in rows and the content hash watermark.
                coverage.publish_source(
                    self.path,
                    "contexts",
                    payload,
                    publisher="context-container-worker",
                    watermark=coverage._hash(payload),
                    observed_at=now,
                    _pg=pg,
                )
                coverage.certify(self.path, "contexts", _pg=pg)
            return {
                "mode": "apply" if apply else "dry-run",
                **stats,
                "applied": len(changes) if apply else 0,
            }


class ContextContainerWorker:
    def __init__(self, path, *, interval_seconds=60):
        self.writer = ContextContainerWriter(path)
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="context-containers", daemon=True
        )
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.writer.run_once(apply=True)
            except Exception as exc:
                # Error content could contain a source identity; log only type.
                log.warning("context container pass failed (%s)", type(exc).__name__)
            self._stop.wait(self.interval_seconds)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
