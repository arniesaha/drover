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


class ProfileConflict(ValueError):
    """The target changed after the proposal or before its reversal."""


def _text(value, name, limit=65536):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f"{name} must be nonempty text within {limit} bytes")
    return value.strip()


def _change(values):
    if set(values) - {"layer", "kind", "tier", "body", "expires_at"}:
        raise ValueError("unsupported profile change field")
    result = {key: values.get(key) for key in ("layer", "kind", "tier", "body")}
    if result["layer"] not in LAYERS or result["tier"] not in TIERS:
        raise ValueError("invalid profile layer or tier")
    result["kind"] = _text(result["kind"], "kind", 64)
    result["body"] = _text(result["body"], "body")
    expiry = values.get("expires_at")
    if expiry is not None:
        if not isinstance(expiry, str):
            raise ValueError("expires_at must be an ISO timestamp")
        expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("expires_at requires a timezone")
        expiry = expiry.isoformat()
    result["expires_at"] = expiry
    return result


def _lock_item(con, item_id):
    # Covers creation too, where there is not yet a row to SELECT FOR UPDATE.
    con.execute("SELECT pg_advisory_xact_lock(hashtext(?))", [f"profile:{item_id}"])
    row = con.execute(
        "SELECT to_jsonb(i) FROM profile_items i WHERE item_id = ? FOR UPDATE",
        [item_id],
    ).fetchone()
    return row[0] if row else None


def _accept(con, proposal_id, item_id, change, previous, agent, session, actor):
    from psycopg.types.json import Jsonb

    revision = (previous["revision"] if previous else 0) + 1
    provenance = {
        "agent": agent,
        "session": session,
        "time": datetime.now(timezone.utc).isoformat(),
        "actor": actor.agent_id,
        "proposal_id": proposal_id,
    }
    con.execute(
        """INSERT INTO profile_items
           (item_id, layer, kind, tier, body, provenance, expires_at, revision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (item_id) DO UPDATE SET
             layer = EXCLUDED.layer, kind = EXCLUDED.kind, tier = EXCLUDED.tier,
             body = EXCLUDED.body, provenance = EXCLUDED.provenance,
             expires_at = EXCLUDED.expires_at, revision = EXCLUDED.revision,
             status = 'active', updated_at = now()""",
        [
            item_id,
            change["layer"],
            change["kind"],
            change["tier"],
            change["body"],
            Jsonb(provenance),
            change["expires_at"],
            revision,
        ],
    )
    con.execute(
        "UPDATE profile_proposals SET status = 'accepted', actor = ?, acted_at = now(), "
        "before_snapshot = ?, accepted_revision = ?, "
        "change = change || jsonb_build_object('accepted_provenance', ?::jsonb) "
        "WHERE proposal_id = ?",
        [actor.agent_id, Jsonb(previous), revision, Jsonb(provenance), proposal_id],
    )


