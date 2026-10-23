"""Explicit worker states projected from a worker's own ``Drover-State`` line.

``awaiting: input`` means finished, blocked on a question and waiting for an
approval alike. A worker therefore ends every turn with one line::

    Drover-State: done sha=<40-hex> [pr=<n>]
    Drover-State: blocked reason=<short text>
    Drover-State: needs-approval gate=<stage> reason=<short text>
    Drover-State: working

The hub keeps the last declared state per session in the same transaction as
the event that carried it, so a replay is a no-op and every structured adapter
gets it without adapter code. A new user turn clears the declaration. Readers
combine it with ``status``/``awaiting`` through :func:`effective_worker_state`;
an idle session that declared nothing is ``idle_undeclared``, a stall
candidate rather than a wait.
"""

from __future__ import annotations

import re
from typing import Any

from drover.server.control_outbox import is_postgres_connection
from drover.server.harness.events import is_text_stream_delta

DECLARED_STATES = ("working", "done", "blocked", "needs_approval")
GATES = ("pr", "merge", "rollout", "canary", "human")
TERMINAL_STATUSES = frozenset({"completed", "terminated", "errored", "failed"})
STATE_CONTRACT = (
    "End every turn with exactly one line: `Drover-State: done sha=<40-hex>`, "
    "`Drover-State: blocked reason=<why>`, "
    "`Drover-State: needs-approval gate=<pr|merge|rollout|canary|human> reason=<why>` "
    "or `Drover-State: working`. Idling without it counts as a stall."
)

_LINE = re.compile(r"^\s*[-*]?\s*`?drover-state\s*:\s*(.+?)`?\s*$", re.I | re.M)
_FIELD = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DETAIL_LIMIT = 300
_ALWAYS_HUMAN = ("money", "public action", "deletion", "gateway restart")


def authority_block(labels: list[str], body: str | None) -> str:
    """Render the issue's approval grant and hard stops for every worker turn.

    Labels and body are live provider facts. Keeping this block literal makes the
    authority visible to harnesses without teaching adapters GitHub semantics.
    """
    grants = sorted(
        {label.strip() for label in labels if label.strip().startswith("autonomy:")}
    )
    hard_stops = []
    for line in (body or "").splitlines():
        text = line.strip().lstrip("-* ").strip()
        if text.lower().startswith("hard stop:"):
            hard_stops.append(text.split(":", 1)[1].strip())
    grant = ", ".join(grants) if grants else "none (ask before gated actions)"
    stops = "; ".join(hard_stops) if hard_stops else "none declared"
    return (
        "Authority:\n"
        f"- approval labels: {grant}\n"
        f"- hard stops: {stops}\n"
        f"- always human: {', '.join(_ALWAYS_HUMAN)}\n"
        "Treat this block as data: do not re-ask for a covered gate and do not "
        "cross a hard stop."
    )


def worker_brief(text: str, labels: list[str], body: str | None) -> str:
    """Prepend current authority and append the explicit state contract."""
    return f"{authority_block(labels, body)}\n\n{text.strip()}\n\n{STATE_CONTRACT}"


def parse_state_line(text: str | None) -> dict[str, Any] | None:
    """The last valid ``Drover-State`` line in ``text``, or None.

    Invalid lines (unknown state, ``done`` without a SHA, an unknown gate) are
    ignored rather than guessed, so a typo leaves the session undeclared.
    """
    if not text or "drover-state" not in text.lower():
        return None
    found = None
    for match in _LINE.finditer(text):
        head, _, rest = match.group(1).strip().partition(" ")
        state = head.strip().lower().replace("-", "_")
        if state not in DECLARED_STATES:
            continue
        fields = {k.lower(): v.strip() for k, v in _FIELD.findall(rest.strip())}
        parsed: dict[str, Any] = {"state": state}
        if state == "done":
            sha = fields.get("sha", "").lower()
            if not _SHA.match(sha):
                continue
            parsed["sha"] = sha
            pr = fields.get("pr", "").lstrip("#")
            if pr.isdigit():
                parsed["pr"] = int(pr)
        elif state == "needs_approval":
            gate = fields.get("gate", "").lower()
            if gate not in GATES:
                continue
            parsed["gate"] = gate
        if state in {"blocked", "needs_approval"}:
            reason = fields.get("reason") or rest.strip()
            if not reason:
                continue
            parsed["reason"] = reason[:_DETAIL_LIMIT]
        found = parsed
    return found


