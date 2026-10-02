"""Derived session memory in the PostgreSQL control store (#480).

One repository for everything Drover derives from a session:

* ``session_memory`` -- one row per session with two phases. A live session
  carries an incremental *recap*; once the summarizer finishes it carries the
  *final summary* as well and its phase is ``final``. :meth:`latest` is the
  single reader for "what do we currently know about this session": the final
  summary when there is one, the live recap otherwise.
* ``project_briefs`` -- the summary-of-summaries per ``owner/name``.
* ``session_embeddings`` -- pgvector ``vector(768)`` of the final summary.
  Search is exact cosine distance (no ANN index): at 10k x 768 it measured
  ~4 ms median, and ANN recall was poor at low ``ef_search``. Model and
  dimension are checked on every write and every query; a mismatch is an
  explicit :class:`EmbeddingMismatch`, never a silent empty result.

None of this lives in DuckDB any more. Analytical callers that used to join
``session_summaries`` fetch the rows they need from here by session id.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from drover.server.db import control_plane_connection
from drover.server.ledger import require_memory_store
from drover.server.postgres_schema import EMBEDDING_DIM, vector_extension_schema

# --------------------------------------------------------------------------- #
# Rows                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    summary_md: str
    next_steps_md: Optional[str] = None
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    project_key: Optional[str] = None
    ended_at: Optional[datetime] = None
    files_touched: Sequence[str] = field(default_factory=tuple)
    tools_used: Mapping[str, int] = field(default_factory=dict)
    open_questions: Sequence[str] = field(default_factory=tuple)
    last_user_prompt: Optional[str] = None
    last_assistant: Optional[str] = None
    status: str = "completed"
    source_version: Optional[str] = None
    generator_model: Optional[str] = None
    generated_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        """The legacy ``session_summaries`` row shape MCP/recall callers expect."""
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "project_key": self.project_key,
            "ended_at": self.ended_at,
            "summary_md": self.summary_md,
            "next_steps_md": self.next_steps_md,
            "files_touched": list(self.files_touched),
            "tools_used": dict(self.tools_used),
            "open_questions": list(self.open_questions),
            "last_user_prompt": self.last_user_prompt,
            "last_assistant": self.last_assistant,
            "status": self.status,
            "source_version": self.source_version,
            "generator_model": self.generator_model,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class LiveRecap:
    """The latest recap for one live session."""

    session_id: str
    text: str
    source_seq: int
    generated_at: datetime
    generator_model: Optional[str]


@dataclass(frozen=True)
class SessionMemory:
    """What Drover currently knows about a session, in either phase."""

    session_id: str
    phase: str  # 'live' | 'final'
    summary: Optional[SessionSummary]
    recap: Optional[LiveRecap]
    updated_at: datetime

    @property
    def text(self) -> str:
        if self.summary is not None:
            return self.summary.summary_md
        return self.recap.text if self.recap is not None else ""


@dataclass(frozen=True)
class ProjectBrief:
    project_key: str
    repo_owner: str
    repo_name: str
    brief_md: str
    recent_themes_md: Optional[str] = None
    key_files: Sequence[str] = field(default_factory=tuple)
    open_questions: Sequence[str] = field(default_factory=tuple)
    next_steps_md: Optional[str] = None
    session_count: int = 0
    last_activity_at: Optional[datetime] = None
    source_session_id: Optional[str] = None
    source_version: Optional[str] = None
    generator_model: Optional[str] = None
    generated_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_key": self.project_key,
            "repo_owner": self.repo_owner,
            "repo_name": self.repo_name,
            "brief_md": self.brief_md,
            "recent_themes_md": self.recent_themes_md,
            "key_files": list(self.key_files),
            "open_questions": list(self.open_questions),
            "next_steps_md": self.next_steps_md,
            "session_count": self.session_count,
            "last_activity_at": self.last_activity_at,
            "source_session_id": self.source_session_id,
            "source_version": self.source_version,
            "generator_model": self.generator_model,
            "generated_at": self.generated_at,
        }


# Match Phase 2's canonical-summary contract using authoritative PG identity.
# An explicit native alias is hidden only once its harness summary exists.
CANONICAL_MEMORY = """NOT EXISTS (
    SELECT 1 FROM harness_sessions identity
    JOIN session_memory canonical ON canonical.session_id = identity.session_id
    WHERE identity.native_session_id = session_memory.session_id
      AND canonical.session_id <> session_memory.session_id
      AND canonical.phase = 'final'
)"""


_SUMMARY_COLUMNS = (
    "session_id, summary_md, next_steps_md, task_id, agent_id, project_key, ended_at, "
    "files_touched, tools_used, open_questions, last_user_prompt, last_assistant, "
    "summary_status, summary_source_version, summary_model, summary_generated_at"
)
_RECAP_COLUMNS = "recap_text, recap_source_seq, recap_generated_at, recap_model"
_BRIEF_COLUMNS = (
    "project_key, repo_owner, repo_name, brief_md, recent_themes_md, key_files, "
    "open_questions, next_steps_md, session_count, last_activity_at, "
    "source_session_id, source_version, generator_model, generated_at"
)


def _summary_from_row(row: Sequence[Any]) -> SessionSummary:
    tools = row[8]
    if isinstance(tools, str):
        tools = json.loads(tools)
    return SessionSummary(
        session_id=row[0],
        summary_md=row[1],
        next_steps_md=row[2],
        task_id=row[3],
        agent_id=row[4],
        project_key=row[5],
        ended_at=row[6],
        files_touched=tuple(row[7] or ()),
        tools_used=dict(tools or {}),
        open_questions=tuple(row[9] or ()),
        last_user_prompt=row[10],
        last_assistant=row[11],
        status=row[12] or "completed",
        source_version=row[13],
        generator_model=row[14],
        generated_at=row[15],
    )


def _recap_from_row(session_id: str, row: Sequence[Any]) -> Optional[LiveRecap]:
    if row[0] is None:
        return None
    return LiveRecap(
        session_id=session_id,
        text=row[0],
        source_seq=int(row[1]),
        generated_at=row[2],
        generator_model=row[3],
    )


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


# --------------------------------------------------------------------------- #
# Session memory and briefs                                                   #
# --------------------------------------------------------------------------- #


class MemoryRepository:
    """Reads and writes ``session_memory`` and ``project_briefs``.

    Write methods take the caller's connection so a derived row and the job
    transition that produced it commit in one transaction.
    """

    def __init__(self, store_path: str | Path) -> None:
        require_memory_store(store_path)
        self.store_path = Path(store_path)

    def connection(self, timeout: float | None = None):
        return control_plane_connection(self.store_path, timeout=timeout)

    # -- writes ------------------------------------------------------------- #

    @staticmethod
    def put_summary(con, summary: SessionSummary) -> None:
        """Write the final phase. Keeps any live recap already on the row."""
        con.execute(
            """INSERT INTO session_memory
                 (session_id, phase, summary_md, next_steps_md, task_id, agent_id,
                  project_key, ended_at, files_touched, tools_used, open_questions,
                  last_user_prompt, last_assistant, summary_status,
                  summary_source_version, summary_model, summary_generated_at, updated_at)
               VALUES (?, 'final', ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?, ?, ?,
                       COALESCE(?, now()), now())
               ON CONFLICT (session_id) DO UPDATE SET
                 phase = 'final',
                 summary_md = excluded.summary_md,
                 next_steps_md = excluded.next_steps_md,
                 task_id = excluded.task_id,
                 agent_id = excluded.agent_id,
                 project_key = excluded.project_key,
                 ended_at = excluded.ended_at,
                 files_touched = excluded.files_touched,
                 tools_used = excluded.tools_used,
                 open_questions = excluded.open_questions,
                 last_user_prompt = excluded.last_user_prompt,
                 last_assistant = excluded.last_assistant,
                 summary_status = excluded.summary_status,
                 summary_source_version = excluded.summary_source_version,
                 summary_model = excluded.summary_model,
                 summary_generated_at = excluded.summary_generated_at,
                 updated_at = now()""",
            [
                summary.session_id,
                summary.summary_md,
                summary.next_steps_md,
                summary.task_id,
                summary.agent_id,
                summary.project_key,
                summary.ended_at,
                list(summary.files_touched),
                json.dumps(dict(summary.tools_used), sort_keys=True),
                list(summary.open_questions),
                summary.last_user_prompt,
                summary.last_assistant,
                summary.status,
                summary.source_version,
                summary.generator_model,
                summary.generated_at,
            ],
        )

    @staticmethod
    def put_recap(
        con, session_id: str, text: str, source_seq: int, model: Optional[str]
    ) -> bool:
        """Advance the live phase. An older ``source_seq`` never overwrites a newer one."""
        row = con.execute(
            """INSERT INTO session_memory
                 (session_id, phase, recap_text, recap_source_seq, recap_model,
                  recap_generated_at, updated_at)
               VALUES (?, 'live', ?, ?, ?, now(), now())
               ON CONFLICT (session_id) DO UPDATE SET
                 recap_text = excluded.recap_text,
                 recap_source_seq = excluded.recap_source_seq,
                 recap_model = excluded.recap_model,
                 recap_generated_at = excluded.recap_generated_at,
                 updated_at = now()
               WHERE session_memory.recap_source_seq IS NULL
                  OR session_memory.recap_source_seq <= excluded.recap_source_seq
               RETURNING session_id""",
            [session_id, text, int(source_seq), model],
        ).fetchone()
        return row is not None

    @staticmethod
    def put_brief(con, brief: ProjectBrief) -> None:
        con.execute(
            f"""INSERT INTO project_briefs ({_BRIEF_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, now()))
                ON CONFLICT (project_key) DO UPDATE SET
                  repo_owner = excluded.repo_owner, repo_name = excluded.repo_name,
                  brief_md = excluded.brief_md, recent_themes_md = excluded.recent_themes_md,
                  key_files = excluded.key_files, open_questions = excluded.open_questions,
                  next_steps_md = excluded.next_steps_md,
                  session_count = excluded.session_count,
                  last_activity_at = excluded.last_activity_at,
                  source_session_id = excluded.source_session_id,
                  source_version = excluded.source_version,
                  generator_model = excluded.generator_model,
                  generated_at = excluded.generated_at""",
            [
                brief.project_key,
                brief.repo_owner,
                brief.repo_name,
                brief.brief_md,
                brief.recent_themes_md,
                list(brief.key_files),
                list(brief.open_questions),
                brief.next_steps_md,
                int(brief.session_count or 0),
                brief.last_activity_at,
                brief.source_session_id,
                brief.source_version,
                brief.generator_model,
                brief.generated_at,
            ],
        )

    # -- reads -------------------------------------------------------------- #

    def summaries(self, session_ids: Iterable[str]) -> dict[str, SessionSummary]:
        """Final-phase summaries for the given sessions (missing ids omitted)."""
        ids = sorted({str(s) for s in session_ids if s})
        if not ids:
            return {}
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT {_SUMMARY_COLUMNS} FROM session_memory
                     WHERE phase = 'final' AND session_id = ANY(?)""",
                [ids],
            ).fetchall()
        return {row[0]: _summary_from_row(row) for row in rows}

    def summary(self, session_id: str) -> Optional[SessionSummary]:
        return self.summaries([session_id]).get(session_id)

    def recent_summaries(
        self,
        *,
        limit: int = 20,
        project_key: Optional[str] = None,
        task_id: Optional[str] = None,
        session_ids: Optional[Iterable[str]] = None,
        since: Optional[datetime] = None,
    ) -> list[SessionSummary]:
        """Final summaries, newest ``ended_at`` first, optionally scoped."""
        clauses = ["phase = 'final'", CANONICAL_MEMORY]
        params: list[Any] = []
        if project_key is not None:
            clauses.append("project_key = ?")
            params.append(project_key)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if session_ids is not None:
            ids = sorted({str(s) for s in session_ids if s})
            if not ids:
                return []
            clauses.append("session_id = ANY(?)")
            params.append(ids)
        if since is not None:
            clauses.append("ended_at >= ?")
            params.append(since)
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT {_SUMMARY_COLUMNS} FROM session_memory
                     WHERE {' AND '.join(clauses)}
                     ORDER BY ended_at DESC NULLS LAST, session_id
                     LIMIT ?""",
                [*params, max(1, int(limit))],
            ).fetchall()
        return [_summary_from_row(row) for row in rows]

    def search_summaries(self, query: str, *, limit: int = 20) -> list[SessionSummary]:
        """Case-insensitive substring match over summary and next steps."""
        pattern = (
            "%"
            + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT {_SUMMARY_COLUMNS} FROM session_memory
                     WHERE phase = 'final' AND {CANONICAL_MEMORY}
                       AND (summary_md ILIKE ? OR next_steps_md ILIKE ?)
                     ORDER BY ended_at DESC NULLS LAST LIMIT ?""",
                [pattern, pattern, max(1, int(limit))],
            ).fetchall()
        return [_summary_from_row(row) for row in rows]

    def latest(self, session_ids: Iterable[str]) -> dict[str, SessionMemory]:
        """The single "latest memory" reader: final summary, else live recap."""
        ids = sorted({str(s) for s in session_ids if s})
        if not ids:
            return {}
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT phase, updated_at, {_SUMMARY_COLUMNS}, {_RECAP_COLUMNS}
                      FROM session_memory WHERE session_id = ANY(?)""",
                [ids],
            ).fetchall()
        out: dict[str, SessionMemory] = {}
        for row in rows:
            phase, updated_at, summary_row, recap_row = (
                row[0],
                row[1],
                row[2:18],
                row[18:],
            )
            session_id = summary_row[0]
            out[session_id] = SessionMemory(
                session_id=session_id,
                phase=phase,
                summary=_summary_from_row(summary_row) if phase == "final" else None,
                recap=_recap_from_row(session_id, recap_row),
                updated_at=updated_at,
            )
        return out

    def live_recaps(self, session_ids: Iterable[str]) -> dict[str, LiveRecap]:
        ids = sorted({str(s) for s in session_ids if s})
        if not ids:
            return {}
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT session_id, {_RECAP_COLUMNS} FROM session_memory
                     WHERE recap_text IS NOT NULL AND session_id = ANY(?)""",
                [ids],
            ).fetchall()
        return {row[0]: _recap_from_row(row[0], row[1:]) for row in rows}  # type: ignore[misc]

    def brief(self, project_key: str) -> Optional[ProjectBrief]:
        with self.connection() as con:
            row = con.execute(
                f"SELECT {_BRIEF_COLUMNS} FROM project_briefs WHERE project_key = ?",
                [project_key],
            ).fetchone()
        return (
            ProjectBrief(*row[:5], tuple(row[5] or ()), tuple(row[6] or ()), *row[7:])
            if row
            else None
        )

    def briefs(self, *, limit: int = 20) -> list[ProjectBrief]:
        with self.connection() as con:
            rows = con.execute(
                f"""SELECT {_BRIEF_COLUMNS} FROM project_briefs
                     ORDER BY generated_at DESC LIMIT ?""",
                [max(1, int(limit))],
            ).fetchall()
        return [
            ProjectBrief(*row[:5], tuple(row[5] or ()), tuple(row[6] or ()), *row[7:])
            for row in rows
        ]

    def counts(self, *, con=None) -> dict[str, int]:
        """Row counts for audits: summaries, recaps, briefs, ready bundles."""
        if con is None:
            with self.connection() as own:
                return self.counts(con=own)
        row = con.execute("""SELECT count(*) FILTER (WHERE phase = 'final'),
                      count(*) FILTER (WHERE recap_text IS NOT NULL),
                      count(*) FILTER (WHERE phase = 'final'
                        AND NULLIF(trim(COALESCE(summary_md, '')), '') IS NOT NULL
                        AND NULLIF(trim(COALESCE(next_steps_md, '')), '') IS NOT NULL)
                 FROM session_memory""").fetchone()
        briefs = con.execute("SELECT count(*) FROM project_briefs").fetchone()[0]
        return {
            "session_summaries": int(row[0] or 0),
            "live_recaps": int(row[1] or 0),
            "bundle_ready_summaries": int(row[2] or 0),
            "project_briefs": int(briefs or 0),
        }


