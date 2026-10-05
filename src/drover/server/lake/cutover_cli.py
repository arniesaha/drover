"""``drover-server gate`` and ``drover-server cutover``; never read the hub config.

All names derive from ``--lake`` and ``--lake-root`` (``DROVER_LAKE_ROOT``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from .cutover import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_PLIST,
    MEMORY_LIMIT_ENV,
    CutoverError,
    CutoverPlan,
    ServiceView,
    execute,
    exit_code,
    rollback_actions,
    run_preflight,
    switch_actions,
    take_backup,
    verdict,
)


def _plan_options(command):
    options = [
        click.option("--lake", "lake", required=True, help="Target lake name."),
        click.option(
            "--lake-root",
            envvar="DROVER_LAKE_ROOT",
            required=True,
            type=click.Path(path_type=Path),
            help="Directory holding <lake>/ (env DROVER_LAKE_ROOT).",
        ),
        click.option("--gate-root", type=click.Path(path_type=Path)),
        click.option("--backup-root", type=click.Path(path_type=Path)),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _plan(lake, lake_root, gate_root, backup_root) -> CutoverPlan:
    try:
        return CutoverPlan.from_name(
            lake, lake_root=lake_root, gate_root=gate_root, backup_root=backup_root
        )
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from None


def _emit(document: dict) -> None:
    click.echo(json.dumps(document, indent=2, default=str))
    raise SystemExit(exit_code(document))


@click.command("gate")
@_plan_options
@click.option(
    "--preflight",
    is_flag=True,
    help="Only check prerequisites as the service sees them.",
)
@click.option(
    "--stage",
    type=click.Choice(["gate", "switch"]),
    default="gate",
    show_default=True,
    help="Preflight for running the gate, or for the switch itself.",
)
@click.option("--release", help="Release the service must run (preflight).")
@click.option(
    "--plist",
    type=click.Path(path_type=Path),
    default=str(DEFAULT_PLIST),
    show_default=True,
)
@click.option("--min-free-gb", type=float, default=50, show_default=True)
@click.option("--min-ram-gb", type=float, default=16, show_default=True)
@click.option("--max-verdict-age-hours", type=float, default=24, show_default=True)
@click.option(
    "--legacy-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Retained legacy parquet root to import from (read only).",
)
@click.option("--source-dsn-env", help="Env var with the source control-store DSN.")
@click.option(
    "--scratch-admin-dsn-env",
    help="Env var with an admin DSN on the scratch PostgreSQL cluster.",
)
@click.option("--control-schema", default="drover_control", show_default=True)
@click.option("--since-days", type=int, default=60, show_default=True)
@click.option(
    "--memory-limit",
    default=lambda: os.environ.get(MEMORY_LIMIT_ENV, DEFAULT_MEMORY_LIMIT),
    show_default=f"${MEMORY_LIMIT_ENV} or {DEFAULT_MEMORY_LIMIT}",
    help="Analytical budget the spare hub's peak RSS must stay within.",
)
@click.option("--port", type=int, default=0, help="Spare hub port (0 = free).")
@click.option("--hub-timeout", type=float, default=600, show_default=True)
@click.option("--keep", is_flag=True, help="Keep the scratch databases.")
@click.option("--replace", is_flag=True, help="Replace a previous gate's scratch.")
@click.option(
    "--allow-shared-cluster",
    is_flag=True,
    help="Permit scratch databases on the source's PostgreSQL cluster.",
)
@click.option("--pg-bin", type=click.Path(path_type=Path))
def gate_cmd(
    lake,
    lake_root,
    gate_root,
    backup_root,
    preflight,
    stage,
    release,
    plist,
    min_free_gb,
    min_ram_gb,
    max_verdict_age_hours,
    legacy_root,
    source_dsn_env,
    scratch_admin_dsn_env,
    control_schema,
    since_days,
    memory_limit,
    port,
    hub_timeout,
    keep,
    replace,
    allow_shared_cluster,
    pg_bin,
):
    """Pre-switch gate: one JSON verdict, non-zero exit on any failure."""
    plan = _plan(lake, lake_root, gate_root, backup_root)
    if preflight:
        if not release:
            raise click.UsageError("--preflight requires --release")
        try:
            service = ServiceView.load(plist)
        except (OSError, ValueError) as exc:
            _emit(verdict("preflight", plan, [], error=f"plist unreadable: {exc}"))
        checks = run_preflight(
            plan,
            service,
            stage=stage,
            release=release,
            min_free_bytes=int(min_free_gb * 1024**3),
            min_ram_bytes=int(min_ram_gb * 1024**3),
            max_verdict_age_hours=max_verdict_age_hours,
        )
        _emit(verdict("preflight", plan, checks, stage=stage, release=release))
    missing = [
        flag
        for flag, value in (
            ("--legacy-root", legacy_root),
            ("--source-dsn-env", source_dsn_env),
            ("--scratch-admin-dsn-env", scratch_admin_dsn_env),
        )
        if not value
    ]
    if missing:
        raise click.UsageError(f"the gate requires {', '.join(missing)}")
    from .gate import GateOptions, run_gate

    options = GateOptions(
        legacy_root=legacy_root,
        source_dsn_env=source_dsn_env,
        scratch_admin_dsn_env=scratch_admin_dsn_env,
        control_schema=control_schema,
        since_days=since_days,
        memory_limit=memory_limit,
        hub_timeout=hub_timeout,
        port=port,
        keep=keep,
        replace=replace,
        allow_shared_cluster=allow_shared_cluster,
        pg_bin=pg_bin,
    )
    _emit(run_gate(plan, options, echo=lambda m: click.echo(m, err=True)))


@click.group("cutover")
def cutover_cmd():
    """Backup-first, dry-runnable production switch and rollback."""


def _service_options(command):
    options = [
        click.option(
            "--plist",
            type=click.Path(path_type=Path),
            default=str(DEFAULT_PLIST),
            show_default=True,
        ),
        click.option("--pg-bin", type=click.Path(path_type=Path)),
        click.option(
            "--dry-run", is_flag=True, help="Print the exact actions; change nothing."
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _service(plist) -> ServiceView:
    try:
        return ServiceView.load(plist)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"plist unreadable: {exc}") from None


def _run(actions, dry_run) -> None:
    try:
        execute(actions, dry_run=dry_run, echo=click.echo)
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from None


@cutover_cmd.command("backup")
@_plan_options
@_service_options
def backup_cmd(lake, lake_root, gate_root, backup_root, plist, pg_bin, dry_run):
    """Control DB dump + config/plist copy, verified."""
    plan = _plan(lake, lake_root, gate_root, backup_root)
    service = _service(plist)
    if dry_run:
        click.echo(f"(dry-run) back up into {plan.backup_dir}/<utc stamp>/")
        return
    try:
        receipt = take_backup(plan, service, service.config(), pg_bin=pg_bin)
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(json.dumps(receipt, indent=2))


@cutover_cmd.command("switch")
@_plan_options
@_service_options
@click.option(
    "--legacy-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--max-verdict-age-hours", type=float, default=24, show_default=True)
@click.option("--verify-timeout", type=float, default=300, show_default=True)
def switch_cmd(
    lake,
    lake_root,
    gate_root,
    backup_root,
    plist,
    pg_bin,
    dry_run,
    legacy_root,
    max_verdict_age_hours,
    verify_timeout,
):
    """Gate verdict → backup → stop → fenced delta import → flip → start → verify."""
    plan = _plan(lake, lake_root, gate_root, backup_root)
    try:
        actions = switch_actions(
            plan,
            _service(plist),
            legacy_root=legacy_root,
            max_verdict_age_hours=max_verdict_age_hours,
            pg_bin=pg_bin,
            verify_timeout=verify_timeout,
        )
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from None
    _run(actions, dry_run)


@cutover_cmd.command("rollback")
@_plan_options
@_service_options
@click.option(
    "--since",
    type=float,
    help="Replay outbox commits from this epoch (default: recorded switch time).",
)
@click.option("--verify-timeout", type=float, default=300, show_default=True)
def rollback_cmd(
    lake,
    lake_root,
    gate_root,
    backup_root,
    plist,
    pg_bin,
    dry_run,
    since,
    verify_timeout,
):
    """Backup → stop → flip to legacy → outbox replay → start → verify."""
    plan = _plan(lake, lake_root, gate_root, backup_root)
    try:
        actions = rollback_actions(
            plan,
            _service(plist),
            since=since,
            pg_bin=pg_bin,
            verify_timeout=verify_timeout,
        )
    except CutoverError as exc:
        raise click.ClickException(str(exc)) from None
    _run(actions, dry_run)
