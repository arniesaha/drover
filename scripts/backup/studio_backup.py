#!/usr/bin/env python3
"""Private nightly Studio snapshots. No service restarts; fail on any failed step."""

import argparse
import ctypes
import ctypes.util
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".drover/backup-state"
LOG = Path("/Volumes/M2 1/drover-cutover-staging/logs/backup.log")
BACKUP_CONFIG = Path("/Volumes/M2 1/drover-cutover-staging/config/backup-config.json")
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
# Apply to both raw passes. Never transfer database pages or transient locks.
LAKE_EXCLUDES = [
    "*.duckdb",
    "*.duckdb.wal",
    "*.wal",
    "*.lock",
    "*.lck",
    "*.sock",
    ".drover-snapshots",
    ".drover-backup-snapshots",
]
RISK = (
    "DuckDB stores are stable atomic DB/WAL clones checkpointed in isolation. "
    "Postgres databases have separate consistent dump snapshots. Lake manifests "
    "describe the frozen NAS copy after the final pass. Cross-store atomicity "
    "and DuckLake catalog reference completeness are not proven."
)
RECEIPT_KEY_ENV = "DROVER_NAS_SNAPSHOT_RECEIPT_KEY"
RECEIPT_PRODUCER = {"name": "studio_backup", "schema_version": 2}


def log(message):
    with LOG.open("a") as f:
        f.write(dt.datetime.now().astimezone().isoformat() + " " + message + "\n")


def sign_receipt(receipt, key):
    """Bind private local evidence to a versioned Studio backup producer."""
    if not isinstance(key, bytes) or not key:
        raise RuntimeError("NAS snapshot receipt key missing")
    signed = json.loads(json.dumps(receipt))
    producer = signed.setdefault("producer", dict(RECEIPT_PRODUCER))
    producer.pop("signature", None)
    payload = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    producer["signature"] = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return signed


def backup_manifest_identity(generation_id, entries):
    """Match the private NAS MANIFEST.json bytes before publishing a receipt."""
    request = {"generation_id": generation_id, "files": entries}
    return hashlib.sha256(json.dumps(request, indent=2).encode()).hexdigest()


def redact_diagnostic(value):
    """Keep error reasons, never command arguments, credentials or data paths."""
    text = (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )
    # Redact before taking the tail so truncation cannot expose half a secret.
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://\S+", "[redacted-url]", text)
    text = re.sub(
        r"(?i)\b(password|pgpassword|passwd|passphrase|(?:access[_-]?)?token|"
        r"(?:aws[_-]?)?secret(?:[_-]?access[_-]?key)?|api[_-]?key|authorization)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*[^\n]+", "authorization=[redacted]", text
    )
    # Quoted filenames include spaces; traceback filenames are quoted too.
    text = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", "[redacted]", text)
    for private in (
        str(HOME),
        NAS,
        STUDIO_FROM_NAS,
        *(str(path) for path in DATA.values()),
    ):
        if private:
            text = text.replace(private, "[redacted]")
    text = re.sub(r"(?:[\w.-]+@)?[\w.-]+:(?=/)", "[redacted]:", text)
    text = re.sub(r"(?:~?/|\b[\w.-]+/)[^\s,;:)]+", "[redacted-path]", text)
    text = re.sub(r"\b[\w.-]+@[\w.-]+\b", "[redacted-host]", text)
    return text.encode("utf-8")[-2048:].decode("utf-8", errors="ignore")


class CommandFailure(RuntimeError):
    def __init__(self, operation, result):
        super().__init__("command failed (exit %d)" % result.returncode)
        self.operation = operation
        self.returncode = result.returncode
        self.stderr = redact_diagnostic(result.stderr or b"")


def failure_details(error):
    if isinstance(error, CommandFailure):
        return " " + json.dumps(
            {
                "operation": error.operation,
                "exit_code": error.returncode,
                "stderr_tail": error.stderr,
            },
            ensure_ascii=True,
        )
    return ""


def run(args, *, input_data=None, output=None, operation=None, accepted_codes=()):
    operation = operation or Path(args[0]).name
    is_ssh = args[0] == SSH[0]
    offset = output.tell() if output is not None and is_ssh else None
    for attempt in range(2 if is_ssh else 1):
        result = subprocess.run(
            args,
            input=input_data,
            stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE,
            umask=0o077,
        )
        if result.returncode == 0 or result.returncode in accepted_codes:
            return result.stdout
        # Authentication and remote program failures are not transport retries.
        transient = result.returncode in (12, 255) and any(
            reason in (result.stderr or b"").lower()
            for reason in (
                b"connection reset",
                b"connection timed out",
                b"connection closed",
                b"broken pipe",
                b"no route to host",
                b"connection refused",
                b"connection unexpectedly closed",
            )
        )
        if not transient or attempt:
            raise CommandFailure(operation, result)
        log("stage=ssh_retry operation=" + operation)
        if offset is not None:
            output.seek(offset)
            output.truncate()


