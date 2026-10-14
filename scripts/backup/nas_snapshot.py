#!/usr/bin/env python3
"""NAS-side verification and retention. Uploaded by studio_backup.py over SSH."""

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def retained_dates(names):
    """Newest seven dates plus newest representative of each of four ISO weeks."""
    dates = sorted((n for n in names if DATE.fullmatch(n)), reverse=True)
    keep = set(dates[:7])
    weeks = set()
    for name in dates:
        week = dt.date.fromisoformat(name).isocalendar()[:2]
        if week not in weeks:
            if len(weeks) == 4:
                break
            weeks.add(week)
            keep.add(name)
    return keep


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pending_path(root, date):
    if not isinstance(date, str) or not DATE.fullmatch(date):
        raise ValueError("invalid generation date")
    dt.date.fromisoformat(date)
    pending = root / (".pending-" + date)
    if pending.is_symlink():
        raise ValueError("symlink pending generation")
    return pending


def preserve_failed(root, date):
    pending = pending_path(root, date)
    if not pending.exists():
        return None
    # Nanoseconds avoid collisions when the same date is retried quickly.
    failed = root / (
        ".failed-"
        + date
        + "-"
        + dt.datetime.now().strftime("%H%M%S")
        + "-"
        + str(time.time_ns())
    )
    pending.rename(failed)
    return failed.name


def manifest_path(pending, value):
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("invalid manifest path")
    path = pending / relative
    candidate = pending
    if candidate.is_symlink():
        raise ValueError("symlink manifest path")
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("symlink manifest path")
    return path


def inventory(pending, trees):
    entries = []
    for tree in trees:
        if tree not in ("drover-data", "drover-lakes"):
            raise ValueError("invalid inventory tree")
        root = manifest_path(pending, tree)
        if not root.is_dir():
            raise ValueError("missing inventory tree")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            before = path.stat()
            checksum = digest(path)
            after = path.stat()
            if (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("artifact changed during inventory")
            entries.append(
                {
                    "path": path.relative_to(pending).as_posix(),
                    "size": after.st_size,
                    "sha256": checksum,
                }
            )
    return entries


def verify(pending, request):
    # Persist expected hashes even when verification fails, for diagnosis.
    for entry in request["files"]:
        manifest_path(pending, entry["path"])
    with (pending / "MANIFEST.json").open("w") as f:
        json.dump(request, f, indent=2)
    failures = []
    total = 0
    for entry in request["files"]:
        path = manifest_path(pending, entry["path"])
        reason = None
        if not path.is_file():
            reason = "missing artifact"
        elif path.stat().st_mode & 0o077:
            reason = "artifact permissions are not private"
        else:
            before = path.stat()
            checksum = digest(path)
            after = path.stat()
            if (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                reason = "artifact changed during verification"
            elif after.st_size != entry["size"] or checksum != entry["sha256"]:
                reason = "checksum mismatch"
        if reason:
            failures.append({"path": entry["path"], "reason": reason})
        else:
            total += entry["size"]
    result = {
        "verification_ok": not failures,
        "verified_files": len(request["files"]) - len(failures),
        "verified_bytes": total,
    }
    if failures:
        result["failures"] = failures
    with (pending / "VERIFICATION.json").open("w") as f:
        json.dump(result, f, indent=2)
    return result


def main():
    action, root_arg = sys.argv[1:]
    root = Path(root_arg)
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("symlink backup root")
    if action == "prepare":
        date = json.load(sys.stdin)["date"]
        pending = pending_path(root, date)
        final = root / date
        if final.exists():
            raise ValueError("generation already exists")
        # The Studio holds its local job lock. A leftover pending generation is
        # an interrupted run; retain it and allow the same date to be retried.
        failed = preserve_failed(root, date)
        pending.mkdir(mode=0o700)
        previous = sorted(
            p.name
            for p in root.iterdir()
            if DATE.fullmatch(p.name)
            and not p.is_symlink()
            and (p / "SUCCESS.json").is_file()
        )
        print(
            json.dumps(
                {"previous": previous[-1] if previous else None, "failed": failed}
            )
        )
    elif action == "fail":
        print(
            json.dumps({"failed": preserve_failed(root, json.load(sys.stdin)["date"])})
        )
    elif action == "inventory":
        request = json.load(sys.stdin)
        pending = pending_path(root, request["date"])
        print(json.dumps({"files": inventory(pending, request["trees"])}))
    elif action == "verify":
        request = json.load(sys.stdin)
        pending = pending_path(root, request["date"])
        print(json.dumps(verify(pending, request)))
    elif action == "publish":
        receipt = json.load(sys.stdin)
        pending = pending_path(root, receipt["date"])
        # Only the caller's successful read-back pg_restore permits publication.
        if (
            not receipt["nas_pg_restore_list"]
            or not (pending / "MANIFEST.json").is_file()
            or not json.loads((pending / "VERIFICATION.json").read_text())[
                "verification_ok"
            ]
        ):
            raise ValueError("verification incomplete")
        with (pending / "SUCCESS.json").open("w") as f:
            json.dump(receipt, f, indent=2)
        pending.rename(root / receipt["date"])
        dates = [
            p.name
            for p in root.iterdir()
            if DATE.fullmatch(p.name)
            and not p.is_symlink()
            and (p / "SUCCESS.json").is_file()
        ]
        keep = retained_dates(dates)
        pruned = []
        for date in dates:
            if date not in keep:
                shutil.rmtree(root / date)
                pruned.append(date)
        print(json.dumps({"retained": sorted(keep), "pruned": sorted(pruned)}))
    else:
        raise ValueError("unknown action")


if __name__ == "__main__":
    main()
