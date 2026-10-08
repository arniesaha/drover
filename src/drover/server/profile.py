"""PostgreSQL portable profile policy shared by HTTP, MCP and import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drover.server.control_store import postgres_control_store

TIERS = ("general", "trusted", "private")
LAYERS = ("user", "work", "decision")
SCOPES = ("first_turn", "full", *LAYERS)
BUNDLE_TOKENS = 1500


@dataclass(frozen=True)
class ProfileActor:
    agent_id: str = "anonymous"
    tier: str = "general"
    user: bool = False


def resolve_actor(path: Path, credential_id: str | None = None) -> ProfileActor:
    """Only a transport-verified credential ID may be passed here."""
    if not credential_id:
        return ProfileActor()
    with postgres_control_store(path).connection() as con:
        row = con.execute(
            "SELECT agent_id, tier FROM profile_agents WHERE credential_id = ?",
            [credential_id],
        ).fetchone()
    return ProfileActor(*row) if row else ProfileActor(credential_id)


def http_actor(path, auth, headers):
    import hmac

    from drover.server.web.auth import bearer_credential

    authorization = headers.get("Authorization", "") or ""
    if (
        auth.enabled
        and auth.legacy_token_enabled
        and auth.api_token
        and hmac.compare_digest(authorization, f"Bearer {auth.api_token}")
    ):
        return ProfileActor("operator", "private", True)
    credential = bearer_credential(auth, headers) if auth.enabled else None
    return resolve_actor(path, credential.id if credential else None)


def token_upper_bound(text: str) -> int:
    """One token per UTF-8 byte safely bounds byte-based model tokenizers."""
    return len(text.encode("utf-8"))


def _contexts(path):
    import duckdb

    from drover.server.lake.serving import selected_config
    from drover.server.mcp.tools import drover_recent_contexts

    # The existing legacy reader opens read/write; never create a database here.
    if selected_config(path).backend == "legacy" and not Path(path).exists():
        return [], "unavailable"
    try:
        result = drover_recent_contexts(duckdb_path=path, limit=1000)
    except (duckdb.Error, OSError):
        return [], "unavailable"
    return result.get("contexts", []), result.get("status", "ok")


def read_profile(path: Path, scope="first_turn", *, actor=None, now=None):
    if scope not in SCOPES:
        raise ValueError("unsupported profile scope")
    actor = actor or ProfileActor()
    if actor.tier not in TIERS:
        raise ValueError("invalid reader tier")
    now = now or datetime.now(timezone.utc)
    allowed = TIERS[: TIERS.index(actor.tier) + 1]
    candidates = []
    scoped = scope in LAYERS
    placeholders = ",".join("?" for _ in allowed)
    where = """status = 'active' AND (expires_at IS NULL OR expires_at > ?)
        AND (layer <> 'work' OR updated_at >= ?)
        AND (layer <> 'decision' OR updated_at >= ?)"""
    params = [now, now - timedelta(days=14), now - timedelta(days=30)]
    if scoped:
        where += " AND layer = ?"
        params.append(scope)
    with postgres_control_store(path).connection() as con:
        withheld = con.execute(
            f"SELECT count(*) FROM profile_items WHERE {where} "
            f"AND tier NOT IN ({placeholders})",
            [*params, *allowed],
        ).fetchone()[0]
        cursor = con.execute(
            f"SELECT item_id, layer, kind, body, updated_at FROM profile_items "
            f"WHERE {where} AND tier IN ({placeholders}) "
            "ORDER BY CASE WHEN kind = 'rule' THEN 0 WHEN layer = 'user' THEN 1 "
            "WHEN layer = 'work' THEN 2 ELSE 3 END, updated_at DESC, item_id LIMIT 1001",
            [*params, *allowed],
        )
        rows = cursor.fetchall()
        source_truncated = len(rows) > 1000
        for item_id, layer, kind, body, updated in rows[:1000]:
            candidates.append((item_id, layer, kind, body, updated))
        if not scoped or scope == "work":
            rows = con.execute(
                "SELECT project_key, brief_md, next_steps_md, last_activity_at "
                "FROM project_briefs WHERE last_activity_at >= ? "
                "ORDER BY last_activity_at DESC, project_key LIMIT 1001",
                [now - timedelta(days=14)],
            ).fetchall()
            source_truncated |= len(rows) > 1000
            for key, body, next_step, updated in rows[:1000]:
                candidates.append(
                    (
                        f"brief:{key}",
                        "work",
                        "thread",
                        f"{key}: {body}\nNext: {next_step or ''}",
                        updated,
                    )
                )
    context_status = "not_requested"
    if not scoped or scope == "work":
        contexts, context_status = _contexts(path)
        source_truncated |= len(contexts) >= 1000
        for context in contexts:
            updated = context.get("last_touched_at")
            if isinstance(updated, str):
                updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if updated is None:
                continue  # Unknown activity is not fresh work.
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated < now - timedelta(days=14):
                continue
            # The old table has no access tier. Only redacted code context is public.
            tier = (
                "general"
                if (
                    context.get("container_type") == "code_project"
                    and context.get("redaction_policy") == "session-summary-redacted"
                )
                else "private"
            )
            if tier not in allowed:
                withheld += 1
                continue
            body = "\n".join(
                str(context.get(k) or "")
                for k in ("label", "summary_md", "next_action", "open_loop")
            )
            candidates.append(
                (f"context:{context['context_id']}", "work", "thread", body, updated)
            )

    def order(row):
        item_id, layer, kind, _, updated = row
        priority = (
            0
            if kind == "rule"
            else 1 if layer == "user" else 2 if layer == "work" else 3
        )
        return priority, -updated.timestamp(), item_id

    candidates.sort(key=order)
    header = f"# Drover profile\n"
    footer = f"\nWithheld: {withheld} items.\n"
    # Reserve the truncation notice before selecting complete entries.
    notice = "\nSome items omitted to fit the budget.\n"
    budget = BUNDLE_TOKENS - token_upper_bound(header + footer + notice)
    entries = []
    truncated = source_truncated
    for _, layer, kind, body, _ in candidates:
        entry = f"\n[{layer}/{kind}] {body}\n"
        size = token_upper_bound(entry)
        if size > budget:
            truncated = True
            continue
        entries.append(entry)
        budget -= size
    bundle = header + "".join(entries) + (notice if truncated else "") + footer
    return {
        "bundle": bundle,
        "token_upper_bound": token_upper_bound(bundle),
        "token_budget": BUNDLE_TOKENS,
        "withheld_count": withheld,
        "truncated": truncated,
        "context_status": context_status,
    }
