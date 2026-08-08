"""drover-collect CLI: per-host shipper for Drover."""

from __future__ import annotations

import logging
import os
import socket
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import click

from drover.collect.cursor import CursorLocked, CursorStore
from drover.collect.shipper import ShipError, ship_staging
from drover.collect import tempo_relay
from drover.collect.sources import (
    ClaudeCodeSource,
    ClaudeMacMiniSource,
    HermesSource,
    OpenClawSource,
    PiMonoSource,
    Source,
    latest_event_timestamp,
    write_events_jsonl,
)
from drover.config import default_config_file
from drover.models import AgentEvent

log = logging.getLogger("drover.collect")

_DEFAULT_CONFIG_PATH = default_config_file("collect.toml")


def _default_host_id() -> str:
    return socket.gethostname().split(".")[0]


_DEFAULT_CONFIG_TEMPLATE = """\
# drover-collect host config

host_id     = "{host_id}"
remote_host = "127.0.0.1"
remote_user = "{user}"
state_dir   = "{home}/.drover/state"
staging_dir = "{home}/.drover/staging"

[sources.claude_code]
enabled = true
root    = "{home}/.claude/projects"

[sources.claude_macmini]
enabled = false
root    = "{home}/Library/Application Support/Claude/local-agent-mode-sessions"

[sources.hermes]
enabled = false
root    = "{home}/.hermes/profiles/jenny/sessions"

[sources.openclaw]
enabled = false
root    = "{home}/.openclaw/agents/main/sessions"

[sources.pi_mono]
enabled = false
db_path = "{home}/max/data/task-journal.db"

# Tempo → OTLP relay. When enabled, `drover-collect tempo-relay` pulls
# AgentWeave spans from Tempo and pushes them to the lakehouse's OTLP
# gRPC receiver. Independent of the file-shipper above.
[tempo]
enabled              = false
tempo_base           = "http://127.0.0.1:3200"
services             = []
target_otlp_endpoint = "127.0.0.1:4317"
lookback_seconds     = 60
initial_window_s     = 3600
max_window_seconds   = 3600
search_timeout_s     = 30
trace_fetch_timeout_s = 60
push_timeout_s       = 30
"""


def _load_config(path: Optional[str]) -> dict:
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not p.exists():
        raise click.ClickException(
            f"config not found: {p}; run `drover-collect init` first"
        )
    with open(p, "rb") as f:
        return tomllib.load(f)


def _build_sources(cfg: dict) -> list[Source]:
    sources: list[Source] = []
    src_cfg = cfg.get("sources", {})
    # The host_id is the canonical agent_id for this machine's Claude Code
    # sessions. Per-source overrides (rare) win over the host default.
    host_id = cfg.get("host_id") or _default_host_id()

    if src_cfg.get("claude_code", {}).get("enabled"):
        sources.append(
            ClaudeCodeSource(
                root=Path(src_cfg["claude_code"]["root"]).expanduser(),
                agent_id=src_cfg["claude_code"].get("agent_id") or host_id,
            )
        )
    if src_cfg.get("claude_macmini", {}).get("enabled"):
        sources.append(
            ClaudeMacMiniSource(
                root=Path(src_cfg["claude_macmini"]["root"]).expanduser(),
                agent_id=src_cfg["claude_macmini"].get("agent_id") or host_id,
            )
        )
    if src_cfg.get("hermes", {}).get("enabled"):
        sources.append(HermesSource(root=Path(src_cfg["hermes"]["root"]).expanduser()))
    if src_cfg.get("openclaw", {}).get("enabled"):
        sources.append(
            OpenClawSource(root=Path(src_cfg["openclaw"]["root"]).expanduser())
        )
    if src_cfg.get("pi_mono", {}).get("enabled"):
        sources.append(
            PiMonoSource(db_path=Path(src_cfg["pi_mono"]["db_path"]).expanduser())
        )
    return sources


@click.group()
@click.option("--config", "config_path", default=None, help="Path to collect.toml")
@click.option("-v", "--verbose", is_flag=True, help="Enable DEBUG logging")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str], verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing config file")
@click.pass_context
def init(ctx: click.Context, force: bool) -> None:
    """Write a default collect.toml at --config (defaults to ~/.drover/collect.toml)."""
    p = Path(ctx.obj["config_path"]) if ctx.obj["config_path"] else _DEFAULT_CONFIG_PATH
    if p.exists() and not force:
        click.echo(f"config already exists: {p} (use --force to overwrite)", err=True)
        sys.exit(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        _DEFAULT_CONFIG_TEMPLATE.format(
            home=os.path.expanduser("~"),
            host_id=_default_host_id(),
            user=os.environ.get("USER", "user"),
        )
    )
    click.echo(f"wrote {p}")


