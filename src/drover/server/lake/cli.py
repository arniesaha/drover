"""Offline lake tooling, independent of live hub config resolution."""

import json
import os
from pathlib import Path

import click
import duckdb

from .backup import create_backup, restore_backup
from .rebuild import rebuild, verify
from .runtime import LakeError, LakeSpec, sanitize_detail

REBUILD_RSS_CEILING_ENV = "DROVER_LAKE_REBUILD_RSS_CEILING_BYTES"
REBUILD_RSS_CEILING_MIN = 512 * 1024**2
REBUILD_RSS_CEILING_MAX = 12 * 1024**3


def rebuild_rss_ceiling_from_env() -> int:
    """Read the rebuild-only aggregate supervisor cap in bytes.

    This intentionally is not a general lake runtime setting: it is consumed
    only by ``lake rebuild`` and is bounded to keep an accidental shell export
    from removing the offline job's safety ceiling.
    """
    from .admin_process import RSS_CEILING

    value = os.environ.get(REBUILD_RSS_CEILING_ENV)
    if value is None:
        return RSS_CEILING
    try:
        ceiling = int(value)
    except ValueError as exc:
        raise LakeError("rebuild_rss_ceiling_invalid") from exc
    if not REBUILD_RSS_CEILING_MIN <= ceiling <= REBUILD_RSS_CEILING_MAX:
        raise LakeError("rebuild_rss_ceiling_invalid")
    return ceiling


def _lake_failure(exc: LakeError, data_root) -> click.ClickException:
    """Show the stable code plus its sanitized cause; keep the cause with the root."""
    detail = exc.detail
    if not detail:
        return click.ClickException(exc.code)
    try:
        root = Path(data_root)
        if root.is_dir():
            with (root / "admin-error.log").open("a") as log:
                log.write(f"{exc.code}: {detail}\n")
    except OSError:
        pass
    return click.ClickException(f"{exc.code}: {detail}")


def spec_from_options(data_root, catalog_dsn_env):
    directory = os.environ.get("DROVER_LAKE_EXTENSION_DIR")
    digest = os.environ.get("DROVER_LAKE_ENGINE_SHA256")
    if not directory or not digest:
        raise click.ClickException(
            "set DROVER_LAKE_EXTENSION_DIR and independently verified DROVER_LAKE_ENGINE_SHA256"
        )
    return LakeSpec(catalog_dsn_env, data_root, Path(directory), digest)


@click.group("lake")
def lake_cmd():
    """Build and verify isolated event lakes; never resolve the hub config."""


@lake_cmd.command("rebuild")
@click.option(
    "--from-tar",
    "source",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--data-root", type=click.Path(path_type=Path), required=True)
@click.option("--catalog-dsn-env", required=True)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Build accounting and lineage locally without contacting PostgreSQL.",
)
def rebuild_cmd(source, data_root, catalog_dsn_env, dry_run):
    try:
        report = rebuild(
            source,
            spec_from_options(data_root, catalog_dsn_env),
            dry_run=dry_run,
            rss_ceiling=rebuild_rss_ceiling_from_env(),
        )
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    except duckdb.OutOfMemoryException:
        raise click.ClickException(
            "lake_tool_memory_limit_exceeded; incomplete root retained for inspection"
        ) from None
    click.echo(json.dumps(report, indent=2))


@lake_cmd.command("seed-tar")
@click.option("--output", type=click.Path(path_type=Path), required=True)
def seed_tar_cmd(output):
    """Write the empty-seed tar a fresh v2 lake is rebuilt from (no data)."""
    from .gate import write_seed_tar

    if output.exists():
        raise click.ClickException(f"{output} already exists")
    click.echo(json.dumps({"seed_tar": str(write_seed_tar(output))}))


@lake_cmd.command("backup")
@click.option(
    "--staging-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--catalog-snapshot",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--backup-root", type=click.Path(path_type=Path), required=True)
@click.option("--generation-id")
def backup_cmd(staging_root, catalog_snapshot, backup_root, generation_id):
    """Create an immutable generation only from a marked isolated staging copy."""
    try:
        receipt = create_backup(
            staging_root, catalog_snapshot, backup_root, generation_id=generation_id
        )
    except LakeError as exc:
        raise click.ClickException(exc.code) from None
    click.echo(json.dumps(receipt, indent=2))


@lake_cmd.command("restore")
@click.option(
    "--generation",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--data-root", type=click.Path(path_type=Path), required=True)
@click.option("--catalog-destination", type=click.Path(path_type=Path), required=True)
def restore_cmd(generation, data_root, catalog_destination):
    """Verify and restore a generation into two new, separate locations."""
    try:
        receipt = restore_backup(generation, data_root, catalog_destination)
    except LakeError as exc:
        raise click.ClickException(exc.code) from None
    click.echo(json.dumps(receipt, indent=2))


@lake_cmd.command("import")
@click.option("--since", help="Import events since this date (YYYY-MM-DD).")
@click.option("--session", "session_id", help="Import only this session ID.")
@click.option("--data-root", type=click.Path(path_type=Path), required=True)
@click.option("--catalog-dsn-env", required=True)
@click.option(
    "--legacy-root", type=click.Path(exists=True, path_type=Path), required=True
)
def import_cmd(since, session_id, data_root, catalog_dsn_env, legacy_root):
    from .legacy_import import run_import

    try:
        report = run_import(
            spec_from_options(data_root, catalog_dsn_env),
            legacy_root,
            since=since,
            session_id=session_id,
        )
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    click.echo(json.dumps(report, indent=2))


@lake_cmd.command("verify")
@click.option(
    "--data-root", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option("--catalog-dsn-env", required=True)
def verify_cmd(data_root, catalog_dsn_env):
    try:
        report = verify(spec_from_options(data_root, catalog_dsn_env))
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    except duckdb.OutOfMemoryException:
        raise click.ClickException(
            "lake_tool_memory_limit_exceeded; incomplete root retained for inspection"
        ) from None
    click.echo(json.dumps(report, indent=2))


@lake_cmd.command("provision-exporter")
@click.option(
    "--data-root", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--catalog-dsn-env",
    required=True,
    help="Environment variable holding the catalog ADMIN DSN.",
)
@click.option(
    "--exporter-role",
    required=True,
    help="Catalog group role that may activate the exporter guard.",
)
def provision_exporter_cmd(data_root, catalog_dsn_env, exporter_role):
    """Explicit admin step: create export tables and the commit guard.

    Never run at startup. Run after ``rebuild`` and before role grants and
    ``verify``: provisioning changes the snapshot and invalidates any serving
    proof.
    """
    from .exporter import provision_exporter

    spec = spec_from_options(data_root, catalog_dsn_env)
    try:
        provision_exporter(spec, exporter_role=exporter_role)
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    except Exception as exc:
        # Raw driver errors may embed connection details; show only the cleaned cause.
        failure = LakeError(
            "lake_export_provision_failed",
            sanitize_detail(exc, os.environ.get(catalog_dsn_env, "")),
        )
        raise _lake_failure(failure, data_root) from None
    click.echo(
        json.dumps(
            {
                "provisioned": True,
                "exporter_role": exporter_role,
                "next": "grant roles, then run lake verify (serving proof invalidated)",
            },
            indent=2,
        )
    )
