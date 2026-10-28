#!/usr/bin/env python3
"""Safely roll a Studio hub and its local harnessd to one staged runtime.

This is deliberately an operator tool, not an installer hook.  It refuses a
partly specified release and makes its rollback choice explicit in the JSON
report.  In particular, ``--dry-run`` performs every inspection but never
writes a plist, flips a link, or calls a mutating launchctl subcommand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LABELS = ("com.drover.server", "com.drover.harnessd")
CONTINUITY_SECRET = "DROVER_WORK_COORDINATOR_CONTINUITY_SECRET"
MIN_FREE_BYTES = 5 * 1024**3


class RolloutError(RuntimeError):
    """An expected rollout failure; its message is safe to put in a report."""


@dataclass(frozen=True)
class PlistView:
    label: str
    path: Path
    launchd_environment: Mapping[str, str]
    environment: Mapping[str, str]
    arguments: tuple[str, ...]


def _secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _env_file(arguments: list[object]) -> Path | None:
    """Return the fixed env-file argument rendered by ``render_launchd``."""
    if (
        len(arguments) >= 5
        and arguments[:4]
        == [
            "/bin/sh",
            "-c",
            'set -a; . "$1" || exit $?; shift; exec "$@"',
            "drover-service-env",
        ]
        and isinstance(arguments[4], str)
    ):
        return Path(arguments[4])
    return None


def _read_env_file(path: Path) -> dict[str, str]:
    """Read the installer-owned, single-line quoted environment file.

    It intentionally accepts no shell syntax other than the format the
    installer writes.  A rollout must not execute an operator's env file just
    to inspect it.
    """
    environment: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RolloutError(f"environment_file_unreadable:{path.name}") from exc
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r'([A-Z_][A-Z0-9_]*)="((?:[^"\\]|\\[\\"$`])*)"', line)
        if match is None:
            raise RolloutError(f"environment_file_invalid:{path.name}")
        name, encoded = match.groups()
        environment[name] = re.sub(r"\\([\\\"$`])", r"\1", encoded)
    return environment


def load_plist(path: Path) -> PlistView:
    try:
        payload = plistlib.loads(path.read_bytes())
        label = payload["Label"]
        raw_environment = payload.get("EnvironmentVariables", {})
        arguments = payload["ProgramArguments"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RolloutError(f"plist_invalid:{path.name}") from exc
    if not isinstance(label, str) or not isinstance(raw_environment, dict):
        raise RolloutError(f"plist_invalid:{path.name}")
    launchd_environment = {
        str(name): str(value)
        for name, value in raw_environment.items()
        if isinstance(name, str) and isinstance(value, str)
    }
    environment = dict(launchd_environment)
    if not isinstance(arguments, list):
        raise RolloutError(f"plist_invalid:{path.name}")
    source = _env_file(arguments)
    if source is not None:
        environment.update(_read_env_file(source))
    if not all(isinstance(argument, str) for argument in arguments):
        raise RolloutError(f"plist_invalid:{path.name}")
    return PlistView(
        label,
        path,
        launchd_environment,
        environment,
        tuple(arguments),
    )


def config_from_arguments(view: PlistView, fallback: Path) -> Path:
    """Use the config the server unit actually starts, not the caller's cwd."""
    try:
        return Path(view.arguments[view.arguments.index("--config") + 1])
    except (ValueError, IndexError):
        return fallback


def launchctl_environment(text: str) -> dict[str, str]:
    """Parse launchctl's printed environment without depending on its layout."""
    found: dict[str, str] = {}
    for name, value in re.findall(r"(?m)^\s*([A-Z_][A-Z0-9_]*)\s*=>\s*(.*?)\s*$", text):
        found[name] = value.strip().strip('"')
    return found


def environment_diff(required: set[str], *environments: Mapping[str, str]) -> dict:
    """Return a non-secret diff for candidate, last-good, and running envs."""
    names = sorted(required | set().union(*(set(env) for env in environments)))
    rows = {}
    for name in names:
        values = [env.get(name) for env in environments]
        rows[name] = {
            "present": [value is not None and bool(value) for value in values],
            "fingerprints": [
                _secret_fingerprint(value) if value else None for value in values
            ],
            "changed": len({value for value in values if value is not None}) > 1,
        }
    missing = {
        name: [index for index, present in enumerate(row["present"]) if not present]
        for name, row in rows.items()
        if name in required and not all(row["present"])
    }
    return {"variables": rows, "missing_required": missing}


