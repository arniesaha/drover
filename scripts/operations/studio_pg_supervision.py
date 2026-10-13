#!/opt/homebrew/bin/python3
"""Explicitly scheduled supervision transfer for the verified Studio cluster.

No service mutations without --execute. --prepare stages files, never loads jobs.
Diagnostics intentionally omit command output and credentials.
"""

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".drover/maintenance"
DATA = HOME / ".drover-postgres-fresh-test"
BIN = Path("/opt/homebrew/opt/postgresql@17/bin")
CELLAR = Path("/opt/homebrew/Cellar/postgresql@17/17.11/bin/postgres")
AGENTS = HOME / "Library/LaunchAgents"
PG = AGENTS / "com.drover.postgres.plist"
HUB = AGENTS / "com.drover.server.plist"
WAIT = ROOT / "wait-for-postgres.sh"
DOMAIN = f"gui/{os.getuid()}"
LOG = DATA / "postgres.log"
ACTIVE = ROOT / "pg-supervision-active.json"
EXPECTED = ROOT / "pg-supervision-prepared.json"


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(args, timeout=30, stdout=subprocess.PIPE):
    result = subprocess.run(
        [str(a) for a in args],
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    require(
        result.returncode == 0, f"{Path(str(args[0])).name} failed (output suppressed)"
    )
    return result.stdout


def atomic(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=path.name + ".", delete=False
    ) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            tmp.chmod(mode)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save(path, value):
    atomic(path, json.dumps(value, indent=2).encode() + b"\n")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sql(statement, db="postgres"):
    # Socket authentication, no DSN or password in argv or output.
    return (
        run(
            [
                BIN / "psql",
                "-X",
                "-w",
                "-h",
                DATA,
                "-p",
                "15432",
                "-U",
                "arnab",
                "-d",
                db,
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                statement,
            ]
        )
        .decode()
        .strip()
    )


def identity():
    require(
        sql(
            "select current_setting('data_directory') || '|' || "
            "current_setting('port') || '|' || current_user || '|' || "
            "(select rolsuper::text from pg_roles where rolname=current_user)"
        )
        == f"{DATA}|15432|arnab|true",
        "Cluster identity or administrator mismatch",
    )


def live_pid():
    try:
        return int((DATA / "postmaster.pid").read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def loaded(label):
    result = subprocess.run(
        ["launchctl", "list", label],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if result.returncode:
        return None
    # launchctl list uses OpenStep dictionary syntax, not XML plist syntax.
    text = result.stdout.decode()
    match = re.search(r'"PID"\s*=\s*(\d+)', text)
    job = {"PID": int(match.group(1)) if match else None}
    detail = subprocess.run(
        ["launchctl", "print", DOMAIN + "/" + label],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    match = re.search(r"^\s*runs = (\d+)", detail.stdout.decode(), re.M)
    job["Runs"] = int(match.group(1)) if match else None
    return job


def tcp_open():
    try:
        with socket.create_connection(("127.0.0.1", 15432), timeout=1):
            return True
    except OSError:
        return False


def wait(condition, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(1)
    raise RuntimeError(message)


def pg_ready():
    if not tcp_open():
        return False
    try:
        run([BIN / "pg_isready", "-h", "127.0.0.1", "-p", "15432"], timeout=5)
        identity()
        return sql("select 1") == "1"
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def http_ready():
    # Local requests only, no proxies, redirects, bearer tokens or response logging.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        for endpoint in ("healthz", "readyz"):
            with opener.open("http://127.0.0.1:7080/" + endpoint, timeout=5) as r:
                body = r.read(2 * 1024 * 1024 + 1)
                if r.status != 200 or len(body) > 2 * 1024 * 1024:
                    return False
                if endpoint == "healthz" and body.strip() != b"ok\nanalytical=ok":
                    return False
                if endpoint == "readyz" and json.loads(body).get("ready") is not True:
                    return False
        return True
    except Exception:
        return False


def probe():
    require(pg_ready() and http_ready(), "Database or hub readiness failed")
    run(
        ["/bin/zsh", HOME / ".drover/soak-probe.sh"],
        timeout=60,
        stdout=subprocess.DEVNULL,
    )
    print(
        "Read-only database, health/readiness and existing soak probe: PASS", flush=True
    )


def pg_dict():
    return {
        "Label": "com.drover.postgres",
        "ProgramArguments": [str(BIN / "postgres"), "-D", str(DATA), "-p", "15432"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 300,
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "LC_ALL": "en_US.UTF-8",
            "LANG": "en_US.UTF-8",
        },
        "StandardOutPath": str(HOME / "Library/Logs/drover/postgres.out.log"),
        "StandardErrorPath": str(HOME / "Library/Logs/drover/postgres.err.log"),
    }


WAIT_TEXT = """#!/bin/zsh
set -eu
# Prefix the ORIGINAL hub ProgramArguments with this path. Do not interpolate them.
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  if /opt/homebrew/opt/postgresql@17/bin/pg_isready -q -t 1 -h 127.0.0.1 -p 15432 &&
     /opt/homebrew/opt/postgresql@17/bin/pg_isready -q -t 1 -h "$HOME/.drover-postgres-fresh-test" -p 15432; then
    exec "$@"
  fi
  sleep 1
done
print -u2 'PostgreSQL startup wait expired'
exit 1
"""


def prepare():
    identity()
    require(not loaded("com.drover.postgres"), "Postgres agent already loaded")
    require(
        not ACTIVE.exists(), "An attempt already exists; inspect it before preparing"
    )
    hub = plistlib.loads(HUB.read_bytes())
    require(
        hub.get("Label") == "com.drover.server"
        and "Program" not in hub
        and hub.get("ProgramArguments"),
        "Unexpected hub plist shape",
    )
    require(str(WAIT) not in hub["ProgramArguments"], "Hub already wrapped")
    if PG.exists():
        require(
            plistlib.loads(PG.read_bytes()) == pg_dict(), "Existing PG plist differs"
        )
    else:
        atomic(PG, plistlib.dumps(pg_dict()))
    if WAIT.exists():
        require(WAIT.read_text() == WAIT_TEXT, "Existing wait wrapper differs")
    else:
        atomic(WAIT, WAIT_TEXT.encode(), 0o700)
    # Pin the exact original dictionary, including environment, without displaying it.
    prepared = {
        "hub_sha256": digest(HUB),
        "postgres_sha256": digest(PG),
        "wait_sha256": digest(WAIT),
        "soak_sha256": digest(HOME / ".drover/soak-probe.sh"),
        "config_hashes": {
            name: digest(DATA / name)
            for name in ("postgresql.conf", "pg_hba.conf", "pg_ident.conf")
        },
    }
    save(EXPECTED, prepared)
    validate()
    print("Prepared agent and wait wrapper; live hub plist untouched; nothing loaded")


def validate():
    require(
        plistlib.loads(PG.read_bytes()) == pg_dict(), "Postgres plist dict mismatch"
    )
    expected = json.loads(EXPECTED.read_text())
    for key, path in [
        ("postgres", PG),
        ("wait", WAIT),
        ("soak", HOME / ".drover/soak-probe.sh"),
    ]:
        require(
            digest(path) == expected[key + "_sha256"],
            f"{key} changed since preparation",
        )
    for name, value in expected["config_hashes"].items():
        require(
            digest(DATA / name) == value, "Postgres config changed since preparation"
        )
    run(["plutil", "-lint", PG])
    run(["plutil", "-lint", HUB])
    for path in [
        WAIT,
        ROOT / "pg-switchover.sh",
        ROOT / "pg-rollback.sh",
        HOME / ".drover/soak-probe.sh",
    ]:
        run(["/bin/zsh", "-n", path])


def preflight():
    validate()
    require(
        os.path.samefile(BIN / "postgres", CELLAR), "Stable binary changed from 17.11"
    )
    require(DATA.stat().st_uid == os.getuid(), "Cluster owner mismatch")
    require(not loaded("com.drover.postgres"), "Postgres agent must be unloaded")
    require(
        digest(HUB) == json.loads(EXPECTED.read_text())["hub_sha256"],
        "Hub plist changed",
    )
    require((loaded("com.drover.server") or {}).get("PID"), "Hub job not running")
    identity()
    require(alive(live_pid()), "Postmaster not running")
    # No unrecorded launch options are allowed into this transfer.
    import shlex

    require(
        shlex.split((DATA / "postmaster.opts").read_text())
        == [str(CELLAR), "-D", str(DATA)],
        "Manual postgres invocation changed",
    )
    require(LOG.is_file(), "Original postgres log missing")
    probe()
    print("Preflight PASS; no backups, stops, plist edits or loads performed")


def mark(state, stage):
    state["stage"] = stage
    state["updated_epoch"] = time.time()
    save(ACTIVE, state)
    print("Stage:", stage, flush=True)


def backup(out):
    names = json.loads(
        sql(
            "select coalesce(json_agg(datname order by datname), '[]'::json) "
            "from pg_database where not datistemplate"
        )
    )
    require(names and "postgres" in names, "Database enumeration failed")
    require(
        all(re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names),
        "Database names need explicit safe encoding; refusing backup",
    )
    files = []
    globals_file = out / "globals.sql"
    with globals_file.open("xb") as f:
        run(
            [
                BIN / "pg_dumpall",
                "-w",
                "-h",
                DATA,
                "-p",
                "15432",
                "-U",
                "arnab",
                "--globals-only",
            ],
            timeout=600,
            stdout=f,
        )
    files.append(globals_file)
    manifest = {}
    for i, name in enumerate(names):
        archive = out / f"db-{i:03d}.dump"
        with archive.open("xb") as f:
            run(
                [
                    BIN / "pg_dump",
                    "-w",
                    "-h",
                    DATA,
                    "-p",
                    "15432",
                    "-U",
                    "arnab",
                    "--format=custom",
                    "--dbname",
                    name,
                ],
                timeout=3600,
                stdout=f,
            )
        with (out / f"db-{i:03d}.list").open("xb") as f:
            run([BIN / "pg_restore", "--list", archive], timeout=60, stdout=f)
        files.extend([archive, out / f"db-{i:03d}.list"])
        manifest[archive.name] = name
    save(out / "databases.json", manifest)
    files.append(out / "databases.json")
    require(all(p.stat().st_size > 0 for p in files), "Empty backup file")
    sums = {p.name: digest(p) for p in files}
    save(out / "sha256.json", sums)
    require(
        all(digest(out / name) == value for name, value in sums.items()),
        "Backup checksum mismatch",
    )
    print(
        "Globals and all non-template DB backups, archive lists, SHA-256 verification: PASS",
        flush=True,
    )


def stop_hub():
    job = loaded("com.drover.server")
    if job:
        pid = job.get("PID")
        run(["launchctl", "bootout", DOMAIN + "/com.drover.server"], timeout=360)
        wait(
            lambda: not loaded("com.drover.server") and not alive(pid),
            360,
            "Hub did not exit",
        )


def soak_twice():
    pid = live_pid()
    pg_job = loaded("com.drover.postgres")
    job = loaded("com.drover.server")
    require(job and job.get("PID"), "Hub PID unavailable")
    for i in range(2):
        if i:
            time.sleep(30)
        probe()
        current = loaded("com.drover.server")
        require(
            live_pid() == pid
            and current
            and current.get("PID") == job["PID"]
            and current.get("Runs") == job.get("Runs"),
            "Postgres or hub restarted during soak",
        )
        current_pg = loaded("com.drover.postgres")
        require(current_pg == pg_job, "Postgres agent changed during soak")
        print(
            f"Soak {i + 1}/2 PASS; process PIDs and launchd run counts stable",
            flush=True,
        )


SWITCH_STAGES = (
    "created",
    "backing-up",
    "backup-verified",
    "stopping-hub",
    "stopping-manual-postgres",
    "starting-supervised-postgres",
    "applying-hub-wrapper",
    "starting-hub",
    "soaking",
    "complete",
)


def resume_state():
    state = json.loads(ACTIVE.read_text())
    require(
        state["stage"] in SWITCH_STAGES, "Attempt cannot resume switching; use rollback"
    )
    out = Path(state["directory"])
    require(
        out.parent == ROOT
        and out.name.startswith("pg-switch-")
        and not out.is_symlink(),
        "Unsafe state directory",
    )
    require(
        digest(out / "server.original.plist") == state["original_hub_sha256"],
        "Saved hub checksum mismatch",
    )
    validate()
    require(digest(PG) == state["postgres_sha256"], "Staged Postgres plist changed")
    require(os.path.samefile(BIN / "postgres", CELLAR), "Stable binary changed")
    for name, value in state["config_hashes"].items():
        require(
            digest(DATA / name) == value, "Postgres config changed since switchover"
        )
    original = plistlib.loads((out / "server.original.plist").read_bytes())
    patched = dict(original)
    patched["ProgramArguments"] = [str(WAIT), *original["ProgramArguments"]]
    hub = plistlib.loads(HUB.read_bytes())
    allowed = (
        (original, patched)
        if state["stage"] == "applying-hub-wrapper"
        else (
            (patched,)
            if state["stage"] in ("starting-hub", "soaking", "complete")
            else (original,)
        )
    )
    require(hub in allowed, "Hub plist changed")
    if SWITCH_STAGES.index(state["stage"]) >= SWITCH_STAGES.index("backup-verified"):
        backups = Path(state.get("backup_directory", str(out)))
        require(
            backups in (out, out / "backups") and not backups.is_symlink(),
            "Unsafe backup directory",
        )
        sums = json.loads((backups / "sha256.json").read_text())
        require(
            bool(sums)
            and all(
                Path(name).name == name and digest(backups / name) == value
                for name, value in sums.items()
            ),
            "Backup checksum mismatch",
        )
    return state


def switch():
    if ACTIVE.exists():
        state = resume_state()
        if state["stage"] == "complete":
            print("Switchover already complete; saved state retained for rollback")
            return
        out = Path(state["directory"])
    else:
        preflight()
        out = ROOT / ("pg-switch-" + time.strftime("%Y%m%dT%H%M%S"))
        out.mkdir(mode=0o700)
        atomic(out / "server.original.plist", HUB.read_bytes())
        state = {
            "directory": str(out),
            "stage": "created",
            "original_pid": live_pid(),
            "original_hub_sha256": digest(HUB),
            "postgres_sha256": digest(PG),
            "config_hashes": json.loads(EXPECTED.read_text())["config_hashes"],
        }
        mark(state, "created")
    if state["stage"] in ("created", "backing-up"):
        preflight()
        mark(state, "backing-up")
        # A retry gets clean dump files without altering the original hub backup.
        if (out / "backups").exists():
            shutil.rmtree(out / "backups")
        with tempfile.TemporaryDirectory(prefix="backup-", dir=out) as temporary:
            backup(Path(temporary))
            os.replace(temporary, out / "backups")
        state["backup_directory"] = str(out / "backups")
        mark(state, "backup-verified")
    if state["stage"] == "backup-verified":
        preflight()
        state["downtime_start_epoch"] = time.time()
        mark(state, "stopping-hub")
    if state["stage"] == "stopping-hub":
        stop_hub()
        mark(state, "stopping-manual-postgres")
    if state["stage"] == "stopping-manual-postgres":
        if live_pid():
            require(
                live_pid() == state["original_pid"],
                "Unexpected manual postmaster; refusing",
            )
            identity()
            run(
                [BIN / "pg_ctl", "-D", DATA, "stop", "-m", "fast", "-w", "-t", "300"],
                timeout=310,
            )
        wait(
            lambda: (
                not alive(state["original_pid"]) and not tcp_open() and not live_pid()
            ),
            30,
            "Original PostgreSQL shutdown not verified",
        )
        mark(state, "starting-supervised-postgres")
    if state["stage"] == "starting-supervised-postgres":
        if not loaded("com.drover.postgres"):
            require(
                not alive(state["original_pid"]) and not live_pid() and not tcp_open(),
                "Postgres still running; refusing supervised start",
            )
            run(["launchctl", "bootstrap", DOMAIN, PG])
        wait(pg_ready, 120, "Supervised PostgreSQL not ready")
        require(
            (loaded("com.drover.postgres") or {}).get("PID") == live_pid(),
            "Agent does not own postmaster",
        )
        mark(state, "applying-hub-wrapper")
    if state["stage"] in ("applying-hub-wrapper", "starting-hub", "soaking"):
        require(
            pg_ready()
            and (loaded("com.drover.postgres") or {}).get("PID") == live_pid(),
            "Supervised PostgreSQL ownership or readiness failed",
        )
    if state["stage"] == "applying-hub-wrapper":
        original = plistlib.loads((out / "server.original.plist").read_bytes())
        patched = dict(original)
        patched["ProgramArguments"] = [str(WAIT), *original["ProgramArguments"]]
        require(
            not loaded("com.drover.server"), "Hub unexpectedly loaded before wrapping"
        )
        atomic(HUB, plistlib.dumps(patched))
        run(["plutil", "-lint", HUB])
        mark(state, "starting-hub")
    if state["stage"] == "starting-hub":
        if not loaded("com.drover.server"):
            run(["launchctl", "bootstrap", DOMAIN, HUB])
        wait(http_ready, 300, "Hub readiness timed out")
        state["downtime_end_epoch"] = time.time()
        mark(state, "soaking")
    soak_twice()
    mark(state, "complete")
    print("Switchover complete; saved state retained for rollback")


def rollback(execute):
    if not ACTIVE.exists() and not execute:
        validate()
        identity()
        print("Rollback dry-run PASS; no switch attempted and no state to restore")
        return
    require(ACTIVE.exists(), "No switchover state available")
    state = json.loads(ACTIVE.read_text())
    out = Path(state["directory"])
    require(
        out.parent == ROOT and out.name.startswith("pg-switch-"),
        "Unsafe state directory",
    )
    original_file = out / "server.original.plist"
    require(
        digest(original_file) == state["original_hub_sha256"],
        "Saved hub checksum mismatch",
    )
    require(os.path.samefile(BIN / "postgres", CELLAR), "Stable binary changed")
    for name, value in state["config_hashes"].items():
        require(
            digest(DATA / name) == value, "Postgres config changed since switchover"
        )
    if not execute:
        print(
            "Rollback dry-run PASS; saved hub verified; current stage:", state["stage"]
        )
        return
    if state["stage"] == "rolled-back":
        print("Already rolled back; no service mutations")
        return
    # If no stop was attempted, rollback only closes the failed backup attempt.
    if state["stage"] in ("created", "backing-up", "backup-verified"):
        mark(state, "rolled-back")
        print("No service stops were attempted; nothing to restart")
        return
    pg_job = loaded("com.drover.postgres")
    if live_pid():
        identity()  # Reject any unrelated server before stopping the hub.
    if pg_job:
        require(pg_job.get("PID") == live_pid(), "Postgres agent PID mismatch")
    if PG.exists():
        require(digest(PG) == state["postgres_sha256"], "Staged Postgres plist changed")
    mark(state, "rolling-back")
    stop_hub()
    if pg_job:
        pid = pg_job.get("PID")
        run(["launchctl", "bootout", DOMAIN + "/com.drover.postgres"], timeout=360)
        wait(
            lambda: (
                not alive(pid)
                and not loaded("com.drover.postgres")
                and not live_pid()
                and not tcp_open()
            ),
            360,
            "Supervised PostgreSQL did not exit cleanly",
        )
    # A partial failure may leave the original manual postmaster running.
    if live_pid():
        require(
            live_pid() in (state["original_pid"], state.get("rollback_manual_pid")),
            "Unexpected manual postmaster; refusing",
        )
        identity()
    else:
        require(not tcp_open(), "Port still occupied; refusing manual start")
        run(
            [BIN / "pg_ctl", "-D", DATA, "-l", LOG, "start", "-w", "-t", "120"],
            timeout=130,
        )
    state["rollback_manual_pid"] = live_pid()
    mark(state, "manual-postgres-restored")
    wait(pg_ready, 120, "Manual PostgreSQL not ready")
    if PG.exists():
        destination = out / "com.drover.postgres.disabled.plist"
        require(not destination.exists(), "Disabled plist already exists")
        os.replace(PG, destination)
    atomic(HUB, original_file.read_bytes())
    run(["plutil", "-lint", HUB])
    run(["launchctl", "bootstrap", DOMAIN, HUB])
    wait(http_ready, 300, "Rolled-back hub not ready")
    soak_twice()
    mark(state, "rolled-back")
    print(
        "Manual supervision restored; new agent removed from LaunchAgents; data unchanged"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "switch", "rollback"])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    require(
        pwd.getpwuid(os.getuid()).pw_name == "arnab"
        and Path(pwd.getpwuid(os.getuid()).pw_dir) == HOME
        and os.getuid() == DATA.stat().st_uid,
        "Run as the verified Studio cluster owner",
    )
    require(socket.gethostname() == "Arnabs-Mac-Studio.local", "Wrong host")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with (ROOT / "pg-supervision.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another supervision operation holds the lock") from None
        if args.action == "prepare":
            require(not args.execute, "prepare never accepts --execute")
            prepare()
        elif args.action == "rollback":
            rollback(args.execute)
        elif args.execute:
            switch()
        elif ACTIVE.exists():
            state = resume_state()
            print("Resume dry-run PASS; saved stage:", state["stage"])
        else:
            preflight()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Fixed diagnostics only. Exception strings from libraries may contain secrets.
        message = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
        print("FAILED:", message, file=sys.stderr)
        print(
            "No automatic rollback. Inspect private pg-supervision-active.json; "
            "scheduled rollback: ~/.drover/maintenance/pg-rollback.sh --execute",
            file=sys.stderr,
        )
        sys.exit(1)
