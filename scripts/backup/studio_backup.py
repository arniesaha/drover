#!/usr/bin/env python3
"""Private nightly Studio snapshots. No service restarts; fail on any failed step."""

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".drover/backup-state"
LOG = HOME / "Library/Logs/drover/backup.log"
PG = Path("/opt/homebrew/opt/postgresql@17/bin")
PG_ARGS = [
    "-h",
    str(HOME / ".drover-postgres-fresh-test"),
    "-p",
    "15432",
    "-U",
    "arnab",
]
NAS = ""  # Loaded from private machine configuration when a backup starts.
SSH = [
    "/usr/bin/ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
    "-i",
    str(HOME / ".ssh/id_ed25519_mac_mini_migration"),
]
RSYNC = "/opt/homebrew/bin/rsync"
STUDIO_FROM_NAS = "drover-studio"  # NAS ~/.ssh/config alias for this Mac
DATA = {
    "drover-data": Path("/Volumes/M2 1/drover-data"),
    "drover-lakes": Path("/Volumes/M2 1/drover-lakes"),
}
EXCLUDES = [
    "/worktrees",
    "/rollout-worktrees",
    "/runtime",
    "/incoming",
    "/backup-state",
    "*.duckdb",
    "*.duckdb.wal",
    "*.sock",
]
RISK = (
    "Registry DuckDB and WAL are live file copies, not transactional exports. "
    "Postgres databases have separate consistent dump snapshots. Live lake copies "
    "are taken before and after dumps without deleting the first pass; cross-store "
    "atomicity and DuckLake catalog reference completeness are not proven."
)


def log(message):
    with LOG.open("a") as f:
        f.write(dt.datetime.now().astimezone().isoformat() + " " + message + "\n")


def run(args, *, input_data=None, output=None):
    # Child diagnostics may contain private configuration. Never emit them in logs.
    result = subprocess.run(
        args,
        input=input_data,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE,
        umask=0o077,
    )
    if result.returncode:
        raise RuntimeError("command failed (exit %d)" % result.returncode)
    return result.stdout


def remote(helper, action, root, payload):
    command = "python3 -c %s %s %s" % tuple(map(shlex.quote, (helper, action, root)))
    return json.loads(
        run(SSH + [NAS, command], input_data=json.dumps(payload).encode())
    )


def sync(source, destination, previous=None, excludes=()):
    # UGOS ships a patched rsync that refuses non-shared-folder paths when it
    # runs as the receiving server, so for NAS targets the NAS pulls instead.
    if destination.startswith(NAS + ":"):
        remote_dest = destination[len(NAS) + 1 :]
        args = [
            "rsync",
            "-a",
            "--no-owner",
            "--no-group",
            "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
            "--rsync-path=" + RSYNC,
        ]
        if previous:
            args.append("--link-dest=" + previous)
        args.extend("--exclude=" + item for item in excludes)
        args += [STUDIO_FROM_NAS + ":" + str(source) + "/", remote_dest + "/"]
        # The NAS home ACL leaves pulled files 0777; make the tree private again.
        run(
            SSH
            + [
                NAS,
                "umask 077; mkdir -p "
                + shlex.quote(remote_dest)
                + " && "
                + shlex.join(args)
                + " && chmod -R go= "
                + shlex.quote(remote_dest),
            ]
        )
        return
    args = [
        RSYNC,
        "-a",
        "--no-owner",
        "--no-group",
        "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
        "-e",
        shlex.join(SSH),
    ]
    if previous:
        args.append("--link-dest=" + previous)
    args.extend("--exclude=" + item for item in excludes)
    run(args + [str(source) + "/", destination + "/"])


def file_entries(root, prefix):
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        before = path.stat()
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("source changed during hashing")
        entries.append(
            {
                "path": prefix + "/" + path.relative_to(root).as_posix(),
                "size": after.st_size,
                "sha256": h.hexdigest(),
            }
        )
    return entries


def copy_registry(destination):
    # Retry if metadata changes during the copy. This does not prove transactional
    # consistency of a live database, so the receipt always carries the warning.
    source = HOME / ".drover/drover.registry.duckdb"
    if not source.is_file():
        raise RuntimeError("registry missing")
    paths = [source, Path(str(source) + ".wal")]

    def signature():
        return [
            (p.name, p.stat().st_size, p.stat().st_mtime_ns)
            for p in paths
            if p.exists()
        ]

    for _ in range(3):
        before = signature()
        for p in destination.iterdir():
            p.unlink()
        for p in paths:
            if p.exists():
                shutil.copyfile(p, destination / p.name)
                (destination / p.name).chmod(0o600)
        if before == signature():
            return
    raise RuntimeError("registry changed during all copy attempts")