@main.command()
@click.option("--source", "source_filter", default=None, help="Run only this source id")
@click.option("--dry-run", is_flag=True, help="Stage JSONL but do not run rsync")
@click.pass_context
def run(ctx: click.Context, source_filter: Optional[str], dry_run: bool) -> None:
    """Walk each enabled source, stage events as JSONL, ship via rsync."""
    cfg = _load_config(ctx.obj["config_path"])
    state = CursorStore(state_dir=Path(cfg["state_dir"]).expanduser())
    staging_dir = Path(cfg["staging_dir"]).expanduser()
    staging_dir.mkdir(parents=True, exist_ok=True)
    host_id = cfg["host_id"]
    remote_host = cfg.get("remote_host", "") or ""
    remote_user = cfg.get("remote_user")
    if remote_host in ("", "local", "localhost") and not remote_user:
        # Local-mode: write directly into the lakehouse on this machine.
        remote_target = ""
    else:
        remote_target = f"{remote_user}@{remote_host}" if remote_user else remote_host

    sources = _build_sources(cfg)
    if source_filter:
        sources = [s for s in sources if s.id == source_filter]
        if not sources:
            raise click.ClickException(f"no enabled source named {source_filter!r}")

    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    successes = 0
    failures = 0

    for src in sources:
        try:
            with state.lock(src.id):
                cursor = state.read(src.id)
                watermark = _parse_iso(cursor.get("watermark_iso"))
                files = src.list_files_since(watermark)
                if not files:
                    log.info("[%s] no new files (watermark=%s)", src.id, watermark)
                    successes += 1
                    continue

                events: list[AgentEvent] = []
                for f in files:
                    try:
                        events.extend(src.parse(f))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[%s] parse failed for %s: %s", src.id, f, exc)

                # Filter to events strictly after the watermark to avoid reshipping
                if watermark is not None:
                    events = [
                        e for e in events if _normalize_ts(e.timestamp) > watermark
                    ]

                out = write_events_jsonl(
                    events, staging_dir, run_id=run_id, source_id=src.id
                )
                if out is None:
                    log.info("[%s] no new events", src.id)
                    successes += 1
                    continue

                log.info("[%s] staged %d events to %s", src.id, len(events), out.name)
                new_watermark = latest_event_timestamp(events) or watermark

                if not dry_run:
                    try:
                        result = ship_staging(
                            staging_dir=staging_dir,
                            host=remote_target,
                            host_id=host_id,
                        )
                        log.info(
                            "[%s] shipped %d files (rc=%d)",
                            src.id,
                            result.files,
                            result.returncode,
                        )
                    except ShipError as exc:
                        log.error("[%s] ship failed: %s", src.id, exc)
                        failures += 1
                        continue

                # Advance cursor only after a successful (or dry-run) write
                if new_watermark is not None:
                    state.write(
                        src.id,
                        {
                            "watermark_iso": new_watermark.isoformat(),
                            "last_run_iso": datetime.now(tz=timezone.utc).isoformat(),
                        },
                    )
                successes += 1
        except CursorLocked as exc:
            log.warning("[%s] %s; skipping", src.id, exc)
            failures += 1
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] unexpected error: %s", src.id, exc, exc_info=True)
            failures += 1

    click.echo(f"sources ok: {successes}, failed: {failures}")
    if successes == 0 and failures > 0:
        sys.exit(2)