def required_environment(config_path: Path, label: str) -> set[str]:
    """Return the required environment for one launchd service.

    The hub loads control, continuity, and lake configuration from its
    environment.  harnessd's relay CLI gets its optional credential from an
    argument, ``DROVER_API_TOKEN``, or ``~/.drover/api_token``; it has no
    required launchd environment variables.
    """
    if label == "com.drover.harnessd":
        return set()
    if label != "com.drover.server":
        raise ValueError(f"unknown_launchd_label:{label}")
    from drover.config import load_config

    cfg = load_config(config_path)
    required = {CONTINUITY_SECRET}
    if cfg.control_store.backend == "postgres":
        required.add(cfg.control_store.dsn_env)
    if cfg.analytics.backend == "ducklake":
        required.add(cfg.analytics.catalog_dsn_env)
        required.update(
            name
            for name in (
                cfg.analytics.exporter_dsn_env,
                cfg.analytics.maintenance_dsn_env,
            )
            if name
        )
    return required


def rollout_token(
    config_path: Path, drover_home: Path, *, override_env: str | None = None
) -> str:
    """Use the same private token resolution as the Studio's local CLI.

    The rollout needs a bearer with device-equivalent access, but must never
    mint or persist one.  The shared local API token is accepted by those
    routes and already has an owner-only on-disk home.
    """
    if override_env:
        token = os.environ.get(override_env, "").strip()
        if not token:
            raise RolloutError("rollout_device_token_override_missing")
        return token
    try:
        service_env = _read_env_file(config_path.parent / "server.env")
    except RolloutError:
        service_env = {}
    if token := service_env.get("DROVER_API_TOKEN", "").strip():
        return token
    from drover.config import load_config

    if token := load_config(config_path).auth_api_token.strip():
        return token
    try:
        token = (drover_home / "api_token").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RolloutError("rollout_device_token_missing") from exc
    if not token:
        raise RolloutError("rollout_device_token_missing")
    return token


def _snapshot_name(now: datetime) -> str:
    timestamp = now.astimezone(timezone.utc)
    return timestamp.strftime("%Y%m%dT%H%M%S.%fZ")


def record_last_good(
    plists: Path,
    rollout_root: Path,
    *,
    last_good_name: str = "last-good",
    now: datetime | None = None,
) -> Path:
    """Atomically select a newly verified plist snapshot and retain five."""
    current = now or datetime.now(timezone.utc)
    history = rollout_root / "history"
    history.mkdir(parents=True, exist_ok=True)
    snapshot = history / _snapshot_name(current)
    snapshot.mkdir(mode=0o700)
    for label in LABELS:
        _copy_atomic(plists / f"{label}.plist", snapshot / f"{label}.plist")
    last_good = rollout_root / last_good_name
    staged = rollout_root / f".{last_good_name}.new"
    staged.unlink(missing_ok=True)
    staged.symlink_to(Path("history") / snapshot.name)
    if last_good.exists() and not last_good.is_symlink():
        legacy = rollout_root / f".{last_good_name}.previous"
        if legacy.exists() or legacy.is_symlink():
            shutil.rmtree(legacy)
        os.replace(last_good, legacy)
        try:
            os.replace(staged, last_good)
        finally:
            shutil.rmtree(legacy, ignore_errors=True)
    else:
        os.replace(staged, last_good)
    snapshots = sorted(
        entry
        for entry in history.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    )
    for obsolete in snapshots[:-5]:
        shutil.rmtree(obsolete)
    return snapshot


def runtime_points_to(current: Path, target: Path) -> bool:
    try:
        return current.resolve(strict=True) == target.resolve(strict=True)
    except OSError:
        return False