# --------------------------------------------------------------------------- #
# Embeddings                                                                  #
# --------------------------------------------------------------------------- #


class VectorStoreUnavailable(RuntimeError):
    """pgvector is not installed/enabled in the control store."""


class EmbeddingMismatch(ValueError):
    """A vector's model or dimension does not match the configured embedding space."""


VECTOR_UNAVAILABLE_MESSAGE = (
    "pgvector is not installed in the PostgreSQL control store; session "
    "embeddings and semantic recall are unavailable. Install the 'vector' "
    "extension on the server (e.g. `brew install pgvector` for Homebrew "
    "PostgreSQL 17) and restart drover-server to finish the schema migration."
)


def vector_status(con) -> tuple[bool, str]:
    """(ready, detail) for the embedding table on one control-store connection."""
    schema = vector_extension_schema(con)
    if schema is None:
        return False, VECTOR_UNAVAILABLE_MESSAGE
    table = con.execute("SELECT to_regclass('session_embeddings')").fetchone()
    if table is None or table[0] is None:
        return False, (
            "pgvector is installed but session_embeddings has not been created; "
            "restart drover-server so the control-store bootstrap can finish"
        )
    return True, f"pgvector ready in schema {schema}"


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


@dataclass(frozen=True)
class EmbeddingHit:
    session_id: str
    similarity: float
    model: str


