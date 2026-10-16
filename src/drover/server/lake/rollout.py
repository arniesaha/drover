"""Read-only point-release preflight. Schema changes stay on the full lake gate."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from drover.config import ACTIVATION_IN_PLACE
from drover.server.runtime import MARKER_FILENAME, RuntimeLayout, compare_versions
from drover.server.service_units import validate_launchd

from .cutover import (
    SERVICE_LABEL,
    Check,
    CutoverError,
    CutoverPlan,
    ServiceView,
    http_get,
    run_check,
    verify_backup,
)
from .runtime import sha256_file

# Run the SAME script in both interpreters, including old releases that do not
# ship this module. -I prevents a checkout/PYTHONPATH from impersonating a venv.
# No database connections, catalog attachment, downloads or migrations here.
SCHEMA_PROBE = r"""
import ast, hashlib, importlib.metadata, json, re, sys, zipfile
from pathlib import Path
import drover
import duckdb, _duckdb
from drover.server import postgres_schema
from drover.server.lake import runtime

root = Path(drover.__file__).resolve().parent
schema = []
for path in sorted((root / "server/lake").glob("*.py")):
    if path.name in {"rollout.py", "cutover.py", "cutover_cli.py", "gate.py"}:
        continue
    tree = ast.parse(path.read_text())
    declarations = []
    for node in tree.body:
        names = [n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)] if isinstance(node, (ast.Assign, ast.AnnAssign)) else []
        schema_assignment = any(n.endswith("_SCHEMA") or n in {"SCHEMAS", "LINEAGE"} for n in names)
        ddl_function = isinstance(node, (ast.FunctionDef, ast.ClassDef)) and any(
            isinstance(n, ast.Constant) and isinstance(n.value, str)
            and re.search(r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|VIEW|SCHEMA)\b", n.value, re.I)
            for n in ast.walk(node))
        schema_helper = isinstance(node, (ast.FunctionDef, ast.ClassDef)) and any(
            isinstance(n, ast.Call) and (
                isinstance(n.func, ast.Name) and n.func.id in {"create_table", "configure_catalog"}
                or isinstance(n.func, ast.Attribute) and n.func.attr in {"create_table", "configure_catalog"}
            ) for n in ast.walk(node))
        if schema_assignment or ddl_function or schema_helper:
            declarations.append(ast.dump(node, include_attributes=False))
    if declarations:
        schema.append([path.name, declarations])
if not schema:
    raise ValueError("lake schema declarations missing")
if duckdb.__version__ != runtime.DUCKDB_VERSION:
    raise ValueError("installed lake engine version mismatch")
engine_hash = hashlib.sha256(Path(_duckdb.__file__).read_bytes()).hexdigest()
with duckdb.connect(config={"autoload_known_extensions": False}) as con:
    platform = con.execute("PRAGMA platform").fetchone()[0]
hash_verified = False
if len(sys.argv) > 1:
    with zipfile.ZipFile(sys.argv[1]) as wheel:
        files = [n for n in wheel.namelist() if n.startswith("drover/") and not n.endswith("/")]
        if not files:
            raise ValueError("wheel has no installed package files")
        for name in files:
            relative = Path(name).relative_to("drover")
            if ".." in relative.parts:
                raise ValueError("wheel path invalid")
            installed = (root / relative).resolve()
            if root not in installed.parents or installed.read_bytes() != wheel.read(name):
                raise ValueError("installed package hash mismatch")
    hash_verified = True
