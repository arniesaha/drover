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


def _with_proof_pin(report, spec):
    from .serving_proof import proof_pin, proof_pin_hint

    digest = proof_pin(spec)
    return {
        **report,
        "verification_sha256": digest,
        "verification_sha256_hint": proof_pin_hint(digest),
    }


def _echo_report_with_proof_pin(report, spec):
    receipt = _with_proof_pin(report, spec)
    click.echo(json.dumps(receipt, indent=2))
    click.echo(receipt["verification_sha256_hint"])


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
    spec = spec_from_options(data_root, catalog_dsn_env)
    try:
        report = verify(spec)
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    except duckdb.OutOfMemoryException:
        raise click.ClickException(
            "lake_tool_memory_limit_exceeded; incomplete root retained for inspection"
        ) from None
    _echo_report_with_proof_pin(report, spec)


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
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the storage options and grants; do not contact or mutate the catalog.",
)
def provision_exporter_cmd(data_root, catalog_dsn_env, exporter_role, dry_run):
    """Explicit admin step: create export tables and the commit guard.

    Never run at startup. Run after ``rebuild`` and before role grants and
    ``verify``: provisioning changes the snapshot and invalidates any serving
    proof.
    """
    from .exporter import (
        EXPORT_INLINE_ROW_LIMIT,
        EXPORT_INLINE_TABLES,
        EXPORT_MUTABLE_TABLES,
        provision_exporter,
    )

    spec = spec_from_options(data_root, catalog_dsn_env)
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "dry_run": True,
                    "exporter_role": exporter_role,
                    "options": {
                        "data_inlining_row_limit": {
                            **{table: 0 for table in EXPORT_MUTABLE_TABLES},
                            **{
                                table: EXPORT_INLINE_ROW_LIMIT
                                for table in EXPORT_INLINE_TABLES
                            },
                        },
                        "expire_older_than": "30 days",
                    },
                    "grants": [
                        "GRANT TEMPORARY ON DATABASE <catalog> TO <admin-role>",
                        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA <metadata-schema> TO "
                        + exporter_role,
                        "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA <metadata-schema> TO "
                        + exporter_role,
                    ],
                    "next": "stop the hub, grant TEMP to the admin role, then run without --dry-run",
                },
                indent=2,
            )
        )
        return
    try:
        result = provision_exporter(spec, exporter_role=exporter_role)
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
                **result,
                "exporter_role": exporter_role,
                "next": "grant roles, then run lake verify (serving proof invalidated)",
            },
            indent=2,
        )
    )


@lake_cmd.command("maintain")
@click.option(
    "--data-root", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--catalog-dsn-env",
    required=True,
    help="Environment variable holding the catalog ADMIN DSN.",
)
@click.option("--dry-run", is_flag=True, help="Print the fenced maintenance plan.")
def maintain_cmd(data_root, catalog_dsn_env, dry_run):
    """Compact export files and expire old snapshots on a stopped hub."""
    from .maintenance import CLEANUP_RETENTION, EXPORT_MAINTENANCE_TABLES, maintain_lake

    if dry_run:
        click.echo(
            json.dumps(
                {
                    "dry_run": True,
                    "tables": EXPORT_MAINTENANCE_TABLES,
                    "operations": [
                        "drained_mutation fence",
                        "merge adjacent files per table",
                        "expire snapshots using configured retention",
                        f"clean files scheduled more than {CLEANUP_RETENTION} ago",
                    ],
                    "next": "stop the hub, then run without --dry-run",
                },
                indent=2,
            )
        )
        return
    try:
        report = maintain_lake(spec_from_options(data_root, catalog_dsn_env))
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    click.echo(json.dumps(report, indent=2))


@lake_cmd.command("deprecate-legacy")
@click.option(
    "--data-root", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--catalog-dsn-env",
    required=True,
    help="Environment variable holding the catalog ADMIN DSN.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Delete only the reported unreceipted legacy-import rows and recertify.",
)
def deprecate_legacy_cmd(data_root, catalog_dsn_env, apply):
    """Retire unreceipted legacy-import history; dry-run is the default."""
    from .legacy_deprecation import deprecate_legacy

    spec = spec_from_options(data_root, catalog_dsn_env)
    try:
        report = deprecate_legacy(spec, apply=apply)
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    if apply and not report.get("no_op"):
        _echo_report_with_proof_pin(report, spec)
    else:
        click.echo(json.dumps(report, indent=2))


@lake_cmd.command("reconcile")
@click.option(
    "--data-root", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--catalog-dsn-env", required=True, help="Catalog ADMIN DSN environment variable."
)
@click.option(
    "--control-dsn-env",
    required=True,
    help="Control PostgreSQL DSN environment variable.",
)
@click.option("--control-schema", default="drover_control", show_default=True)
@click.option(
    "--apply",
    is_flag=True,
    help="Republish the listed frozen batches; dry-run is the default.",
)
def reconcile_cmd(data_root, catalog_dsn_env, control_dsn_env, control_schema, apply):
    """Find acknowledged control batches missing from this lake and repair them."""
    from drover.config import ControlStoreConfig
    from drover.server.control_store import close_control_store, configure_control_store

    from .reconcile import reconcile_acknowledged

    # Lake tools deliberately do not resolve hub config. Register this short-
    # lived, explicitly named PG connection only for the operator command.
    control_path = data_root / ".reconcile-control-store"
    configure_control_store(
        control_path,
        ControlStoreConfig("postgres", control_dsn_env, 1, 1, 5.0, 5.0, control_schema),
    )
    try:
        report = reconcile_acknowledged(
            control_path, spec_from_options(data_root, catalog_dsn_env), apply=apply
        )
    except LakeError as exc:
        raise _lake_failure(exc, data_root) from None
    finally:
        close_control_store(control_path)
    click.echo(json.dumps(report, indent=2))
