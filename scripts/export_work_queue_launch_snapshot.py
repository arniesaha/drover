#!/usr/bin/env python3
"""Export a sanitized, read-only control-plane launch fixture.

The exporter never opens a writable transaction against its source.  Supplying
``--dry-run`` runs the Postgres launch contract against a disposable target DB,
with this export as its input; it never points tests at the source hub.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-dsn-env", default="DROVER_STUDIO_POSTGRES_DSN")
    parser.add_argument("--schema", default="drover_control")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the launch contract using DROVER_TEST_POSTGRES_DSN after export",
    )
    parser.add_argument("--test-dsn-env", default="DROVER_TEST_POSTGRES_DSN")
    return parser.parse_args()


def rows(cursor):
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def export(args: argparse.Namespace, dsn: str) -> dict:
    # psycopg applies this option before any query. BEGIN READ ONLY below is a
    # second independent guard against accidentally writing the Studio hub.
    import psycopg
    from psycopg import sql

    tables = {
        "hosts": ("harness_hosts", "host_id = %s", [args.host_id]),
        "work_items": (
            "work_items",
            "workspace_id = %s",
            [args.workspace_id],
        ),
        "execution_links": (
            "execution_links",
            "workspace_id = %s AND item_number = %s",
            [args.workspace_id, args.issue_number],
        ),
    }
    captured = {}
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as con:
        with con.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            for name, (table, where, parameters) in tables.items():
                cur.execute(
                    sql.SQL("SELECT * FROM {}.{} WHERE " + where).format(
                        sql.Identifier(args.schema), sql.Identifier(table)
                    ),
                    parameters,
                )
                captured[name] = rows(cur)
            cur.execute("ROLLBACK")

    if len(captured["hosts"]) != 1:
        raise SystemExit("expected exactly one selected harness host")
    host = captured["hosts"][0]
    # URLs, owners, actor identities, titles and acceptance text are not needed
    # to exercise the launch path. Keep only structural, non-secret fields.
    return {
        "format": 1,
        "workspace_id": "sanitized-launch-workspace",
        "issue_number": args.issue_number,
        "hosts": [
            {
                "host_id": "launch-host",
                "display_name": "Sanitized launch host",
                "kind": str(host["kind"]),
                "capabilities": host["capabilities_json"],
            }
        ],
        "work_items": [
            {"stage": row["stage"], "gate": row["gate"]}
            for row in captured["work_items"]
        ],
        "execution_links": [
            {
                "run_role": row["run_role"],
                "failure_count": row["failure_count"],
                "has_session": row["session_id"] is not None,
            }
            for row in captured["execution_links"]
        ],
    }


def main() -> None:
    args = arguments()
    source = os.environ.get(args.source_dsn_env)
    if not source:
        raise SystemExit(f"{args.source_dsn_env} is required")
    snapshot = export(args, source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    if not args.dry_run:
        return
    test_dsn = os.environ.get(args.test_dsn_env)
    if not test_dsn:
        raise SystemExit(f"{args.test_dsn_env} is required for --dry-run")
    if test_dsn == source:
        raise SystemExit("dry-run target must not be the source Studio Postgres DSN")
    env = {
        **os.environ,
        "DROVER_LAUNCH_SNAPSHOT": str(args.output.resolve()),
        "DROVER_TEST_POSTGRES_DSN": test_dsn,
    }
    subprocess.run(
        [
            "uv",
            "run",
            "--extra",
            "dev",
            "--extra",
            "postgres",
            "python",
            "-m",
            "pytest",
            "tests/test_work_queue_launch_postgres.py",
            "-q",
        ],
        check=True,
        env=env,
    )


if __name__ == "__main__":
    main()