print(json.dumps({
    "version": importlib.metadata.version("drover"),
    "migrations": postgres_schema._MIGRATIONS,
    "lake_schema_version": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
    "runtime_pins": hashlib.sha256(json.dumps([runtime.DUCKDB_VERSION, runtime.EXTENSION_HASHES, engine_hash], sort_keys=True).encode()).hexdigest(),
    "engine_sha256": engine_hash,
    "extension_hashes": runtime.EXTENSION_HASHES.get(platform, {}),
    "installed_hash_verified": hash_verified,
}))
"""


def probe_runtime(python: Path, wheel: Path | None = None) -> dict:
    """Ask an installed interpreter for compatibility and installed-file proof."""
    args = [str(python), "-I", "-B", "-c", SCHEMA_PROBE]
    if wheel is not None:
        args.append(str(wheel))
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode:
        # Subprocess stderr can contain private filesystem or environment data.
        raise CutoverError("release_runtime_probe_failed")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise CutoverError("release_runtime_metadata_invalid") from None
    if not isinstance(data, dict):
        raise CutoverError("release_runtime_metadata_invalid")
    return data


def run_release_preflight(
    plan: CutoverPlan,
    service: ServiceView,
    *,
    release: str,
    wheel_sha256: str,
    lock_sha256: str,
    backup: Path,
    min_free_bytes: int = 5 * 1024**3,
    max_backup_age_hours: float = 24,
    pg_bin: Path | None = None,
    device_token: str = "",
    probe: Callable = probe_runtime,
    run: Callable = subprocess.run,
    fetch: Callable = http_get,
) -> list[Check]:
    """Check staging before a flip, without requiring the target to be active.

    Independent wheel/lock digests come from the published release checksum
    manifest, never from a cache-generated manifest. No service mutations.
    """
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", release):
        raise CutoverError("release_version_invalid")
    if min_free_bytes < 0 or max_backup_age_hours <= 0:
        raise CutoverError("release_limits_invalid")
    if not device_token:
        raise CutoverError("release_device_token_missing")
    want = release.lstrip("v")
    checks = []
    holder = {}

    def plist():
        job = validate_launchd(service.plist_path.read_bytes())
        return job["Label"] == SERVICE_LABEL, {"valid": True}

    checks.append(run_check("plist_validity", plist))
    try:
        cfg = service.config()
    except Exception:
        return [
            *checks,
            Check("service_config", False, {"error": "service config unreadable"}),
        ]
    layout = RuntimeLayout(service.home / ".drover", root=cfg.update_runtime_root)

    def updater():
        pinned = cfg.update_pinned_version.lstrip("v")
        marker = (layout.root / MARKER_FILENAME).exists()
        return (not cfg.update_enabled or pinned == want) and not marker, {
            "update_enabled": cfg.update_enabled,
            "pinned_version": pinned,
            "release": want,
            "pending_verification_marker": marker,
        }

    checks.append(run_check("updater_state", updater))

    def runtime():
        if not all(
            re.fullmatch(r"[0-9a-fA-F]{64}", v) for v in (wheel_sha256, lock_sha256)
        ):
            raise CutoverError("release_digests_invalid")
        cached = layout.cached_artifact(want)
        if cached is None:
            raise CutoverError("release_artifact_cache_missing")
        if (
            sha256_file(cached.wheel) != wheel_sha256.lower()
            or sha256_file(cached.lock) != lock_sha256.lower()
        ):
            raise CutoverError("release_artifact_hash_mismatch")
        target = probe(layout.executable("python", want), cached.wheel)
        if cfg.analytics.backend == "ducklake":
            if target.get("engine_sha256") != cfg.analytics.engine_sha256:
                raise CutoverError("release_lake_engine_hash_mismatch")
            hashes = target.get("extension_hashes", {})
            if set(hashes) != {"ducklake", "postgres_scanner"}:
                raise CutoverError("release_lake_platform_unsupported")
            for name, digest in hashes.items():
                path = Path(cfg.analytics.extension_dir) / f"{name}.duckdb_extension"
                if sha256_file(path) != digest:
                    raise CutoverError("release_lake_extension_hash_mismatch")
        holder["target"] = target
        verified = target.get("installed_hash_verified") is True
        return verified and target.get("version") == want, {
            "release": want,
            "installed_hash_verified": verified,
            "reported_version": target.get("version"),
        }

    checks.append(run_check("runtime_installed", runtime))

    def kind():
        active = layout.active_version()
        if not active or not re.fullmatch(r"\d+\.\d+\.\d+", active):
            raise CutoverError("release_active_runtime_unknown")
        if compare_versions(want, active) < 0:
            raise CutoverError("release_downgrade_refused")
        argv = service.version_argv()
        program = Path(argv[0])
        current_python = (
            program.with_name("python") if program.name == "drover-server" else program
        )
        if cfg.update_activation == ACTIVATION_IN_PLACE:
            if not cfg.update_in_place_venv:
                raise CutoverError("release_in_place_venv_missing")
            expected_python = Path(cfg.update_in_place_venv).expanduser() / "bin/python"
        else:
            expected_python = layout.executable("python", active)
        # bin/python is commonly a symlink to a shared base interpreter.
        # Compare the venv directories, not that shared executable's inode.
        if current_python.parent.resolve() != expected_python.parent.resolve():
            raise CutoverError("release_service_runtime_mismatch")
        current = probe(current_python)
        if current.get("version") != active:
            raise CutoverError("release_current_version_mismatch")
        target = holder.get("target", {})
        keys = ("migrations", "lake_schema_version", "runtime_pins")
        if not all(current.get(k) and target.get(k) for k in keys):
            raise CutoverError("release_schema_metadata_missing")
        changed = [k for k in keys if current[k] != target[k]]
        return not changed, {
            "rollout_kind": "lake_schema" if changed else "point_release",
            "changed": changed,
            "current_version": active,
            "release": want,
            "lake_schema_version": target["lake_schema_version"],
        }

    checks.append(run_check("release_kind", kind))

    def disk():
        free = shutil.disk_usage(layout.root).free
        return free >= min_free_bytes, {
            "free_bytes": free,
            "min_free_bytes": min_free_bytes,
        }

    checks.append(run_check("disk", disk))

    def saved():
        directory = Path(backup)
        manifest = json.loads((directory / "manifest.json").read_text())
        required = {"control.dump", "config.toml", service.plist_path.name}
        if (
            set(manifest.get("files", {})) != required
            or manifest.get("lake") != plan.name
            or manifest.get("control_schema") != cfg.control_store.schema
        ):
            raise CutoverError("release_backup_manifest_invalid")
        created = datetime.fromisoformat(manifest["created_at"])
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if not 0 <= age <= max_backup_age_hours * 3600:
            raise CutoverError("release_backup_stale")
        for original, name in (
            (service.config_path, "config.toml"),
            (service.plist_path, service.plist_path.name),
        ):
            if sha256_file(original) != manifest["files"][name]["sha256"]:
                raise CutoverError("release_backup_configuration_changed")
        verify_backup(directory, run=run, pg_bin=pg_bin)
        return True, {"verified": True, "age_seconds": round(age)}

    checks.append(run_check("backup", saved))

    def health():
        url = service.hub_url(cfg)
        live_status, live = fetch(url + "/healthz")
        ready_status, _ = fetch(url + "/readyz")
        return (
            live_status == ready_status == 200 and live.strip() == "ok\nanalytical=ok",
            {
                "healthz_status": live_status,
                "readyz_status": ready_status,
                "analytical_ok": live.strip() == "ok\nanalytical=ok",
            },
        )

    checks.append(run_check("health", health))

    def host_proxy():
        headers = {"Authorization": f"Bearer {device_token}"}
        hub_url = service.hub_url(cfg)
        status, body = fetch(hub_url + "/harness/hosts", headers=headers)
        if status != 200:
            return False, {"hosts_status": status, "online_hosts": []}
        try:
            payload = json.loads(body)
            hosts = payload["hosts"]
        except (KeyError, TypeError, ValueError):
            return False, {"hosts_status": status, "online_hosts": []}
        online_ids = sorted(
            str(host.get("host_id"))
            for host in hosts
            if isinstance(host, dict)
            and host.get("status") == "online"
            and isinstance(host.get("host_id"), str)
            and host["host_id"]
        )
        statuses = {}
        for host_id in online_ids:
            proxy_status, _ = fetch(
                f"{hub_url}/harness/hosts/{host_id}/fs/list", headers=headers
            )
            statuses[host_id] = proxy_status
        return all(status == 200 for status in statuses.values()), {
            "hosts_status": status,
            "online_hosts": online_ids,
            "fs_list_statuses": statuses,
        }

    checks.append(run_check("host_proxy", host_proxy, device_token))
    return checks
