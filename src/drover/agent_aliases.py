"""Canonical `agent_id` mapping shared between write-side parsers and the CLI.

The canonical form (used in `lakehouse.agent_events.agent_id` and now
`lakehouse.spans.agent_id` after parser normalization) is `<host>-<tool>`,
e.g. `nas-claude`, `nas-openclaw`, `macmini-hermes`. AgentWeave emits
`<tool>-<host>`; older v1 instrumentations use single-token names.

`canonicalize` is idempotent: an already-canonical id passes through
unchanged. Call it from both ingest-time parsing and read-time user input
normalization so `drover trace search --agent nix` and `--agent nas-openclaw`
both work.
"""

from typing import Optional


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


AGENT_ALIASES = {
    # AgentWeave proxy attribute style: <tool>-<host>
    "claude-nas": "nas-claude",
    "claude-code-nas": "nas-claude",
    "hermes-macmini": "macmini-hermes",
    "pimono-macmini": "macmini-pimono",
    "claude-macmini": "macmini-claude",
    # Historical AgentWeave ids observed in spans before macmini naming settled.
    "claude-code-mac": "macmini-claude",
    "claude-code-mac-subagent": "macmini-claude",
    "claude-code-mac-session-hook": "macmini-claude",
    "claude-work-macbook": "work-macbook-claude",
    "claude-code-work-macbook": "work-macbook-claude",
    "openclaw-nas": "nas-openclaw",
    "max-macmini": "macmini-pimono",
    # v1 instrumentation: bare agent name
    "max-v1": "macmini-pimono",
    "nix-v1": "nas-openclaw",
    "nix-v1-subagent-v1": "nas-openclaw",
    # Friendly shortcuts a human types from the CLI
    "claude": "nas-claude",
    "nix": "nas-openclaw",
    "openclaw": "nas-openclaw",
    "hermes": "macmini-hermes",
    "jenny": "macmini-hermes",
    "max": "macmini-pimono",
    "pimono": "macmini-pimono",
}


def canonicalize(agent_id: Optional[str]) -> Optional[str]:
    """Return the canonical `<host>-<tool>` form of an agent identifier.

    Unknown values are returned unchanged so non-aliased identifiers
    (e.g. `mux-router`) survive untouched.
    """
    if agent_id is None:
        return None
    return AGENT_ALIASES.get(agent_id, agent_id)


def canonicalize_sql(expr: str) -> str:
    """Return a DuckDB SQL expression that canonicalizes an agent-id column.

    This keeps read-time views/macros on the same alias map as parser ingest and
    CLI input normalization, including historical spans already written with
    AgentWeave-shaped ids.
    """
    cases = "\n".join(
        f"    WHEN {expr} = {_sql_quote(raw)} THEN {_sql_quote(canonical)}"
        for raw, canonical in AGENT_ALIASES.items()
    )
    return f"CASE\n{cases}\n    ELSE {expr}\n  END"