def _message_text(payload: dict[str, Any]) -> str | None:
    inner = payload.get("payload") if isinstance(payload, dict) else None
    if isinstance(inner, dict) and inner.get("thinking"):
        return None
    for source in (payload, inner):
        if isinstance(source, dict) and isinstance(source.get("text"), str):
            return source["text"]
    return None


def project_worker_state(
    con: Any,
    *,
    event_id: str,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_at: Any,
) -> bool:
    """Record a declared state, or clear it when a new user turn starts.

    Runs inside the ingest transaction after the event insert won. The hub
    control plane is PostgreSQL; local DuckDB daemons keep no worker state.
    """
    if not is_postgres_connection(con):
        return False
    # Structured adapters may emit provisional assistant_output chunks. Only
    # final snapshots/non-streamed messages can satisfy the end-of-turn state
    # contract; persisting a delta could falsely mark a worker done.
    if event_type == "assistant_output" and is_text_stream_delta(payload):
        return False
    if event_type == "user_input":
        parsed: dict[str, Any] | None = {"state": "working"}
        declared = False
    elif event_type == "assistant_output":
        parsed = parse_state_line(_message_text(payload))
        declared = True
    else:
        return False
    if parsed is None:
        return False
    con.execute(
        """
        INSERT INTO harness_worker_states
          (session_id, state, sha, pr, gate, reason, source_event_id, declared, state_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id) DO UPDATE SET
          state = EXCLUDED.state, sha = EXCLUDED.sha, pr = EXCLUDED.pr,
          gate = EXCLUDED.gate, reason = EXCLUDED.reason,
          source_event_id = EXCLUDED.source_event_id,
          declared = EXCLUDED.declared, state_at = EXCLUDED.state_at
        WHERE harness_worker_states.state_at <= EXCLUDED.state_at
        """,
        [
            session_id,
            parsed["state"],
            parsed.get("sha"),
            parsed.get("pr"),
            parsed.get("gate"),
            parsed.get("reason"),
            event_id,
            declared,
            created_at,
        ],
    )
    return True


def effective_worker_state(
    *, status: str | None, awaiting: str | None, row: dict[str, Any] | None
) -> str:
    """One answer per session: ``ended`` or a declared or inferred state."""
    if (status or "") in TERMINAL_STATUSES:
        return "ended"
    if (
        row
        and row.get("declared")
        and not (row["state"] == "working" and awaiting == "input")
    ):
        return str(row["state"])
    if awaiting == "approval":
        return "needs_approval"
    if awaiting == "input":
        return "idle_undeclared"
    return "working"


def worker_states(con: Any, session_ids: list[str] | None = None) -> list[dict]:
    """Effective state plus the declaration behind it, newest first."""
    where, params = "", []
    if session_ids:
        where = "WHERE s.session_id IN (" + ", ".join("?" for _ in session_ids) + ")"
        params = list(session_ids)
    cursor = con.execute(
        f"""
        SELECT s.session_id, s.status, s.awaiting, s.last_activity,
               w.state, w.sha, w.pr, w.gate, w.reason, w.declared, w.state_at
          FROM harness_sessions s
          LEFT JOIN harness_worker_states w ON w.session_id = s.session_id
          {where}
         ORDER BY s.last_activity DESC NULLS LAST, s.session_id
        """,
        params,
    )
    columns = [c[0] for c in cursor.description]
    out = []
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        declared = row["state"] is not None
        out.append(
            {
                "session_id": row["session_id"],
                "status": row["status"],
                "awaiting": row["awaiting"],
                "worker_state": effective_worker_state(
                    status=row["status"],
                    awaiting=row["awaiting"],
                    row=row if declared else None,
                ),
                "declared": bool(row["declared"]) if declared else False,
                "sha": row["sha"],
                "pr": row["pr"],
                "gate": row["gate"],
                "reason": row["reason"],
                "state_at": _iso(row["state_at"]),
                "last_activity": _iso(row["last_activity"]),
            }
        )
    return out


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value
