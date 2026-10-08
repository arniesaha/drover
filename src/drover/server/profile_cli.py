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
    r"\b(?:health|medical|finance|finances|financial|money|job[\s_-]*search|personal)\b",
    re.IGNORECASE,
)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_markdown(text, *, tier="general"):
    """Section items inherit private classification from every ancestor heading."""
    if tier not in ("general", "trusted"):
        raise ValueError("import tier must be general or trusted")
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
        private = any(SENSITIVE_HEADING.search(h) for h in headings)
        public_category = re.search(
            r"\b(?:preferences?|rules?|constraints?|style|work|projects?|threads?|decisions?)\b",
            heading,
            re.I,
        )
        ordinary_tier = tier if public_category else "trusted"
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
                tier="private" if private else ordinary_tier,
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


def import_sources(source: Path, *, tier="general"):
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
            record["change"],
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
    type=click.Choice(["general", "trusted"]),
    default="general",
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
