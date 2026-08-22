"""The Phase 0 scratch ledger: one row per loop turn.

Its own DuckDB file, never the analytical store. Two reasons, both current:
`drover.duckdb` is already hitting a 2GB ceiling on the hub (drover#246), and
decision D3 -- whether control-plane state stays behind the single DuckDB
writer or moves to a transactional store -- is still open. A loop that writes a
row per iteration into the analytical instance would prejudge that question by
adding to the problem it is meant to help answer.

Explicitly disposable. The schema here is a sketch to be contradicted by what
Phase 0 actually needs; the free-text `note` exists to capture what the fixed
columns could not, which is the main input to the production model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_iterations (
  goal_id            VARCHAR NOT NULL,
  ordinal            INTEGER NOT NULL,
  started_at         TIMESTAMPTZ NOT NULL,
  ended_at           TIMESTAMPTZ,
  host_id            VARCHAR,
  session_id         VARCHAR,
  -- Which harness ran this iteration. Added by decision R4: without it the
  -- brief corpus cannot say whether a rendered brief was harness-specific,
  -- which is the question a portable profile has to answer before it can be
  -- specified rather than guessed at.
  harness            VARCHAR,
  -- The exact text carried across the context boundary. The other half of R4,
  -- and the direct evidence for the design's open question about how much of
  -- the brief should be rendered claims versus letting the agent read the
  -- working tree.
  brief_rendered     VARCHAR,
  verification_cmd   VARCHAR,
  verification_exit  INTEGER,
  verification_tail  VARCHAR,
  claimed_outcome    VARCHAR,
  diff_ref           VARCHAR,
  prompt_tokens      BIGINT,
  completion_tokens  BIGINT,
  cost_usd           DOUBLE,
  note               VARCHAR,
  PRIMARY KEY (goal_id, ordinal)
);
"""


@dataclass
class Iteration:
    """One turn of the loop, as recorded."""

    goal_id: str
    ordinal: int
    started_at: datetime
    ended_at: Optional[datetime] = None
    host_id: Optional[str] = None
    session_id: Optional[str] = None
    harness: Optional[str] = None
    brief_rendered: Optional[str] = None
    verification_cmd: Optional[str] = None
    verification_exit: Optional[int] = None
    verification_tail: Optional[str] = None
    claimed_outcome: Optional[str] = None
    diff_ref: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    note: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class ScratchLedger:
    """Append-only record of what each iteration did, and what checked it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(self.path))
        try:
            con.execute(_SCHEMA)
        finally:
            con.close()

    def append(self, iteration: Iteration) -> None:
        note = iteration.note
        if iteration.extra:
            # Fixed columns will be wrong; that is the point of Phase 0. Keep
            # what they could not hold rather than discarding it.
            note = json.dumps({"note": note, **iteration.extra}, default=str)
        con = duckdb.connect(str(self.path))
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO goal_iterations VALUES
                  (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    iteration.goal_id,
                    iteration.ordinal,
                    iteration.started_at,
                    iteration.ended_at,
                    iteration.host_id,
                    iteration.session_id,
                    iteration.harness,
                    iteration.brief_rendered,
                    iteration.verification_cmd,
                    iteration.verification_exit,
                    iteration.verification_tail,
                    iteration.claimed_outcome,
                    iteration.diff_ref,
                    iteration.prompt_tokens,
                    iteration.completion_tokens,
                    iteration.cost_usd,
                    note,
                ],
            )
        finally:
            con.close()

    def history(self, goal_id: str) -> list[dict[str, Any]]:
        con = duckdb.connect(str(self.path))
        try:
            cursor = con.execute(
                "SELECT * FROM goal_iterations WHERE goal_id = ? ORDER BY ordinal",
                [goal_id],
            )
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            con.close()

    def next_ordinal(self, goal_id: str) -> int:
        con = duckdb.connect(str(self.path))
        try:
            row = con.execute(
                "SELECT max(ordinal) FROM goal_iterations WHERE goal_id = ?",
                [goal_id],
            ).fetchone()
        finally:
            con.close()
        return int(row[0]) + 1 if row and row[0] is not None else 1

    def tokens_used(self, goal_id: str) -> int:
        """Tokens this goal has spent so far, for the driver's hard cap.

        Tokens rather than dollars, because tokens are observed and dollars
        would be estimated. Drover does not price tokens anywhere: `cost_usd`
        on `spans` is carried from whatever produced the span, never computed,
        so a USD cap on harness sessions could only be invented. See drover#17.
        """
        con = duckdb.connect(str(self.path))
        try:
            row = con.execute(
                "SELECT coalesce(sum(coalesce(prompt_tokens, 0) "
                "+ coalesce(completion_tokens, 0)), 0) FROM goal_iterations "
                "WHERE goal_id = ?",
                [goal_id],
            ).fetchone()
        finally:
            con.close()
        return int(row[0]) if row and row[0] is not None else 0

    def spend_usd(self, goal_id: str) -> float:
        """Kept, and deliberately unused by the cap.

        `cost_usd` stays a column so a producer that does supply cost has
        somewhere to put it. Nothing populates it today, which is exactly why
        the cap does not read it: a cap reading a column nobody writes is not a
        cap, and the driver ran with one for its first three live runs.
        """
        con = duckdb.connect(str(self.path))
        try:
            row = con.execute(
                "SELECT coalesce(sum(cost_usd), 0.0) FROM goal_iterations "
                "WHERE goal_id = ?",
                [goal_id],
            ).fetchone()
        finally:
            con.close()
        return float(row[0]) if row and row[0] is not None else 0.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