def backup():
    global NAS
    config = json.loads((HOME / ".drover/maintenance/backup-config.json").read_text())
    NAS = config["nas"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", NAS):
        raise RuntimeError("invalid NAS SSH target")
    started = time.monotonic()
    date = dt.date.today().isoformat()
    host = re.sub(r"[^A-Za-z0-9._-]", "_", socket.gethostname())
    # The storage home can differ from the SSH login account on UGOS.
    target = config.get("target_root", "/home/Arnab/backups/drover")
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or ".." in Path(target).parts
    ):
        raise RuntimeError("invalid NAS backup root")
    root = str(Path(target) / host)
    helper = Path(__file__).with_name("nas_snapshot.py").read_text()
    for source in DATA.values():
        if not source.is_dir():
            raise RuntimeError("required M2 data root is missing")
    log("stage=NAS_preflight")
    preparation = remote(helper, "prepare", root, {"date": date})
    pending = root + "/.pending-" + date
    previous = root + "/" + preparation["previous"] if preparation["previous"] else None
    with tempfile.TemporaryDirectory(prefix="stage-", dir=STATE) as staging:
        stage = Path(staging)
        for name in ("postgres", "configs", "launchagents", "registry"):
            (stage / name).mkdir(mode=0o700)
        log("stage=lake_first_pass")
        for name, source in DATA.items():
            sync(
                source,
                NAS + ":" + pending + "/" + name,
                previous + "/" + name if previous else None,
            )
        log("stage=postgres_dumps")
        databases = (
            run(
                [str(PG / "psql")]
                + PG_ARGS
                + [
                    "-d",
                    "postgres",
                    "-XAt",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname",
                ]
            )
            .decode()
            .splitlines()
        )
        if not databases:
            raise RuntimeError("no databases found")
        with (stage / "postgres/globals.sql").open("wb") as f:
            run([str(PG / "pg_dumpall")] + PG_ARGS + ["--globals-only"], output=f)
        database_files = []
        for index, database in enumerate(databases):
            dump = stage / ("postgres/db-%02d.dump" % index)
            run(
                [str(PG / "pg_dump")]
                + PG_ARGS
                + ["-Fc", "-d", database, "-f", str(dump)]
            )
            with dump.with_suffix(".list").open("wb") as f:
                run([str(PG / "pg_restore"), "--list", str(dump)], output=f)
            database_files.append({"database": database, "file": dump.name})
        (stage / "postgres/databases.json").write_text(
            json.dumps(database_files, indent=2)
        )
        for name in (
            "postgresql.conf",
            "postgresql.auto.conf",
            "pg_hba.conf",
            "pg_ident.conf",
        ):
            source = HOME / ".drover-postgres-fresh-test" / name
            if source.is_file():
                shutil.copyfile(source, stage / "postgres" / name)
        log("stage=configs_and_registry")
        sync(HOME / ".drover", str(stage / "configs"), excludes=EXCLUDES)
        sync(HOME / "Library/LaunchAgents", str(stage / "launchagents"))
        copy_registry(stage / "registry")
        (stage / "CONSISTENCY.txt").write_text(RISK + "\n")
        for path in stage.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        entries = file_entries(stage, "metadata")
        # Every dump and copied configuration is hashed before transfer.
        sync(
            stage,
            NAS + ":" + pending + "/metadata",
            previous + "/metadata" if previous else None,
        )
        log("stage=lake_second_pass_and_hashes")
        for name, source in DATA.items():
            sync(
                source,
                NAS + ":" + pending + "/" + name,
                previous + "/" + name if previous else None,
            )
            entries.extend(file_entries(source, name))
        log("stage=NAS_checksum_verification")
        verification = remote(helper, "verify", root, {"date": date, "files": entries})
        # Read every archive back from NAS and list it with the matching local tool.
        # NAS does not need PostgreSQL installed. No production restore is performed.
        log("stage=NAS_pg_restore_list")
        for item in database_files:
            returned = stage / "nas-readback.dump"
            with returned.open("wb") as f:
                run(
                    SSH
                    + [
                        NAS,
                        "cat "
                        + shlex.quote(pending + "/metadata/postgres/" + item["file"]),
                    ],
                    output=f,
                )
            run([str(PG / "pg_restore"), "--list", str(returned)])
            returned.unlink()
        receipt = {
            "date": date,
            "host": host,
            "nas_path": root + "/" + date,
            "completed_at": dt.datetime.now().astimezone().isoformat(),
            "completed_epoch": time.time(),
            "duration_s": round(time.monotonic() - started, 2),
            "databases": len(databases),
            "nas_pg_restore_list": True,
            "consistency_risk": RISK,
            **verification,
        }
        retention = remote(helper, "publish", root, receipt)
        temporary = STATE / "last-success.json.tmp"
        temporary.write_text(json.dumps(receipt, indent=2))
        temporary.replace(STATE / "last-success.json")
        log("SUCCESS " + json.dumps({**receipt, "retention": retention}))


def main():
    os.umask(0o077)
    # Keep the Mac awake for the run; the Studio sleeps after 1 min idle.
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.touch(mode=0o600, exist_ok=True)
    LOG.chmod(0o600)
    with (STATE / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("FAILURE backup already running")
            return 1
        try:
            backup()
        except Exception as error:
            # Do not print exception strings, paths, DSNs, or child diagnostics.
            log(
                "FAILURE type="
                + type(error).__name__
                + (
                    " (" + str(error) + ")"
                    if re.fullmatch(r"command failed \(exit -?\d+\)", str(error))
                    else ""
                )
                + "; generation unpublished; inspect NAS pending directory"
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