def remote(helper, action, root, payload):
    command = "python3 -c %s %s %s" % tuple(map(shlex.quote, (helper, action, root)))
    return json.loads(
        run(
            SSH + [NAS, command],
            input_data=json.dumps(payload).encode(),
            operation="nas_" + action,
        )
    )


def sync(source, destination, previous=None, excludes=(), *, second_pass=False):
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
        if "*.lock" in excludes:
            args.append("--include=uv.lock")
        args.extend("--exclude=" + item for item in excludes)
        args += [STUDIO_FROM_NAS + ":" + str(source) + "/", remote_dest + "/"]
        # Check rsync's status before chmod, which otherwise hides the operation
        # behind a single SSH status. Only the final raw pass permits vanished files.
        allowed = "0|24" if second_pass else "0"
        command = (
            "umask 077; mkdir -p "
            + shlex.quote(remote_dest)
            + " || exit $?; "
            + shlex.join(args)
            + "; status=$?; case $status in "
            + allowed
            + ") ;; "
            + '*) printf \'operation=rsync exit=%s\\n\' "$status" >&2; exit "$status";; esac; '
            + "chmod -R go= "
            + shlex.quote(remote_dest)
            + ' || { status=$?; printf \'operation=chmod exit=%s\\n\' "$status" >&2; exit "$status"; }'
        )
        run(
            SSH + [NAS, command],
            operation="nas_pull_second_pass" if second_pass else "nas_pull",
        )
        return
    args = [
        RSYNC,
        "-a",
        "--no-owner",
        "--no-group",
        "--links",
        "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
        "-e",
        shlex.join(SSH),
    ]
    if previous:
        args.append("--link-dest=" + previous)
    if "*.lock" in excludes:
        args.append("--include=uv.lock")
    args.extend("--exclude=" + item for item in excludes)
    run(
        args + [str(source) + "/", destination + "/"],
        accepted_codes=(24,) if second_pass else (),
    )


def file_entries(root, prefix):
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            target_bytes = target.encode("utf-8", "surrogateescape")
            entries.append(
                {
                    "path": prefix + "/" + path.relative_to(root).as_posix(),
                    "kind": "symlink",
                    "target": target,
                    "size": len(target_bytes),
                    "sha256": hashlib.sha256(target_bytes).hexdigest(),
                }
            )
            continue
        if not path.is_file():
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


def clone_file(source, destination):
    # Atomic extents are required. A streamed copy of a live database can tear.
    if sys.platform != "darwin":
        raise RuntimeError("atomic DuckDB clone unavailable")
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    clone = libc.clonefile
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 0):
        raise RuntimeError("atomic DuckDB clone failed")


