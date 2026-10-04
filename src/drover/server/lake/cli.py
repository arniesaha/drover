"""Offline lake tooling, independent of live hub config resolution."""

import json
import os
from pathlib import Path

import click
import duckdb

from .rebuild import rebuild, verify
from .runtime import LakeError, LakeSpec


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
            source, spec_from_options(data_root, catalog_dsn_env), dry_run=dry_run
        )
    except LakeError as exc:
        raise click.ClickException(exc.code) from None
    except duckdb.OutOfMemoryException:
        raise click.ClickException(
            "lake_tool_memory_limit_exceeded; incomplete root retained for inspection"
        ) from None
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
        raise click.ClickException(exc.code) from None
    except duckdb.OutOfMemoryException:
        raise click.ClickException(
            "lake_tool_memory_limit_exceeded; incomplete root retained for inspection"
        ) from None
    click.echo(json.dumps(report, indent=2))