def bootstrap_last_good(
    plists: Path,
    rollout_root: Path,
    required: Mapping[str, set[str]],
    running_environments: Mapping[str, Mapping[str, str]],
    *,
    last_good_name: str = "last-good",
    apply: bool = True,
) -> Path | None:
    """Seed script-owned history only from a complete, live-safe baseline."""
    for label in LABELS:
        current = load_plist(plists / f"{label}.plist")
        diff = environment_diff(
            required.get(label, set()), current.environment, running_environments[label]
        )
        if diff["missing_required"]:
            raise RolloutError(f"required_environment_missing:{label}")
    if not apply:
        return None
    return record_last_good(plists, rollout_root, last_good_name=last_good_name)


def runtime_preflight(target: Path, engine_sha256: str = "") -> dict:
    python = target / "bin/python"
    server = target / "bin/drover-server"
    harnessd = target / "bin/drover-harnessd"
    if not all(
        path.is_file() and os.access(path, os.X_OK)
        for path in (python, server, harnessd)
    ):
        raise RolloutError("target_runtime_incomplete")
    probe = (
        "import hashlib,json,sys; import _duckdb; "
        "print(json.dumps({'python':list(sys.version_info[:2]),"
        "'engine_sha256':hashlib.sha256(open(_duckdb.__file__,\"rb\").read()).hexdigest()}))"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", probe],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RolloutError("target_runtime_probe_failed") from exc
    try:
        evidence = json.loads(result.stdout)
    except ValueError as exc:
        raise RolloutError("target_runtime_probe_failed") from exc
    if result.returncode or evidence.get("python") != [3, 14]:
        raise RolloutError("target_runtime_not_python_3_14")
    if engine_sha256 and evidence.get("engine_sha256") != engine_sha256:
        raise RolloutError("target_lake_engine_sha256_mismatch")
    return evidence


def health(url: str, *, timeout: float = 5) -> tuple[bool, dict]:
    try:
        with urlopen(Request(url), timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return 200 <= response.status < 300, {
                "status": response.status,
                "body": body[:200],
            }
    except (HTTPError, URLError, OSError) as exc:
        return False, {"error": type(exc).__name__}


def api_json(url: str, token: str) -> tuple[bool, dict]:
    try:
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
            return response.status == 200 and isinstance(payload, dict), payload
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return False, {"error": type(exc).__name__}


def relay_hosts(payload: Mapping[str, object]) -> set[str]:
    hosts = payload.get("hosts", [])
    if not isinstance(hosts, list):
        return set()
    return {
        row["host_id"]
        for row in hosts
        if isinstance(row, dict)
        and row.get("connection_kind") == "relay"
        and isinstance(row.get("host_id"), str)
    }


def relay_reconnected(
    payload: Mapping[str, object], expected: set[str]
) -> tuple[bool, list[str]]:
    hosts = payload.get("hosts", [])
    rows = (
        {row.get("host_id"): row for row in hosts if isinstance(row, dict)}
        if isinstance(hosts, list)
        else {}
    )
    missing = sorted(
        host for host in expected if rows.get(host, {}).get("status") != "online"
    )
    return not missing, missing


def work_queue_settled(payload: Mapping[str, object]) -> bool:
    """Interpret either local state or an optional hub report safely.

    Older hubs predate ``execution.active``.  Their report is diagnostic only,
    so missing active state must not turn a compatible rollout into a block.
    """
    if "active" in payload:
        return payload.get("active") == 0
    actions = payload.get("actions", [])
    execution = payload.get("execution", {})
    active = execution.get("active") if isinstance(execution, dict) else None
    return (
        isinstance(actions, list)
        and (active is None or active == 0)
        and not any(
            isinstance(action, dict) and action.get("kind") == "launch_candidate"
            for action in actions
        )
    )


def queue_settlement(
    links: list[Mapping[str, object]],
    *,
    orphan_intent_minutes: int,
    systemic_attempts: Mapping[str, int],
    now: datetime | None = None,
) -> dict:
    """Classify durable links without contacting a hub or changing the store."""
    from drover.server.harness.models import TERMINAL_SESSION_STATUSES
    from drover.server.work_queue.store import stale_sessionless_intent

    active = 0
    stuck_intents = []
    for link in links:
        session_id = link.get("session_id")
        if session_id:
            # HarnessBridge.active() deliberately fails closed when a linked
            # session cannot be resolved; a missing status therefore blocks.
            if link.get("session_status") not in TERMINAL_SESSION_STATUSES:
                active += 1
            continue
        failures = int(link.get("failure_count") or 0)
        attempts = systemic_attempts.get(str(link.get("goal_id")), 3)
        if failures >= attempts:
            reason = "systemic"
        elif stale_sessionless_intent(link, minutes=orphan_intent_minutes, now=now):
            reason = "stale"
        else:
            active += 1
            continue
        stuck_intents.append(
            {
                "id": str(link.get("id", "")),
                "goal_id": str(link.get("goal_id", "")),
                "run_key": str(link.get("run_key", "")),
                "failure_count": failures,
                "reason": reason,
            }
        )
    return {"active": active, "stuck_intents": stuck_intents}


def local_work_queue_state(
    dsn: str,
    *,
    schema: str,
    workspace_id: str,
    orphan_intent_minutes: int,
    systemic_attempts: Mapping[str, int],
) -> dict:
    """Read rollout settledness directly from PostgreSQL in a read-only txn."""
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RolloutError("control_store_postgres_driver_missing") from exc
    query = sql.SQL("""SELECT link.id, link.goal_id, link.run_key, link.session_id,
                  link.failure_count, link.created_at, link.updated_at,
                  session.status AS session_status
             FROM {} AS link
             LEFT JOIN {} AS session ON session.session_id = link.session_id
            WHERE link.workspace_id = %s
            ORDER BY link.created_at, link.id""").format(
        sql.Identifier(schema, "execution_links"),
        sql.Identifier(schema, "harness_sessions"),
    )
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                links = list(connection.execute(query, [workspace_id]))
    except Exception as exc:
        raise RolloutError(f"control_store_read_failed:{type(exc).__name__}") from exc
    return queue_settlement(
        links,
        orphan_intent_minutes=orphan_intent_minutes,
        systemic_attempts=systemic_attempts,
    )


def optional_work_queue_report(url: str, token: str) -> dict:
    """Return, but never gate on, a paired-device queue report."""
    ok, payload = api_json(url, token)
    return {"ok": ok, "report": payload}


def work_queue_attempts(config) -> dict[str, int]:
    """Return each goal's systemic-stop threshold from the hub policy files."""
    from drover.server.work_queue.config import load_policy

    return {
        goal.goal_id: load_policy(goal.policy_path).retries.attempts
        for goal in config.goals
    }


def wait_for_work_queue(
    read_local: Callable[[], dict],
    timeout: float,
    *,
    supplemental: Callable[[], dict] | None = None,
) -> tuple[bool, dict]:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        last = read_local()
        if supplemental is not None:
            last["http_report"] = supplemental()
        if work_queue_settled(last):
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(2)


def _copy_atomic(source: Path, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as staged:
        staged_path = Path(staged.name)
        staged.write(source.read_bytes())
        staged.flush()
        os.fsync(staged.fileno())
    try:
        shutil.copymode(source, staged_path)
        os.replace(staged_path, destination)
    finally:
        staged_path.unlink(missing_ok=True)


def _run(argv: list[str], *, dry_run: bool, actions: list[list[str]]) -> str:
    actions.append(argv)
    if dry_run:
        return ""
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RolloutError("command_failed:" + " ".join(argv[:3]))
    return result.stdout


def _service_pid(launchctl_print: str) -> str | None:
    match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)", launchctl_print)
    return match.group(1) if match else None


def running_plist_matches(view: PlistView, launchctl_print: str) -> bool:
    """Compare the launchd facts that `launchctl print` exposes to a plist.

    launchctl does not retain the source plist as a byte-for-byte object, so
    this deliberately compares its recorded path, executable, and launchd
    environment.  Values supplied by the service's shell-sourced env file are
    effective process environment, not launchd environment, and do not appear
    in this output.  It prevents overwriting a plist while launchd is actually
    serving a different unit (the failure mode from the October incident).
    """
    path = re.search(r"(?m)^\s*path\s*=\s*(.*?)\s*$", launchctl_print)
    program = re.search(r"(?m)^\s*program\s*=\s*(.*?)\s*$", launchctl_print)
    if path is None or program is None:
        return False
    try:
        expected_path = str(view.path.resolve(strict=True))
    except OSError:
        return False
    if (
        path.group(1).strip() != expected_path
        or program.group(1).strip() != view.arguments[0]
    ):
        return False
    running = launchctl_environment(launchctl_print)
    return all(
        running.get(name) == value for name, value in view.launchd_environment.items()
    )


def _stale_staging_link(runtime: Path) -> str | None:
    for name in (".current.rollout", ".current.rollback"):
        path = runtime / name
        if path.exists() or path.is_symlink():
            return name
    return None


def _restart_service(
    label: str,
    plist: Path,
    *,
    plist_changed: bool,
    actions: list[list[str]],
) -> None:
    domain = f"gui/{os.getuid()}"
    if plist_changed:
        _run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            dry_run=False,
            actions=actions,
        )
        _run(
            ["launchctl", "bootstrap", domain, str(plist)],
            dry_run=False,
            actions=actions,
        )
        return
    _run(
        ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
        dry_run=False,
        actions=actions,
    )


def service_runs_target(
    launchctl_print: str, target: Path, *, run: Callable = subprocess.run
) -> bool:
    """Require an actual service pid whose command resolved into target runtime."""
    pid = _service_pid(launchctl_print)
    if pid is None:
        return False
    result = run(
        ["ps", "-p", pid, "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and str(target.resolve()) in result.stdout


def wait_for_health(checks: Mapping[str, str], timeout: float) -> tuple[bool, dict]:
    deadline = time.monotonic() + timeout
    result: dict[str, dict] = {}
    while True:
        result = {
            name: {"ok": ok, **detail}
            for name, url in checks.items()
            for ok, detail in [health(url)]
        }
        if all(item["ok"] for item in result.values()):
            return True, result
        if time.monotonic() >= deadline:
            return False, result
        time.sleep(2)


def _health_checks(tailscale_url: str) -> dict[str, str]:
    return {
        "hub_loopback": "http://127.0.0.1:7080/healthz",
        "hub_tailscale": tailscale_url.rstrip("/") + "/healthz",
        "harnessd": "http://127.0.0.1:7081/healthz",
    }


def _preserve_failed_rollback(rollback_dir: Path, rollout_root: Path) -> Path:
    """Move the only original-plist copy into durable operator-visible history."""
    failed = rollout_root / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    destination = failed / _snapshot_name(datetime.now(timezone.utc))
    os.replace(rollback_dir, destination)
    return destination


def _rollback(
    *,
    runtime: Path,
    current: Path,
    original: str,
    rollback_dir: Path,
    current_plists: Mapping[str, Path],
    candidate: Mapping[str, PlistView],
    tailscale_url: str,
    health_timeout: float,
    actions: list[list[str]],
) -> dict:
    """Restore the recorded runtime and plists, preserving all failures.

    A rollback is recovery work, so a mismatch is reported but does not keep
    the tool from restoring the files that are already known good.
    """
    errors: list[str] = []
    restoration_complete = True
    staging = runtime / ".current.rollback"
    try:
        staging.symlink_to(original)
        os.replace(staging, current)
    except (Exception, KeyboardInterrupt) as exc:
        restoration_complete = False
        errors.append(f"runtime_restore:{type(exc).__name__}")
    for label in LABELS:
        previous = rollback_dir / current_plists[label].name
        if not previous.is_file():
            restoration_complete = False
            errors.append(f"plist_restore_missing:{label}")
            continue
        try:
            printed = _run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                dry_run=False,
                actions=actions,
            )
            if not running_plist_matches(candidate[label], printed):
                errors.append(f"rollback_running_plist_mismatch:{label}")
            plist_changed = previous.read_bytes() != current_plists[label].read_bytes()
            _copy_atomic(previous, current_plists[label])
            _restart_service(
                label,
                current_plists[label],
                plist_changed=plist_changed,
                actions=actions,
            )
        except (Exception, KeyboardInterrupt) as exc:
            restoration_complete = False
            errors.append(f"plist_restore:{label}:{type(exc).__name__}")
    try:
        healthy, details = wait_for_health(
            _health_checks(tailscale_url), health_timeout
        )
    except (Exception, KeyboardInterrupt) as exc:
        healthy, details = False, {"error": type(exc).__name__}
        errors.append(f"rollback_health:{type(exc).__name__}")
    result = {
        "restored": "runtime_and_plists" if restoration_complete else "incomplete",
        "healthy": healthy,
        "checks": details,
    }
    if errors:
        result["errors"] = errors
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_version", nargs="?")
    parser.add_argument("--drover-home", type=Path, default=Path.home() / ".drover")
    parser.add_argument(
        "--plist-dir", type=Path, default=Path.home() / "Library/LaunchAgents"
    )
    parser.add_argument("--candidate-plists", type=Path)
    parser.add_argument("--last-good-plists", type=Path)
    parser.add_argument("--bootstrap-last-good", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--tailscale-url")
    parser.add_argument("--device-token-env")
    parser.add_argument("--health-timeout", type=float, default=120)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    home, runtime = args.drover_home, args.drover_home / "runtime"
    last_good_path = args.last_good_plists or home / "rollout" / "last-good"
    report: dict = {
        "target_version": args.target_version,
        "dry_run": args.dry_run,
        "checks": {},
        "actions": [],
    }
    target = runtime / args.target_version if args.target_version else None
    current = runtime / "current"
    original = os.readlink(current) if current.is_symlink() else None
    marker_started = False
    mutation_started = False
    rollback_dir: Path | None = None
    exit_code = 1
    try:
        current_plists = {label: args.plist_dir / f"{label}.plist" for label in LABELS}
        if args.bootstrap_last_good:
            current_server = load_plist(current_plists["com.drover.server"])
            config_path = args.config or config_from_arguments(
                current_server, home / "config.toml"
            )
            if not config_path.is_file():
                raise RolloutError("service_config_missing")
            required = {
                label: required_environment(config_path, label) for label in LABELS
            }
            running = {
                label: launchctl_environment(
                    _run(
                        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                        dry_run=False,
                        actions=[],
                    )
                )
                for label in LABELS
            }
            snapshot = bootstrap_last_good(
                args.plist_dir,
                last_good_path.parent,
                required,
                running,
                last_good_name=last_good_path.name,
                apply=not args.dry_run,
            )
            report["result"] = (
                "last_good_bootstrapped"
                if not args.dry_run
                else "dry_run_last_good_preflight_passed"
            )
            report["snapshot"] = str(snapshot) if snapshot else None
            if args.dry_run:
                report["actions"] = [["snapshot", str(last_good_path)]]
            print(json.dumps(report, sort_keys=True))
            return 0
        if (
            not args.target_version
            or args.candidate_plists is None
            or not args.tailscale_url
        ):
            parser.error(
                "target_version, --candidate-plists, and --tailscale-url are required"
            )
        stale = _stale_staging_link(runtime)
        if stale:
            raise RolloutError(f"stale_runtime_staging_link:{stale}")
        candidate = {
            label: load_plist(args.candidate_plists / f"{label}.plist")
            for label in LABELS
        }
        deployed = {label: load_plist(current_plists[label]) for label in LABELS}
        last_good = {
            label: load_plist(last_good_path / f"{label}.plist") for label in LABELS
        }
        if any(
            candidate[label].label != label
            or deployed[label].label != label
            or last_good[label].label != label
            for label in LABELS
        ):
            raise RolloutError("plist_label_mismatch")
        if not original or not (runtime / original).is_dir():
            raise RolloutError("current_runtime_missing")
        config_path = args.config or config_from_arguments(
            candidate["com.drover.server"], home / "config.toml"
        )
        if not config_path.is_file():
            raise RolloutError("service_config_missing")
        required = {label: required_environment(config_path, label) for label in LABELS}
        for label in LABELS:
            running = _run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                dry_run=False,
                actions=[],
            )
            matches = running_plist_matches(deployed[label], running)
            report["checks"][f"{label}:running_plist"] = {"ok": matches}
            if not matches:
                raise RolloutError(f"running_plist_mismatch:{label}")
            report["checks"][f"{label}:environment"] = environment_diff(
                required[label],
                candidate[label].environment,
                last_good[label].environment,
                launchctl_environment(running),
            )
            if report["checks"][f"{label}:environment"]["missing_required"]:
                raise RolloutError(f"required_environment_missing:{label}")
        from drover.config import load_config

        cfg = load_config(config_path)
        report["checks"]["runtime"] = runtime_preflight(
            target, cfg.analytics.engine_sha256
        )
        free = shutil.disk_usage(home).free
        report["checks"]["disk"] = {"free_bytes": free, "minimum_bytes": MIN_FREE_BYTES}
        if free < MIN_FREE_BYTES:
            raise RolloutError("insufficient_disk")
        if cfg.control_store.backend != "postgres":
            raise RolloutError("control_store_not_postgres")
        control_dsn = deployed["com.drover.server"].environment.get(
            cfg.control_store.dsn_env, ""
        )
        if not control_dsn:
            raise RolloutError("control_store_dsn_missing")
        systemic_attempts = work_queue_attempts(cfg.work_queue)
        read_queue = lambda: local_work_queue_state(
            control_dsn,
            schema=cfg.control_store.schema,
            workspace_id=cfg.work_queue.workspace_id,
            orphan_intent_minutes=cfg.work_queue.execution.orphan_intent_minutes,
            systemic_attempts=systemic_attempts,
        )
        token = rollout_token(config_path, home, override_env=args.device_token_env)
        local = "http://127.0.0.1:7080"
        listed, hosts_before = api_json(local + "/harness/hosts", token)
        report["checks"]["relay_before"] = (
            sorted(relay_hosts(hosts_before)) if listed else hosts_before
        )
        if not listed:
            raise RolloutError("relay_inventory_unavailable")
        supplemental_queue = (
            (lambda: optional_work_queue_report(local + "/work-queue/report", token))
            if args.device_token_env
            else None
        )
        if args.dry_run:
            settled, queue_before = wait_for_work_queue(
                read_queue, timeout=0, supplemental=supplemental_queue
            )
            report["checks"]["work_queue_before"] = queue_before
            if not settled:
                raise RolloutError("work_queue_launches_not_settled")
            _, details = wait_for_health(_health_checks(args.tailscale_url), timeout=0)
            report["checks"]["health_before"] = details
            restart_actions = []
            for label in LABELS:
                if (
                    candidate[label].path.read_bytes()
                    != current_plists[label].read_bytes()
                ):
                    restart_actions.extend(
                        [
                            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                            [
                                "launchctl",
                                "bootstrap",
                                f"gui/{os.getuid()}",
                                str(current_plists[label]),
                            ],
                        ]
                    )
                else:
                    restart_actions.append(
                        [
                            "launchctl",
                            "kickstart",
                            "-k",
                            f"gui/{os.getuid()}/{label}",
                        ]
                    )
            report["actions"] = [
                [str(current / "bin/drover-server"), "planned-stop", "begin"],
                ["copy", "candidate plists atomically"],
                ["flip", str(current), args.target_version],
                *restart_actions,
                ["snapshot", "deployed plists as last-good"],
                [str(target / "bin/drover-server"), "planned-stop", "complete"],
            ]
            report["result"] = "dry_run_preflight_passed"
            print(json.dumps(report, sort_keys=True))
            return 0

        # This marker is the fence consumed by check-in/recovery paths.
        _run(
            [
                str(current / "bin/drover-server"),
                "--config",
                str(config_path),
                "planned-stop",
                "begin",
                "--step",
                "studio-rollout",
                "--actor",
                "studio-rollout",
            ],
            dry_run=False,
            actions=report["actions"],
        )
        marker_started = True
        settled, queue_before = wait_for_work_queue(
            read_queue, args.health_timeout, supplemental=supplemental_queue
        )
        report["checks"]["work_queue_before"] = queue_before
        if not settled:
            raise RolloutError("work_queue_launches_not_settled")
        # Re-read launchctl immediately before copying: a deployment may have
        # raced the preflight and changed what launchd is serving.
        for label in LABELS:
            printed = _run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                dry_run=False,
                actions=report["actions"],
            )
            if not running_plist_matches(deployed[label], printed):
                raise RolloutError(f"running_plist_mismatch:{label}")
        rollback_dir = Path(tempfile.mkdtemp(prefix=".studio-rollout-", dir=runtime))
        mutation_started = True
        for label in LABELS:
            _copy_atomic(
                current_plists[label], rollback_dir / current_plists[label].name
            )
        plist_changed = {
            label: candidate[label].path.read_bytes()
            != current_plists[label].read_bytes()
            for label in LABELS
        }
        for label in LABELS:
            _copy_atomic(candidate[label].path, current_plists[label])
        staging = runtime / ".current.rollout"
        staging.symlink_to(args.target_version)
        os.replace(staging, current)
        for label in LABELS:
            _restart_service(
                label,
                current_plists[label],
                plist_changed=plist_changed[label],
                actions=report["actions"],
            )
        if not runtime_points_to(current, target):
            raise RolloutError("runtime_repointed_after_harnessd_restart")
        ok, details = wait_for_health(
            _health_checks(args.tailscale_url), args.health_timeout
        )
        report["checks"]["health"] = details
        if not ok:
            raise RolloutError("health_check_failed")
        for label in LABELS:
            printed = _run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                dry_run=False,
                actions=report["actions"],
            )
            if not service_runs_target(printed, target):
                raise RolloutError(f"service_not_running_target:{label}")
        settled, queue_after = wait_for_work_queue(
            read_queue, args.health_timeout, supplemental=supplemental_queue
        )
        report["checks"]["work_queue_after"] = queue_after
        if not settled:
            raise RolloutError("work_queue_launches_not_settled_after_switch")
        relayed, hosts_after = api_json(local + "/harness/hosts", token)
        relay_ok, missing_relays = relay_reconnected(
            hosts_after, relay_hosts(hosts_before)
        )
        report["checks"]["relay_reconnect"] = {
            "ok": relayed and relay_ok,
            "missing": missing_relays,
        }
        if not relayed or not relay_ok:
            raise RolloutError("relay_reconnect_failed")
        report["last_good_snapshot"] = str(
            record_last_good(
                args.plist_dir,
                last_good_path.parent,
                last_good_name=last_good_path.name,
            )
        )
        report["result"] = "rolled_out"
        exit_code = 0
    except (Exception, KeyboardInterrupt) as exc:
        report["failure"] = (
            str(exc)
            if isinstance(exc, RolloutError)
            else f"unexpected_exception:{type(exc).__name__}"
        )
        if mutation_started and rollback_dir is not None:
            if original:
                try:
                    report["rollback"] = _rollback(
                        runtime=runtime,
                        current=current,
                        original=original,
                        rollback_dir=rollback_dir,
                        current_plists=current_plists,
                        candidate=candidate,
                        tailscale_url=args.tailscale_url,
                        health_timeout=args.health_timeout,
                        actions=report["actions"],
                    )
                except (Exception, KeyboardInterrupt) as rollback_exc:
                    report["rollback"] = {
                        "restored": "incomplete",
                        "errors": [type(rollback_exc).__name__],
                    }
            else:
                report["rollback"] = {
                    "restored": "skipped",
                    "errors": ["original_runtime_unavailable"],
                }
        exit_code = 1
    finally:
        if marker_started:
            try:
                _run(
                    [
                        str(current / "bin/drover-server"),
                        "--config",
                        str(config_path),
                        "planned-stop",
                        "complete",
                    ],
                    dry_run=False,
                    actions=report["actions"],
                )
                report["checks"]["planned_stop_clear"] = {"ok": True}
            except (Exception, KeyboardInterrupt) as clear_exc:
                report["checks"]["planned_stop_clear"] = {
                    "ok": False,
                    "error": type(clear_exc).__name__,
                }
                report.setdefault("failure", "planned_stop_clear_failed")
                exit_code = 1
        if rollback_dir is not None and rollback_dir.exists():
            rollback = report.get("rollback", {})
            clean_rollout = exit_code == 0
            complete_rollback = rollback.get("restored") == "runtime_and_plists"
            if clean_rollout or complete_rollback:
                shutil.rmtree(rollback_dir, ignore_errors=True)
            else:
                try:
                    preserved = _preserve_failed_rollback(
                        rollback_dir, home / "rollout"
                    )
                    report.setdefault("rollback", {"restored": "skipped"})[
                        "preserved_snapshot"
                    ] = str(preserved)
                except (Exception, KeyboardInterrupt) as preserve_exc:
                    rollback_report = report.setdefault(
                        "rollback", {"restored": "incomplete"}
                    )
                    rollback_report["preserved_snapshot"] = str(rollback_dir)
                    rollback_report["preserve_error"] = type(preserve_exc).__name__
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