def snapshot_duckdb(source, destination):
    import duckdb

    source = source.resolve(strict=True)
    wal = Path(str(source) + ".wal")

    def signature():
        result = []
        for path in (source, wal):
            try:
                stat = path.stat()
                result.append(
                    (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                )
            except FileNotFoundError:
                result.append(None)
        return result

    # clonefile requires the scratch directory to be on the source volume.
    scratch = source.parent / ".drover-backup-snapshots"
    scratch.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        copied = Path(temporary) / source.name
        copied_wal = Path(str(copied) + ".wal")
        for _ in range(4):
            copied.unlink(missing_ok=True)
            copied_wal.unlink(missing_ok=True)
            before = signature()
            try:
                clone_file(source, copied)
                if before[1] is not None:
                    clone_file(wal, copied_wal)
            except RuntimeError:
                if before != signature():
                    continue
                raise
            if before == signature():
                break
        else:
            raise RuntimeError("DuckDB changed during all snapshot attempts")
        # Replay the captured WAL and checkpoint only the private clone. Opening
        # also rejects invalid database/WAL pairs; never fall back to raw copies.
        with duckdb.connect(str(copied)) as connection:
            connection.execute("CHECKPOINT")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(copied, destination)
        destination.chmod(0o600)


def snapshot_databases(stage):
    registry = HOME / ".drover/drover.registry.duckdb"
    if not registry.is_file():
        raise RuntimeError("registry missing")
    snapshot_duckdb(registry, stage / "registry" / registry.name)
    for name, root in DATA.items():
        for source in sorted(root.rglob("*.duckdb")):
            relative = source.relative_to(root)
            if any(part in LAKE_EXCLUDES for part in relative.parts[:-1]):
                continue
            if source.is_symlink() or not source.is_file():
                continue
            snapshot_duckdb(source, stage / "duckdb" / name / relative)


def backup(generation_id=None):
    global NAS
    config_path = (
        BACKUP_CONFIG
        if BACKUP_CONFIG.exists()
        else HOME / ".drover/maintenance/backup-config.json"
    )
    config = json.loads(config_path.read_text())
    NAS = config["nas"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", NAS):
        raise RuntimeError("invalid NAS SSH target")
    # Staging state and configuration must not be copied as backup source data.
    guard = Path("/Volumes/M2 1/drover-cutover-staging/config/backup-guard.sh")
    if config_path == BACKUP_CONFIG:
        for protected in [HOME / ".drover", *DATA.values()]:
            for generated in (STATE, LOG, BACKUP_CONFIG):
                run([str(guard), "require_disjoint", str(protected), str(generated)])
    started = time.monotonic()
    date = dt.date.today().isoformat()
    generation_id = generation_id or date
    host = re.sub(r"[^A-Za-z0-9._-]", "_", socket.gethostname())
    # The storage home can differ from the SSH login account on UGOS.
    target = os.environ.get("DROVER_BACKUP_TARGET_ROOT", config.get("target_root"))
    if (
        not isinstance(target, str)
        or not target.startswith("/")
        or ".." in Path(target).parts
    ):
        raise RuntimeError("invalid NAS backup root")
    key_env = config.get("receipt_key_env", RECEIPT_KEY_ENV)
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
        raise RuntimeError("invalid NAS snapshot receipt key environment")
    receipt_key = os.environ.get(key_env, "").encode()
    if not receipt_key:
        raise RuntimeError("NAS snapshot receipt key missing")
    root = str(Path(target) / host)
    helper = Path(__file__).with_name("nas_snapshot.py").read_text()
    for source in DATA.values():
        if not source.is_dir():
            raise RuntimeError("required M2 data root is missing")
    log("stage=NAS_preflight")
    preparation = remote(helper, "prepare", root, {"generation_id": generation_id})
    try:
        backup_generation(
            helper, root, generation_id, preparation, started, host, receipt_key
        )
    except Exception:
        try:
            remote(helper, "fail", root, {"generation_id": generation_id})
            log("stage=failed_generation_preserved")
        except Exception as error:
            log("stage=failed_generation_rename_failed" + failure_details(error))
        raise


def verify_generation(helper, root, generation_id, entries):
    verification = remote(
        helper, "verify", root, {"generation_id": generation_id, "files": entries}
    )
    if verification.get("verification_ok") is not True:
        for failure in verification["failures"]:
            # JSON escaping prevents filenames injecting additional log lines.
            log("NAS_verification_failure " + json.dumps(failure, ensure_ascii=True))
        raise RuntimeError("NAS verification failed")
    return verification


def backup_generation(
    helper, root, generation_id, preparation, started, host, receipt_key
):
    pending = root + "/.pending-" + generation_id
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
                excludes=LAKE_EXCLUDES,
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
        sync(
            HOME / ".drover", str(stage / "configs"), excludes=EXCLUDES + LAKE_EXCLUDES
        )
        sync(HOME / "Library/LaunchAgents", str(stage / "launchagents"))
        snapshot_databases(stage)
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
                excludes=LAKE_EXCLUDES,
                second_pass=True,
            )
        # Sources may change immediately after rsync. Hash the now-frozen
        # destination, including files retained by the first pass, instead.
        entries.extend(
            remote(
                helper,
                "inventory",
                root,
                {"generation_id": generation_id, "trees": list(DATA)},
            )["files"]
        )
        log("stage=NAS_checksum_verification")
        verification = verify_generation(helper, root, generation_id, entries)
        local_backup_manifest_sha256 = backup_manifest_identity(generation_id, entries)
        if not hmac.compare_digest(
            verification.get("artifact_manifest_sha256", ""),
            local_backup_manifest_sha256,
        ):
            raise RuntimeError("NAS manifest identity mismatch")
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
        receipt = sign_receipt(
            {
                "date": generation_id[:10],
                "generation_id": generation_id,
                "host": host,
                "nas_path": root + "/" + generation_id,
                "completed_at": dt.datetime.now().astimezone().isoformat(),
                "completed_epoch": time.time(),
                "duration_s": round(time.monotonic() - started, 2),
                "databases": len(databases),
                "nas_pg_restore_list": True,
                "consistency_risk": RISK,
                "local_backup_manifest_sha256": local_backup_manifest_sha256,
                **verification,
            },
            receipt_key,
        )
        retention = remote(helper, "publish", root, receipt)
        temporary = STATE / "last-success.json.tmp"
        temporary.write_text(json.dumps(receipt, indent=2))
        temporary.replace(STATE / "last-success.json")
        log("SUCCESS " + json.dumps({**receipt, "retention": retention}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generation-id",
        help="Unique cutover generation (YYYY-MM-DD-<safe-suffix>); daily runs omit it.",
    )
    args = parser.parse_args(argv or [])
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
            backup(args.generation_id) if args.generation_id else backup()
        except Exception as error:
            # Arbitrary exceptions remain opaque; command failures carry only
            # bounded, redacted diagnostics with JSON escaping for single-line logs.
            log(
                "FAILURE type="
                + type(error).__name__
                + failure_details(error)
                + "; generation unpublished; inspect NAS failed or pending directory"
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
