"""Point releases must prove compatibility and staging without a lake cutover."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from drover.server.lake.cutover import CutoverPlan, ServiceView
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
            "migrations": [[1, "create table fixture"]],
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
        probe=probe,
        run=run,
        fetch=lambda url: (
            200,
            "ok\nanalytical=ok" if url.endswith("healthz") else "{}",
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


@pytest.mark.parametrize("field", ["migrations", "lake_schema_version", "runtime_pins"])
def test_schema_or_engine_change_requires_full_gate(tmp_path, field):
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
    assert json.loads(result.output)["passed"] is False


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
    assert json.loads(result.output)["passed"] is False
