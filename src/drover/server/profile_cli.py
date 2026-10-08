"""Read-only markdown parsing and explicit portable-profile import."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import click

from drover.server.control_store import postgres_control_store
from drover.server.profile import ProfileActor, act_on_proposal, propose_profile

SENSITIVE_HEADING = re.compile(
    r"\b(?:health|medical|finance|finances|financial|money|job[\s_-]*search|personal|"
    r"tax(?:es)?|income|salary|compensation|address|location|dating|relationships?|"
    r"career|interviews?|offers?|resignation|therapy|pet[\s_-]*health|portfolios?|"
    r"investments?|banking)\b",
    re.IGNORECASE,
)

# Conservative heuristics, not a proof that unmatched text is safe to share.
PII_PATTERNS = {
    "street_address": re.compile(
        r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){1,5}"
        r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|court|ct|"
        r"boulevard|blvd|way|place|pl|terrace|ter|crescent|cres)\b",
        re.I,
    ),
    "ip_address": re.compile(
        r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|"
        r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]{0,39}(?![\w:])",
        re.I,
    ),
    "currency_amount": re.compile(
        r"[$€£¥]\s*\d|\b(?:USD|CAD|EUR|GBP|JPY|AUD)\s*\d|"
        r"\b\d[\d,.]*\s*(?:USD|CAD|EUR|GBP|JPY|AUD|dollars?|euros?|pounds?)\b",
        re.I,
    ),
    "phone_number": re.compile(
        r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?"
        r"\d{3}[ .-]?\d{4}(?!\w)"
    ),
    "email": re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+"),
}


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_markdown(text, *, tier="private"):
    """Section items inherit private classification from every ancestor heading."""
    if tier not in ("general", "trusted", "private"):
        raise ValueError("import tier must be general, trusted or private")
    stack = []
    body = []
    items = []
    fence = None

    def flush():
        content = "\n".join(body).strip()
        if not content:
            return
        headings = [heading for _, heading in stack]
        heading = " / ".join(headings) or "Notes"
        reasons = ["default_private" if tier == "private" else f"explicit_tier:{tier}"]
        reasons.extend(
            f"sensitive_heading:{h}" for h in headings if SENSITIVE_HEADING.search(h)
        )
        reasons.extend(
            f"body_pii:{name}"
            for name, pattern in PII_PATTERNS.items()
            if pattern.search(f"{heading}\n{content}")
        )
        private = len(reasons) > 1
        # Preserve durable preferences; work and decisions get their own freshness.
        layer = (
            "decision"
            if re.search(r"\bdecisions?\b", heading, re.I)
            else (
                "work"
                if re.search(r"\b(?:threads?|active work|projects?)\b", heading, re.I)
                else "user"
            )
        )
        kind = (
            "rule"
            if re.search(r"\b(?:rules?|constraints?)\b", heading, re.I)
            else (
                "thread"
                if layer == "work"
                else "decision" if layer == "decision" else "preference"
            )
        )
        items.append(
            dict(
                layer=layer,
                kind=kind,
                tier="private" if private else tier,
                key=heading,
                tier_reasons=reasons,
                body=f"{heading}\n{content}",
            )
        )

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            chars = marker[1]
            if fence is None:
                fence = (chars[0], len(chars))
            elif chars[0] == fence[0] and len(chars) >= fence[1]:
                fence = None
            body.append(line)
            index += 1
            continue
        match = (
            re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if fence is None
            else None
        )
        setext = (
            fence is None
            and line.strip()
            and index + 1 < len(lines)
            and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", lines[index + 1])
        )
        if match or setext:
            flush()
            body = []
            level = len(match[1]) if match else (1 if "=" in lines[index + 1] else 2)
            title = match[2] if match else line.strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            index += 1 if match else 2
            continue
        body.append(line)
        index += 1
    flush()
    return items


def import_sources(source: Path, *, tier="private"):
    source = Path(source)
    files = sorted(source.rglob("*.md")) if source.is_dir() else [source]
    if not files or len(files) > 100:
        raise ValueError("import requires between 1 and 100 markdown files")
    records = []
    for file in files:
        if (
            not file.is_file()
            or file.suffix.lower() != ".md"
            or file.stat().st_size > 2 * 1024 * 1024
        ):
            raise ValueError("import sources must be markdown files within 2 MiB")
        source_key = _digest(str(file.resolve()))
        occurrences = {}
        for change in parse_markdown(file.read_text(encoding="utf-8"), tier=tier):
            heading = change["body"].split("\n", 1)[0]
            ordinal = occurrences.get(heading, 0)
            occurrences[heading] = ordinal + 1
            slot = _digest(f"{source_key}:{heading}:{ordinal}")
            content = _digest(json.dumps(change, sort_keys=True))
            records.append(
                dict(change=change, slot=slot, import_key=f"{slot}:{content}")
            )
    return records


def import_record(path, record):
    """Idempotent exact imports; changed sections become review proposals."""
    # Serialize same-section imports, including different versions of a section.
    with (
        postgres_control_store(path).connection() as con,
        con._connection.transaction(),
    ):
        con.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            [f"profile-slot:{record['slot']}"],
        )
        existing = con.execute(
            "SELECT proposal_id, status FROM profile_proposals WHERE import_key = ?",
            [record["import_key"]],
        ).fetchone()
        if existing:
            return dict(proposal_id=existing[0], status=existing[1], unchanged=True)
        previous = con.execute(
            "SELECT item_id FROM profile_proposals WHERE split_part(import_key, ':', 1) = ? "
            "AND status = 'accepted' ORDER BY created_at DESC, proposal_id DESC LIMIT 1",
            [record["slot"]],
        ).fetchone()
        proposal = propose_profile(
            path,
            {
                k: v
                for k, v in record["change"].items()
                if k not in ("key", "tier_reasons")
            },
            _classification={
                "key": record["change"]["key"],
                "tier_reasons": record["change"]["tier_reasons"],
            },
            actor=ProfileActor("profile-import", "private"),
            session_id=f"import:{record['slot']}",
            item_id=previous[0] if previous else None,
            import_key=record["import_key"],
            _con=con,
        )
        if previous is None and record["change"]["tier"] != "private":
            act_on_proposal(
                path,
                proposal["proposal_id"],
                "accept",
                actor=ProfileActor("operator", "private", True),
                _con=con,
            )
            proposal["status"] = "accepted"
        return proposal


@click.group()
def profile():
    """Portable profile import and review."""


@profile.command(name="import")
@click.option(
    "--from", "source", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "--tier",
    type=click.Choice(["general", "trusted", "private"]),
    default="private",
    show_default=True,
)
@click.option(
    "--apply",
    is_flag=True,
    help="Write to PostgreSQL. Private and changed sections stay pending.",
)
@click.pass_context
def import_command(ctx, source, tier, apply):
    """Parse markdown without editing sources. Dry-run unless --apply is passed."""
    try:
        records = import_sources(source, tier=tier)
        if apply:
            from drover.server.__main__ import _resolve_config

            cfg = _resolve_config(ctx.obj.get("config_path"))
            results = [import_record(cfg.duckdb_path, record) for record in records]
        else:
            results = [record["change"] for record in records]
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"dry_run": not apply, "items": results}, indent=2))


def _operator_path(ctx):
    from drover.server.__main__ import _resolve_config

    return _resolve_config((ctx.obj or {}).get("config_path")).duckdb_path


@profile.command(name="set-tier")
@click.argument("item_id")
@click.option(
    "--tier", required=True, type=click.Choice(["general", "trusted", "private"])
)
@click.option("--reason", required=True, help="Explanation retained in provenance.")
@click.pass_context
def set_tier_command(ctx, item_id, tier, reason):
    """Promote or demote an accepted item after operator review."""
    from drover.server.profile import set_item_tier

    try:
        result = set_item_tier(
            _operator_path(ctx),
            item_id,
            tier,
            reason=reason,
            actor=ProfileActor("operator", "private", True),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@profile.command(name="review")
@click.argument("proposal_id")
@click.argument("action", type=click.Choice(["accept", "reject", "revert"]))
@click.pass_context
def review_command(ctx, proposal_id, action):
    """Apply an explicit operator decision to a proposal."""
    try:
        result = act_on_proposal(
            _operator_path(ctx),
            proposal_id,
            action,
            actor=ProfileActor("operator", "private", True),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@profile.group(name="agents")
def agents():
    """Operator management of HTTP profile credentials."""


@agents.command(name="issue")
@click.argument("agent_id")
@click.option(
    "--tier",
    type=click.Choice(["general", "trusted"]),
    default="general",
    show_default=True,
)
@click.pass_context
def issue_command(ctx, agent_id, tier):
    """Issue and register a bearer token, printed once. Store it securely."""
    from drover.server.profile import issue_agent_credential

    try:
        result = issue_agent_credential(
            _operator_path(ctx),
            agent_id,
            tier,
            actor=ProfileActor("operator", "private", True),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@agents.command(name="revoke")
@click.argument("agent_id")
@click.pass_context
def revoke_command(ctx, agent_id):
    """Revoke a profile agent's bearer token and trusted access."""
    from drover.server.profile import revoke_agent_credential

    try:
        result = revoke_agent_credential(
            _operator_path(ctx),
            agent_id,
            actor=ProfileActor("operator", "private", True),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))