@main.command("tempo-relay")
@click.option(
    "--backfill-from",
    "backfill_from",
    default=None,
    help="ISO-8601 start of a one-shot backfill window. Bypasses the cursor; requires --backfill-to.",
)
@click.option(
    "--backfill-to",
    "backfill_to",
    default=None,
    help="ISO-8601 end of a one-shot backfill window.",
)
@click.option(
    "--chunk-minutes",
    type=int,
    default=60,
    show_default=True,
    help="Window size (minutes) per Tempo search call during --backfill.",
)
@click.pass_context
def tempo_relay_cmd(
    ctx: click.Context,
    backfill_from: Optional[str],
    backfill_to: Optional[str],
    chunk_minutes: int,
) -> None:
    """Pull AgentWeave spans from Tempo and push to the lakehouse OTLP receiver.

    Reads ``[tempo]`` from collect.toml. Default: walk one cursor-tracked
    window forward (the systemd timer's normal mode). With
    ``--backfill-from`` and ``--backfill-to``, replay an explicit time
    range in chunked windows — useful for catching up after an outage
    or seeding history. Backfill mode does **not** touch the cursor.
    """
    cfg = _load_config(ctx.obj["config_path"])
    t_cfg = cfg.get("tempo") or {}
    if not t_cfg.get("enabled"):
        raise click.ClickException("[tempo].enabled is false in collect.toml")

    state = CursorStore(state_dir=Path(cfg["state_dir"]).expanduser())

    if backfill_from or backfill_to:
        if not (backfill_from and backfill_to):
            raise click.ClickException(
                "--backfill-from and --backfill-to must be used together"
            )
        try:
            start_dt = datetime.fromisoformat(backfill_from)
            end_dt = datetime.fromisoformat(backfill_to)
        except ValueError as exc:
            raise click.ClickException(f"bad ISO timestamp: {exc}") from exc
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        def _progress(chunk_num: int, c_start: int, c_end: int, s) -> None:
            log.info(
                "  chunk %d: %s..%s traces=%d/%d spans=%d errors=%d",
                chunk_num,
                datetime.fromtimestamp(c_start, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(c_end, tz=timezone.utc).isoformat(),
                s.traces_seen,
                s.traces_fetched,
                s.spans_sent,
                len(s.errors),
            )

        stats = tempo_relay.backfill(
            tempo_base=t_cfg["tempo_base"],
            target_otlp=t_cfg["target_otlp_endpoint"],
            services=t_cfg.get("services") or list(tempo_relay.DEFAULT_SERVICES),
            start_epoch=int(start_dt.timestamp()),
            end_epoch=int(end_dt.timestamp()),
            chunk_seconds=chunk_minutes * 60,
            search_limit=int(
                t_cfg.get("search_limit", tempo_relay.DEFAULT_SEARCH_LIMIT)
            ),
            search_timeout_s=float(
                t_cfg.get("search_timeout_s", tempo_relay.DEFAULT_SEARCH_TIMEOUT_S)
            ),
            fetch_timeout_s=float(
                t_cfg.get("trace_fetch_timeout_s", tempo_relay.DEFAULT_FETCH_TIMEOUT_S)
            ),
            push_timeout_s=float(
                t_cfg.get("push_timeout_s", tempo_relay.DEFAULT_PUSH_TIMEOUT_S)
            ),
            progress=_progress,
        )
        click.echo(
            f"backfill {backfill_from}..{backfill_to}: "
            f"traces={stats.traces_seen}/{stats.traces_fetched} "
            f"spans={stats.spans_sent} push_calls={stats.push_calls} "
            f"errors={len(stats.errors)}"
        )
        if stats.errors and stats.spans_sent == 0 and stats.traces_seen > 0:
            sys.exit(2)
        return

    try:
        with state.lock(tempo_relay.CURSOR_KEY):
            stats = tempo_relay.relay_once(
                tempo_base=t_cfg["tempo_base"],
                target_otlp=t_cfg["target_otlp_endpoint"],
                services=t_cfg.get("services") or list(tempo_relay.DEFAULT_SERVICES),
                state=state,
                lookback_s=int(
                    t_cfg.get("lookback_seconds", tempo_relay.DEFAULT_LOOKBACK_S)
                ),
                initial_window_s=int(t_cfg.get("initial_window_s", 3600)),
                max_window_s=int(
                    t_cfg.get("max_window_seconds", tempo_relay.DEFAULT_TIMER_WINDOW_S)
                ),
                search_timeout_s=float(
                    t_cfg.get("search_timeout_s", tempo_relay.DEFAULT_SEARCH_TIMEOUT_S)
                ),
                fetch_timeout_s=float(
                    t_cfg.get(
                        "trace_fetch_timeout_s", tempo_relay.DEFAULT_FETCH_TIMEOUT_S
                    )
                ),
                push_timeout_s=float(
                    t_cfg.get("push_timeout_s", tempo_relay.DEFAULT_PUSH_TIMEOUT_S)
                ),
                search_limit=int(
                    t_cfg.get("search_limit", tempo_relay.DEFAULT_SEARCH_LIMIT)
                ),
            )
    except CursorLocked as exc:
        log.warning("tempo_relay: %s; skipping", exc)
        return

    click.echo(
        f"tempo_relay: traces={stats.traces_fetched}/{stats.traces_seen} "
        f"spans={stats.spans_sent} push_calls={stats.push_calls} "
        f"errors={len(stats.errors)}"
    )
    if stats.errors and stats.spans_sent == 0 and stats.traces_seen > 0:
        sys.exit(2)


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print effective config + per-source cursor state."""
    cfg = _load_config(ctx.obj["config_path"])
    state = CursorStore(state_dir=Path(cfg["state_dir"]).expanduser())
    staging_dir = Path(cfg["staging_dir"]).expanduser()
    sources = _build_sources(cfg)

    click.echo(f"host_id     : {cfg['host_id']}")
    click.echo(f"remote      : {cfg.get('remote_user', '')}@{cfg['remote_host']}")
    click.echo(f"state_dir   : {state.state_dir}")
    click.echo(f"staging_dir : {staging_dir}")
    click.echo(
        f"staging     : {len(list(staging_dir.glob('*.jsonl')))} *.jsonl files pending"
    )
    click.echo("")
    click.echo("sources:")
    for s in sources:
        cur = state.read(s.id)
        wm = cur.get("watermark_iso", "—")
        lr = cur.get("last_run_iso", "—")
        click.echo(f"  {s.id:18s} watermark={wm}  last_run={lr}")

    t_cfg = cfg.get("tempo") or {}
    if t_cfg.get("enabled"):
        cur = state.read(tempo_relay.CURSOR_KEY)
        wm = cur.get("last_end_iso", "—")
        lr = cur.get("last_run_iso", "—")
        click.echo(
            f"  {tempo_relay.CURSOR_KEY:18s} window_end={wm}  last_run={lr}  "
            f"(target={t_cfg.get('target_otlp_endpoint')})"
        )


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _normalize_ts(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


if __name__ == "__main__":  # pragma: no cover
    main()
