"""Point releases must prove compatibility and staging without a lake cutover."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drover.server.lake.cutover import Check, CutoverPlan, ServiceView
from drover.server.runtime import RuntimeLayout
from drover.server.service_units import render_launchd


def fixture(tmp_path):
    from drover.server.lake.rollout import run_release_preflight

    home = tmp_path / "home"
    cfg = home / ".drover/config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[update]\npinned_version = "0.6.5"\n[control_store]\nbackend = "postgres"\ndsn_env = "DROVER_CONTROL_DSN"\n'
    )
    plist = tmp_path / "com.drover.server.plist"
    plist.write_text(
        render_launchd(
            "com.drover.server",
            "/example/drover-server",
            ["--config", str(cfg), "run"],
            home=home,
            path_entries=["/usr/bin"],
        )
    )
    service = ServiceView.load(plist)
    layout = RuntimeLayout(home / ".drover")
    layout.flip("0.6.4")
    for version in ("0.6.4", "0.6.5"):
        python = layout.executable("python", version)
        python.parent.mkdir(parents=True)
        python.write_text("fixture interpreter")
    plist.write_text(
        render_launchd(
            "com.drover.server",
            str(layout.executable("drover-server")),
            ["--config", str(cfg), "run"],
            home=home,
            path_entries=["/usr/bin"],
        )
    )
    service = ServiceView.load(plist)
    wheel = tmp_path / "drover-0.6.5-py3-none-any.whl"
    lock = tmp_path / "requirements.lock.txt"
    wheel.write_bytes(b"wheel bytes")
    lock.write_bytes(b"lock bytes")
    layout.cache_artifact("0.6.5", wheel, lock)
    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    backup = tmp_path / "backup"
    backup.mkdir()
    for name, content in [
        ("control.dump", b"dump"),
        ("config.toml", cfg.read_bytes()),
        (plist.name, plist.read_bytes()),
    ]:
        (backup / name).write_bytes(content)
    from drover.server.lake.cutover import utc_now

    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "lake": "blue",
                "created_at": utc_now(),
                "control_schema": service.config().control_store.schema,
                "files": {
                    p.name: {
                        "bytes": p.stat().st_size,
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    }
                    for p in backup.iterdir()
                },
            }
        )
    )

    def probe(python, wheel=None):
        return {
            "version": "0.6.5" if "0.6.5" in str(python) else "0.6.4",
            "migrations": [[1, ["CREATE TABLE fixture (id INTEGER)"]]],
            "lake_schema_version": "schema-1",
            "runtime_pins": "pins-1",
            "installed_hash_verified": wheel is not None,
        }

    def run(argv, **kwargs):
        assert "--list" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="TABLE fixture", stderr="")

    options = dict(
        release="0.6.5",
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
        backup=backup,
        min_free_bytes=0,
        device_token="device-credential",
        probe=probe,
        run=run,
        fetch=lambda url, **kwargs: (
            (200, "ok\nanalytical=ok")
            if url.endswith("healthz")
            else (
                (
                    200,
                    json.dumps({"hosts": [{"host_id": "host-a", "status": "online"}]}),
                )
                if url.endswith("/harness/hosts")
                else (200, "{}")
            )
        ),
    )

    def check(**overrides):
        return {
            c.id: c
            for c in run_release_preflight(plan, service, **(options | overrides))
        }

    return service, layout, backup, options, check


def test_point_release_passes_with_staged_target_not_current(tmp_path):
    service, layout, backup, options, check = fixture(tmp_path)
    checks = check()
    assert all(c.passed for c in checks.values()), checks
    assert checks["release_kind"].evidence["rollout_kind"] == "point_release"
    assert layout.active_version() == "0.6.4"
    assert not (layout.root / "pending_verification.json").exists()


def test_host_proxy_gate_fails_on_non_200_and_passes_on_200(tmp_path):
    _, _, _, options, check = fixture(tmp_path)
    assert check()["host_proxy"].passed

    def failing_fetch(url, **kwargs):
        if url.endswith("healthz"):
            return 200, "ok\nanalytical=ok"
        if url.endswith("/harness/hosts"):
            assert kwargs["headers"] == {"Authorization": "Bearer device-credential"}
            return 200, json.dumps(
                {"hosts": [{"host_id": "host-a", "status": "online"}]}
            )
        assert url.endswith("/harness/hosts/host-a/fs/list")
        return 502, '{"error":"host offline"}'

    gate = check(fetch=failing_fetch)["host_proxy"]
    assert not gate.passed
    assert gate.evidence["fs_list_statuses"] == {"host-a": 502}


@pytest.mark.parametrize("field", ["lake_schema_version", "runtime_pins"])
def test_lake_schema_or_engine_change_requires_full_gate(tmp_path, field):
    _, _, _, options, check = fixture(tmp_path)
    original = options["probe"]

    def changed(python, wheel=None):
        data = original(python, wheel)
        if wheel is not None:
            data[field] = "changed"
        return data

    result = check(probe=changed)["release_kind"]
    assert not result.passed
    assert result.evidence["rollout_kind"] == "lake_schema"


def _release_metadata(migrations, *, schema="schema-1", pins="pins-1"):
    return {
        "version": "0.6.18",
        "migrations": migrations,
        "lake_schema_version": schema,
        "runtime_pins": pins,
    }


def _latest_control_migration_fixture():
    from drover.server.postgres_schema import _MIGRATIONS

    target = [[version, list(statements)] for version, statements in _MIGRATIONS]
    versions = [version for version, _ in target]
    assert versions == sorted(set(versions))
    return target[:-1], target, target[-1][0]


def test_additive_latest_control_migration_uses_point_release_path():
    from drover.server.lake.rollout import classify_release_kind

    current, target, migration_id = _latest_control_migration_fixture()
    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target)
    )

    assert allowed
    assert evidence["rollout_kind"] == "control_migration"
    assert evidence["new_migration_ids"] == [migration_id]
    expected_verdicts = []
    for statement_index, statement in enumerate(target[-1][1], start=1):
        statement = statement.strip().upper()
        expected_verdicts.append(
            {
                "migration_id": migration_id,
                "statement_index": statement_index,
                "allowed": True,
                "verdict": (
                    "create_table"
                    if statement.startswith("CREATE TABLE")
                    else (
                        "alter_table_add_column_constant_default"
                        if " DEFAULT " in statement
                        else (
                            "alter_table_add_nullable_column_inline_check"
                            if " CHECK " in statement
                            else (
                                "alter_table_add_nullable_column"
                                if statement.startswith("ALTER TABLE")
                                else "create_index"
                            )
                        )
                    )
                ),
            }
        )
    assert evidence["migration_verdicts"] == expected_verdicts


def test_control_migration_accepts_only_the_documented_additive_ddl_subset():
    from drover.server.lake.rollout import classify_release_kind

    current, _, migration_id = _latest_control_migration_fixture()
    target = [
        *current,
        [
            migration_id,
            [
                "CREATE SCHEMA IF NOT EXISTS release_fixture",
                "CREATE TABLE release_fixture.entries (id INTEGER PRIMARY KEY)",
                "CREATE INDEX CONCURRENTLY entries_id ON release_fixture.entries (id)",
                "ALTER TABLE harness_events ADD COLUMN rollout_state TEXT NOT NULL DEFAULT 'pending'",
                "GRANT SELECT ON TABLE release_fixture.entries TO release_reader",
            ],
        ],
    ]
    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target)
    )

    assert allowed
    assert evidence["rollout_kind"] == "control_migration"
    assert [item["verdict"] for item in evidence["migration_verdicts"]] == [
        "create_schema_if_not_exists",
        "create_table",
        "create_index",
        "alter_table_add_column_constant_default",
        "grant",
    ]


def test_control_migration_accepts_nullable_inline_numeric_range_check():
    from drover.server.lake.rollout import classify_release_kind

    current, _, migration_id = _latest_control_migration_fixture()
    target = [
        *current,
        [
            migration_id,
            [
                "ALTER TABLE control_credentials ADD COLUMN IF NOT EXISTS "
                "notification_hour INTEGER CHECK (notification_hour BETWEEN 0 AND 23)"
            ],
        ],
    ]

    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target)
    )

    assert allowed
    assert evidence["migration_verdicts"][0]["verdict"] == (
        "alter_table_add_nullable_column_inline_check"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (other_hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour my_not_null_domain CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour public.integer_domain CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour NUMERIC CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (hour BETWEEN lower_bound AND 23)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (hour BETWEEN 0 AND random())",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (hour BETWEEN 0 AND 23 OR hour = 99)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (hour BETWEEN 0 AND (SELECT 23))",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (hour BETWEEN 0 AND 23) CHECK (hour > 0)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER DEFAULT 0 CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CONSTRAINT range CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD CONSTRAINT hour_range CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD CHECK (hour BETWEEN 0 AND 23)",
        "ALTER TABLE entries ADD COLUMN hour INTEGER CHECK (hour BETWEEN 0 AND 23",
    ],
)
def test_control_migration_refuses_unsafe_inline_checks(statement):
    from drover.server.lake.rollout import classify_release_kind

    current, _, migration_id = _latest_control_migration_fixture()
    allowed, evidence = classify_release_kind(
        _release_metadata(current),
        _release_metadata([*current, [migration_id, [statement]]]),
    )

    assert not allowed
    assert evidence["migration_verdicts"][0]["verdict"] in {
        "alter_table_add_column_unparsed",
        "multiple_or_empty_statement",
    }


def test_control_migrations_28_through_36_classify_from_v0628_baseline():
    from drover.server.lake.rollout import classify_release_kind
    from drover.server.postgres_schema import _MIGRATIONS

    current = [
        [version, list(statements)]
        for version, statements in _MIGRATIONS
        if version <= 27
    ]
    target = [[version, list(statements)] for version, statements in _MIGRATIONS]
    assert [version for version, _ in target if version > 27] == list(range(28, 37))

    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target)
    )

    assert allowed
    assert evidence["new_migration_ids"] == list(range(28, 37))


@pytest.mark.parametrize(
    ("statement", "verdict"),
    [
        ("DROP TABLE old_events", "statement_not_additive_ddl"),
        (
            "ALTER TABLE harness_events RENAME COLUMN old TO new",
            "alter_table_not_add_column",
        ),
        (
            "ALTER TABLE harness_events ALTER COLUMN payload_json SET NOT NULL",
            "alter_table_not_add_column",
        ),
        ("UPDATE harness_events SET payload_json = NULL", "statement_not_additive_ddl"),
        (
            "ALTER TABLE harness_events ADD COLUMN required_value TEXT NOT NULL",
            "add_column_not_null_without_constant_default",
        ),
    ],
)
def test_non_additive_control_migration_is_refused(statement, verdict):
    from drover.server.lake.rollout import classify_release_kind

    current, _, migration_id = _latest_control_migration_fixture()
    target = [*current, [migration_id, [statement]]]
    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target)
    )

    assert not allowed
    assert evidence["rollout_kind"] == "control_migration"
    assert evidence["new_migration_ids"] == [migration_id]
    assert evidence["migration_verdicts"][0]["allowed"] is False
    assert evidence["migration_verdicts"][0]["verdict"] == verdict


def test_edited_shipped_control_migration_is_refused():
    from drover.server.lake.rollout import classify_release_kind

    current, _, _ = _latest_control_migration_fixture()
    target = [[version, list(statements)] for version, statements in current]
    target[0][1][0] = "CREATE TABLE changed_shipped_migration (id INTEGER)"
    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target)
    )

    assert not allowed
    assert evidence["rollout_kind"] == "control_migration"
    assert evidence["new_migration_ids"] == []
    assert (
        evidence["control_migration_reason"] == "migration_ledger_not_strictly_appended"
    )


@pytest.mark.parametrize("field", ["lake_schema_version", "runtime_pins"])
def test_lake_schema_or_runtime_pin_change_stays_on_full_gate(field):
    from drover.server.lake.rollout import classify_release_kind

    current, _, _ = _latest_control_migration_fixture()
    target = _release_metadata(current)
    target[field] = "changed"
    allowed, evidence = classify_release_kind(_release_metadata(current), target)

    assert not allowed
    assert evidence["rollout_kind"] == "lake_schema"


def test_combined_control_and_lake_change_stays_on_full_gate():
    from drover.server.lake.rollout import classify_release_kind

    current, target, migration_id = _latest_control_migration_fixture()
    allowed, evidence = classify_release_kind(
        _release_metadata(current), _release_metadata(target, schema="changed")
    )

    assert not allowed
    assert evidence["rollout_kind"] == "lake_schema"
    assert evidence["new_migration_ids"] == [migration_id]
    assert all(verdict["allowed"] for verdict in evidence["migration_verdicts"])


@pytest.mark.parametrize(
    "failure",
    [
        "pin",
        "marker",
        "wheel",
        "lock",
        "installed",
        "disk",
        "health",
        "backup",
        "stale_backup",
        "missing_metadata",
    ],
)
def test_release_preflight_blocks_unsafe_rollout(tmp_path, failure):
    service, layout, backup, options, check = fixture(tmp_path)
    overrides = {}
    expected = failure
    if failure == "pin":
        service.config_path.write_text('[update]\npinned_version = "0.6.4"\n')
        expected = "updater_state"
    elif failure == "marker":
        layout.write_marker("0.6.3", "0.6.4")
        expected = "updater_state"
    elif failure in ("wheel", "lock"):
        artifact = layout.cached_artifact("0.6.5")
        getattr(artifact, failure).write_bytes(b"corrupted")
        expected = "runtime_installed"
    elif failure == "installed":
        original = options["probe"]

        def unverified(python, wheel=None):
            return original(python, wheel) | {"installed_hash_verified": False}

        overrides["probe"] = unverified
        expected = "runtime_installed"
    elif failure == "disk":
        overrides["min_free_bytes"] = 10**30
        expected = "disk"
    elif failure == "health":
        overrides["fetch"] = lambda url: (503, "unavailable")
    elif failure == "backup":
        (backup / "control.dump").write_bytes(b"changed")
    elif failure == "stale_backup":
        p = backup / "manifest.json"
        d = json.loads(p.read_text())
        d["created_at"] = "2020-01-01T00:00:00+00:00"
        p.write_text(json.dumps(d))
        expected = "backup"
    elif failure == "missing_metadata":
        overrides["probe"] = lambda *args, **kwargs: {}
        expected = "release_kind"
    checks = check(**overrides)
    assert not checks[expected].passed


def test_release_probe_verifies_installed_bytes_and_emits_schema(tmp_path):
    """Use our real interpreter and a tiny wheel matching an installed file."""
    import sys
    import zipfile

    import drover.server.service_units as units
    from drover.server.lake.rollout import probe_runtime

    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("drover/server/service_units.py", Path(units.__file__).read_bytes())
    data = probe_runtime(Path(sys.executable), wheel)
    assert data["installed_hash_verified"] is True
    assert data["migrations"]
    assert data["lake_schema_version"]
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("drover/server/service_units.py", b"corrupted")
    with pytest.raises(Exception):
        probe_runtime(Path(sys.executable), wheel)


def test_array_plist_is_rejected_before_service_view(tmp_path):
    import plistlib

    path = tmp_path / "com.drover.server.plist"
    path.write_bytes(plistlib.dumps(["/example/drover-server", "run"]))
    with pytest.raises(ValueError, match="dictionary"):
        ServiceView.load(path)


def test_cli_emits_failed_verdict_for_invalid_job(tmp_path):
    import plistlib

    from click.testing import CliRunner

    from drover.server.lake.cutover_cli import cutover_cmd

    path = tmp_path / "com.drover.server.plist"
    path.write_bytes(plistlib.dumps(["/example/drover-server", "run"]))
    (tmp_path / "blue").mkdir()
    result = CliRunner().invoke(
        cutover_cmd,
        [
            "release-preflight",
            "--lake",
            "blue",
            "--lake-root",
            str(tmp_path),
            "--plist",
            str(path),
            "--release",
            "0.6.5",
            "--wheel-sha256",
            "a" * 64,
            "--lock-sha256",
            "b" * 64,
            "--backup",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    document = json.loads(result.output)
    assert document["passed"] is False
    assert document["error"] == "release preflight input invalid"
    assert document["detail"].startswith("ValueError:")


def test_preflight_rejects_service_outside_checked_runtime(tmp_path):
    from dataclasses import replace

    from drover.server.lake.rollout import run_release_preflight

    service, layout, backup, options, check = fixture(tmp_path)
    unrelated = replace(service, program=["/different/bin/drover-server", "run"])
    checks = {
        c.id: c
        for c in run_release_preflight(
            CutoverPlan.from_name("blue", lake_root=tmp_path), unrelated, **options
        )
    }
    assert not checks["release_kind"].passed


def _source_probe(tmp_path, source, prefix=""):
    import sys

    from drover.server.lake.rollout import SCHEMA_PROBE

    root = tmp_path / "drover"
    lake = root / "server/lake"
    lake.mkdir(parents=True, exist_ok=True)
    (lake / "producer.py").write_text(source)
    prelude = (
        f'import drover; drover.__file__ = {str(root / "__init__.py")!r}\n' + prefix
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", prelude + SCHEMA_PROBE],
        capture_output=True,
        text=True,
    )
    return result


def test_helper_schema_changes_alter_real_signature(tmp_path):
    source = 'EVENT_SCHEMA = {"id":"VARCHAR"}\ndef provision(con):\n    create_table(con, "events", EVENT_SCHEMA, day_partition=True)\n'
    a = _source_probe(tmp_path, source)
    b = _source_probe(tmp_path, source.replace('"events"', '"events_v2"'))
    assert a.returncode == b.returncode == 0
    assert (
        json.loads(a.stdout)["lake_schema_version"]
        != json.loads(b.stdout)["lake_schema_version"]
    )


def test_probe_rejects_wrong_installed_engine_version(tmp_path):
    result = _source_probe(
        tmp_path,
        'EVENT_SCHEMA = {"id":"VARCHAR"}',
        'import duckdb; duckdb.__version__ = "0.0.0"\n',
    )
    assert result.returncode != 0


def test_installed_engine_bytes_are_part_of_runtime_compatibility(tmp_path):
    engine = tmp_path / "engine.so"
    engine.write_bytes(b"first engine")
    prefix = f"import _duckdb; _duckdb.__file__ = {str(engine)!r}\n"
    a = _source_probe(tmp_path, 'EVENT_SCHEMA = {"id":"VARCHAR"}', prefix)
    engine.write_bytes(b"corrupted engine")
    b = _source_probe(tmp_path, 'EVENT_SCHEMA = {"id":"VARCHAR"}', prefix)
    assert a.returncode == b.returncode == 0
    assert json.loads(a.stdout)["runtime_pins"] != json.loads(b.stdout)["runtime_pins"]


@pytest.mark.parametrize("failure", ["engine", "extension", "missing_extension"])
def test_ducklake_preflight_checks_configured_engine_and_extensions(tmp_path, failure):
    service, layout, backup, options, check = fixture(tmp_path)
    extension_dir = tmp_path / "extensions"
    extension_dir.mkdir()
    for name in ("ducklake", "postgres_scanner"):
        (extension_dir / f"{name}.duckdb_extension").write_bytes(b"verified extension")
    digest = hashlib.sha256(b"verified extension").hexdigest()
    with service.config_path.open("a") as stream:
        stream.write(
            f'\n[analytics]\nbackend = "ducklake"\ncatalog_dsn_env = "DROVER_LAKE_BLUE_READER_DSN"\ndata_root = "{tmp_path / "lake"}"\nextension_dir = "{extension_dir}"\nengine_sha256 = "{"a"*64}"\nverification_sha256 = "{"b"*64}"\n'
        )
    original = options["probe"]

    def probe(python, wheel=None):
        return original(python, wheel) | {
            "engine_sha256": "c" * 64 if failure == "engine" else "a" * 64,
            "extension_hashes": {"ducklake": digest, "postgres_scanner": digest},
        }

    if failure == "extension":
        (extension_dir / "ducklake.duckdb_extension").write_bytes(b"corrupted")
    elif failure == "missing_extension":
        (extension_dir / "ducklake.duckdb_extension").unlink()
    assert not check(probe=probe)["runtime_installed"].passed


def test_service_venv_binding_does_not_follow_python_to_shared_base(tmp_path):
    import sys
    from dataclasses import replace

    from drover.server.lake.rollout import run_release_preflight

    service, layout, backup, options, check = fixture(tmp_path)
    checked_python = layout.executable("python", "0.6.4")
    checked_python.unlink()
    checked_python.symlink_to(sys.executable)
    unrelated_bin = tmp_path / "other-venv/bin"
    unrelated_bin.mkdir(parents=True)
    (unrelated_bin / "python").symlink_to(sys.executable)
    unrelated = replace(service, program=[str(unrelated_bin / "drover-server"), "run"])
    checks = {
        c.id: c
        for c in run_release_preflight(
            CutoverPlan.from_name("blue", lake_root=tmp_path), unrelated, **options
        )
    }
    assert not checks["release_kind"].passed


def test_cli_emits_failed_verdict_for_malformed_xml(tmp_path):
    from click.testing import CliRunner

    from drover.server.lake.cutover_cli import cutover_cmd

    path = tmp_path / "com.drover.server.plist"
    path.write_text('<?xml version="1.0"?><plist><dict>')
    (tmp_path / "blue").mkdir()
    result = CliRunner().invoke(
        cutover_cmd,
        [
            "release-preflight",
            "--lake",
            "blue",
            "--lake-root",
            str(tmp_path),
            "--plist",
            str(path),
            "--release",
            "0.6.5",
            "--wheel-sha256",
            "a" * 64,
            "--lock-sha256",
            "b" * 64,
            "--backup",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    document = json.loads(result.output)
    assert document["passed"] is False
    assert document["error"] == "release preflight input invalid"
    assert document["detail"].startswith("ValueError:")


def _release_preflight_input_args(tmp_path):
    from drover.server.service_units import render_launchd

    lake_root = tmp_path / "lake root"
    (lake_root / "blue").mkdir(parents=True)
    backup = tmp_path / "backup"
    backup.mkdir()
    plist = tmp_path / "com.drover.server.plist"
    plist.write_text(
        render_launchd(
            "com.drover.server",
            "/example/drover-server",
            ["--config", str(tmp_path / "config.toml"), "run"],
            home=tmp_path / "home",
            path_entries=["/usr/bin"],
        )
    )
    return {
        "lake_root": lake_root,
        "plist": plist,
        "backup": backup,
        "wheel_sha256": "a" * 64,
        "lock_sha256": "b" * 64,
    }


def _invoke_release_preflight(runner, arguments, *, env=None, **values):
    from drover.server.lake.cutover_cli import cutover_cmd

    return runner.invoke(
        cutover_cmd,
        [
            "release-preflight",
            "--lake",
            "blue",
            "--lake-root",
            str(values["lake_root"]),
            "--plist",
            str(values["plist"]),
            "--release",
            "0.6.5",
            "--wheel-sha256",
            values["wheel_sha256"],
            "--lock-sha256",
            values["lock_sha256"],
            "--backup",
            str(values["backup"]),
            *arguments,
        ],
        env=env,
    )


def test_release_preflight_ledger_records_snapshot_and_never_claims_rollout(
    tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from drover.server.lake import rollout
    from drover.server.lake.release_workflow import (
        read_release_workflow_ledger,
        sign_snapshot_receipt,
    )

    values = _release_preflight_input_args(tmp_path)
    receipt = tmp_path / "last-success.json"
    receipt.write_text(
        json.dumps(
            sign_snapshot_receipt(
                {
                    "date": "2026-10-22",
                    "host": "studio",
                    "nas_path": "/approved/snapshots/studio/2026-10-22",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "databases": 2,
                    "verification_ok": True,
                    "nas_pg_restore_list": True,
                    "verified_files": 3,
                    "verified_bytes": 100,
                    "artifact_manifest_sha256": "a" * 64,
                    "local_backup_manifest_sha256": "a" * 64,
                    "producer": {"name": "studio_backup", "schema_version": 2},
                },
                "test-receipt-key",
            )
        )
    )
    ledger_root = tmp_path / "evidence"
    ledger_root.mkdir(mode=0o700)
    ledger = ledger_root / "release-ledger.json"
    monkeypatch.setattr(
        rollout,
        "run_release_preflight",
        lambda *args, **kwargs: [Check("backup", True, {"verified": True})],
    )
    result = _invoke_release_preflight(
        CliRunner(),
        [
            "--nas-snapshot-receipt",
            str(receipt),
            "--ledger",
            str(ledger),
            "--ledger-root",
            str(ledger_root),
            "--nas-snapshot-manifest-sha256",
            "a" * 64,
        ],
        env={"DROVER_NAS_SNAPSHOT_RECEIPT_KEY": "test-receipt-key"},
        **values,
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["status"] == "blocked"
    assert document["phases"]["release_preflight"]["state"] == "succeeded"
    assert document["phases"]["rollout"]["state"] == "blocked"
    assert read_release_workflow_ledger(ledger) == document


def test_release_preflight_missing_plist_names_the_input(tmp_path):
    from click.testing import CliRunner

    values = _release_preflight_input_args(tmp_path)
    values["plist"] = tmp_path / "missing.plist"
    result = _invoke_release_preflight(CliRunner(), [], **values)

    document = json.loads(result.output)
    assert result.exit_code == 2
    assert document["error"] == "release preflight input invalid"
    assert document["detail"] == (
        f"FileNotFoundError: plist path does not exist: {values['plist']}"
    )


def test_release_preflight_missing_backup_names_the_input(tmp_path):
    from click.testing import CliRunner

    values = _release_preflight_input_args(tmp_path)
    values["backup"] = tmp_path / "missing-backup"
    result = _invoke_release_preflight(CliRunner(), [], **values)

    document = json.loads(result.output)
    assert result.exit_code == 2
    assert document["error"] == "release preflight input invalid"
    assert document["detail"] == (
        f"FileNotFoundError: backup path does not exist: {values['backup']}"
    )


def test_release_preflight_malformed_sha_names_the_input(tmp_path):
    from click.testing import CliRunner

    values = _release_preflight_input_args(tmp_path)
    values["wheel_sha256"] = "not-a-sha"
    result = _invoke_release_preflight(CliRunner(), [], **values)

    document = json.loads(result.output)
    assert result.exit_code == 2
    assert document["error"] == "release preflight input invalid"
    assert (
        document["detail"]
        == "ValueError: wheel sha256 must be 64 hexadecimal characters"
    )


def test_release_preflight_missing_lake_root_names_the_input(tmp_path):
    from click.testing import CliRunner

    values = _release_preflight_input_args(tmp_path)
    values["lake_root"] = tmp_path / "missing-lake-root"
    result = _invoke_release_preflight(CliRunner(), [], **values)

    document = json.loads(result.output)
    assert result.exit_code == 2
    assert document["error"] == "release preflight input invalid"
    assert document["detail"] == (
        f"FileNotFoundError: lake root does not exist: {values['lake_root']}"
    )


def test_release_preflight_input_error_redacts_token_and_dsn(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from drover.server.lake import rollout

    values = _release_preflight_input_args(tmp_path)
    token = "device-token-that-must-not-leak"
    dsn = "postgresql://release:dsn-password@db.example.test/drover"

    def invalid_input(*args, **kwargs):
        raise ValueError(f"token={token} dsn={dsn} password=dsn-password")

    monkeypatch.setattr(rollout, "run_release_preflight", invalid_input)
    result = _invoke_release_preflight(
        CliRunner(), [], **values, env={"DROVER_RELEASE_GATE_DEVICE_TOKEN": token}
    )

    assert result.exit_code == 2
    assert token not in result.output
    assert dsn not in result.output
    assert "dsn-password" not in result.output