def propose_profile(
    path, values, *, actor=None, session_id=None, item_id=None, import_key=None
):
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    actor = actor or ProfileActor()
    change = _change(values)
    if session_id is not None:
        session_id = _text(session_id, "session_id", 256)
    supplied_id = item_id is not None
    item_id = _text(item_id, "item_id", 256) if supplied_id else uuid4().hex
    proposal_id = uuid4().hex
    with (
        postgres_control_store(path).connection() as con,
        con._connection.transaction(),
    ):
        if import_key:
            con.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                [f"profile-import:{import_key}"],
            )
            found = con.execute(
                "SELECT proposal_id, status FROM profile_proposals WHERE import_key = ?",
                [import_key],
            ).fetchone()
            if found:
                return {"proposal_id": found[0], "status": found[1], "unchanged": True}
        previous = _lock_item(con, item_id)
        if supplied_id and previous is None:
            raise ValueError("unknown profile item")
        if previous and TIERS.index(previous["tier"]) > TIERS.index(actor.tier):
            raise PermissionError("target item is not accessible")
        con.execute(
            """INSERT INTO profile_proposals
               (proposal_id, item_id, base_revision, change, agent_id, session_id,
                status, actor, import_key)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            [
                proposal_id,
                item_id,
                previous["revision"] if previous else 0,
                Jsonb(change),
                actor.agent_id,
                session_id,
                actor.agent_id,
                import_key,
            ],
        )
        auto = (
            actor.tier == "trusted"
            and change["tier"] != "private"
            and (previous is None or previous["tier"] != "private")
        )
        if auto:
            _accept(
                con,
                proposal_id,
                item_id,
                change,
                previous,
                actor.agent_id,
                session_id,
                actor,
            )
    return {
        "proposal_id": proposal_id,
        "status": "accepted" if auto else "pending",
        "item_id": item_id,
        "unchanged": False,
    }


def act_on_proposal(path, proposal_id, action, *, actor):
    from psycopg.types.json import Jsonb

    if not actor.user:
        raise PermissionError("operator scope required")
    if action not in ("accept", "reject", "revert"):
        raise ValueError("unsupported proposal action")
    with (
        postgres_control_store(path).connection() as con,
        con._connection.transaction(),
    ):
        row = con.execute(
            "SELECT to_jsonb(p) FROM profile_proposals p WHERE proposal_id = ? FOR UPDATE",
            [proposal_id],
        ).fetchone()
        if row is None:
            raise ValueError("unknown profile proposal")
        proposal = row[0]
        expected = "accepted" if action == "revert" else "pending"
        if proposal["status"] != expected:
            raise ProfileConflict("proposal is not in the required state")
        if action == "reject":
            con.execute(
                "UPDATE profile_proposals SET status = 'rejected', actor = ?, "
                "acted_at = now() WHERE proposal_id = ?",
                [actor.agent_id, proposal_id],
            )
            return {"proposal_id": proposal_id, "status": "rejected"}
        previous = _lock_item(con, proposal["item_id"])
        revision = previous["revision"] if previous else 0
        expected_revision = (
            proposal["accepted_revision"]
            if action == "revert"
            else proposal["base_revision"]
        )
        if revision != expected_revision:
            raise ProfileConflict("item has changed; submit a new proposal")
        if action == "accept":
            _accept(
                con,
                proposal_id,
                proposal["item_id"],
                proposal["change"],
                previous,
                proposal["agent_id"],
                proposal["session_id"],
                actor,
            )
            status = "accepted"
        else:
            snapshot = proposal["before_snapshot"]
            if snapshot is None:
                con.execute(
                    "UPDATE profile_items SET status = 'reverted', revision = revision + 1, "
                    "updated_at = now() WHERE item_id = ?",
                    [proposal["item_id"]],
                )
            else:
                provenance = snapshot["provenance"]
                provenance["reversion"] = {
                    "actor": actor.agent_id,
                    "proposal_id": proposal_id,
                    "time": datetime.now(timezone.utc).isoformat(),
                }
                con.execute(
                    """UPDATE profile_items SET layer = ?, kind = ?, tier = ?, body = ?,
                       provenance = ?, status = ?, expires_at = ?, updated_at = ?,
                       revision = revision + 1 WHERE item_id = ?""",
                    [
                        snapshot["layer"],
                        snapshot["kind"],
                        snapshot["tier"],
                        snapshot["body"],
                        Jsonb(provenance),
                        snapshot["status"],
                        snapshot["expires_at"],
                        snapshot["updated_at"],
                        proposal["item_id"],
                    ],
                )
            con.execute(
                "UPDATE profile_proposals SET status = 'reverted', actor = ?, "
                "acted_at = now() WHERE proposal_id = ?",
                [actor.agent_id, proposal_id],
            )
            status = "reverted"
    return {"proposal_id": proposal_id, "status": status}


def register_agent(path, credential_id, agent_id, tier, *, actor):
    if not actor.user:
        raise PermissionError("operator scope required")
    if tier not in TIERS:
        raise ValueError("invalid agent tier")
    agent_id = _text(agent_id, "agent_id", 256)
    credential_id = _text(credential_id, "credential_id", 256)
    with postgres_control_store(path).connection() as con:
        con.execute(
            """INSERT INTO profile_agents (agent_id, credential_id, tier, updated_by)
               VALUES (?, ?, ?, ?) ON CONFLICT (agent_id) DO UPDATE SET
               credential_id = EXCLUDED.credential_id, tier = EXCLUDED.tier,
               updated_by = EXCLUDED.updated_by, updated_at = now()""",
            [agent_id, credential_id, tier, actor.agent_id],
        )
    return {"agent_id": agent_id, "tier": tier}


def list_proposals(path, *, actor, status="pending", limit=25):
    """User review queue. Private proposal bodies never reach agent readers."""
    if not actor.user:
        raise PermissionError("operator scope required")
    if status not in ("pending", "accepted", "rejected", "reverted"):
        raise ValueError("invalid proposal status")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with postgres_control_store(path).connection() as con:
        rows = con.execute(
            "SELECT proposal_id, item_id, change, agent_id, session_id, created_at "
            "FROM profile_proposals WHERE status = ? ORDER BY created_at, proposal_id LIMIT ?",
            [status, limit + 1],
        ).fetchall()
    return {
        "proposals": [
            dict(
                proposal_id=r[0],
                item_id=r[1],
                change=r[2],
                agent_id=r[3],
                session_id=r[4],
                created_at=r[5].isoformat(),
            )
            for r in rows[:limit]
        ],
        "truncated": len(rows) > limit,
    }