class EmbeddingStore:
    """Session embeddings in one embedding space (model + dimension)."""

    def __init__(
        self, store_path: str | Path, *, model: str, dim: int = EMBEDDING_DIM
    ) -> None:
        require_memory_store(store_path)
        if dim != EMBEDDING_DIM:
            raise EmbeddingMismatch(
                f"session_embeddings is vector({EMBEDDING_DIM}); "
                f"embedding model {model!r} is configured for {dim} dimensions"
            )
        if not model:
            raise EmbeddingMismatch("an embedding model name is required")
        self.store_path = Path(store_path)
        self.model = model
        self.dim = dim

    def connection(self, timeout: float | None = None):
        return control_plane_connection(self.store_path, timeout=timeout)

    def _vector_schema(self, con) -> str:
        ready, detail = vector_status(con)
        if not ready:
            raise VectorStoreUnavailable(detail)
        schema = vector_extension_schema(con)
        assert schema is not None
        return schema

    def status(self) -> tuple[bool, str]:
        with self.connection() as con:
            return vector_status(con)

    def _validate(self, vector: Sequence[float], model: str) -> None:
        if model != self.model:
            raise EmbeddingMismatch(
                f"embedding model {model!r} does not match the configured model {self.model!r}"
            )
        if not isinstance(vector, (list, tuple)):
            raise EmbeddingMismatch("embedding must be a numeric sequence")
        if len(vector) != self.dim:
            raise EmbeddingMismatch(
                f"embedding has {len(vector)} dimensions; {self.model!r} space is {self.dim}"
            )
        try:
            values = [float(v) for v in vector]
        except (TypeError, ValueError) as exc:
            raise EmbeddingMismatch("embedding contains a non-numeric value") from exc
        if not all(math.isfinite(v) for v in values):
            raise EmbeddingMismatch("embedding contains a non-finite value")
        if not any(values):
            raise EmbeddingMismatch("zero vector has no cosine similarity")

    def put(
        self,
        con,
        session_id: str,
        vector: Sequence[float],
        *,
        model: str,
        source_version: str = "",
    ) -> None:
        """Upsert one session vector on the caller's connection/transaction."""
        self._validate(vector, model)
        schema = self._vector_schema(con).replace('"', '""')
        con.execute(
            f"""INSERT INTO session_embeddings
                  (session_id, embedding, model, dim, source_version, embedded_at)
                VALUES (?, ?::"{schema}".vector, ?, ?, ?, now())
                ON CONFLICT (session_id) DO UPDATE SET
                  embedding = excluded.embedding, model = excluded.model,
                  dim = excluded.dim, source_version = excluded.source_version,
                  embedded_at = now()""",
            [
                session_id,
                _vector_literal(vector),
                model,
                len(vector),
                source_version or "",
            ],
        )

    def search(
        self,
        query: Sequence[float],
        *,
        model: Optional[str] = None,
        limit: int = 10,
        session_ids: Optional[Iterable[str]] = None,
    ) -> list[EmbeddingHit]:
        """Exact cosine search within this store's embedding space."""
        self._validate(query, model or self.model)
        with self.connection() as con:
            schema = self._vector_schema(con).replace('"', '""')
            clauses = [
                "e.model = ?",
                "e.source_version = COALESCE(m.summary_source_version, '')",
                "m.phase = 'final'",
                CANONICAL_MEMORY.replace("session_memory.session_id", "m.session_id"),
            ]
            params: list[Any] = [_vector_literal(query), self.model]
            if session_ids is not None:
                ids = sorted({str(s) for s in session_ids if s})
                if not ids:
                    return []
                clauses.append("e.session_id = ANY(?)")
                params.append(ids)
            rows = con.execute(
                f"""SELECT e.session_id,
                           1 - (e.embedding OPERATOR("{schema}".<=>) q.v) AS similarity,
                           e.model
                      FROM session_embeddings e
                      JOIN session_memory m ON m.session_id = e.session_id,
                           (SELECT ?::"{schema}".vector AS v) q
                     WHERE {' AND '.join(clauses)}
                     ORDER BY e.embedding OPERATOR("{schema}".<=>) q.v
                     LIMIT ?""",
                [*params, max(1, int(limit))],
            ).fetchall()
        return [EmbeddingHit(r[0], float(r[1]), r[2]) for r in rows]

    def count(self, *, con=None) -> dict[str, int]:
        """Embeddings in this space and in any other (stale) model."""
        if con is None:
            with self.connection() as own:
                return self.count(con=own)
        if not vector_status(con)[0]:
            return {"embedded": 0, "other_model": 0}
        row = con.execute(
            """SELECT count(*) FILTER (WHERE model = ?), count(*) FILTER (WHERE model <> ?)
                 FROM session_embeddings""",
            [self.model, self.model],
        ).fetchone()
        return {"embedded": int(row[0] or 0), "other_model": int(row[1] or 0)}

    def embedded_session_ids(self, session_ids: Iterable[str]) -> set[str]:
        ids = sorted({str(s) for s in session_ids if s})
        if not ids:
            return set()
        with self.connection() as con:
            if not vector_status(con)[0]:
                return set()
            rows = con.execute(
                """SELECT e.session_id FROM session_embeddings e
                JOIN session_memory m ON m.session_id=e.session_id
                WHERE e.model = ? AND e.session_id = ANY(?)
                  AND e.source_version = COALESCE(m.summary_source_version, '')
                  AND m.phase = 'final'""",
                [self.model, ids],
            ).fetchall()
        return {r[0] for r in rows}
