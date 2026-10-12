#!/usr/bin/env python3
"""NAS-side verification and retention. Uploaded by studio_backup.py over SSH."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

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


def main():
    action, root_arg = sys.argv[1:]
    root = Path(root_arg)
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("symlink backup root")
    if action == "prepare":
        date = json.load(sys.stdin)["date"]
        dt.date.fromisoformat(date)
        final, pending = root / date, root / (".pending-" + date)
        if final.exists() or pending.exists():
            raise ValueError("generation already exists; inspect it before retry")
        pending.mkdir(mode=0o700)
        previous = sorted(p.name for p in root.iterdir()
                          if DATE.fullmatch(p.name) and not p.is_symlink()
                          and (p / "SUCCESS.json").is_file())
        print(json.dumps({"previous": previous[-1] if previous else None}))
    elif action == "verify":
        request = json.load(sys.stdin)
        pending = root / (".pending-" + request["date"])
        total = 0
        for entry in request["files"]:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid manifest path")
            path = pending / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing artifact")
            if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
                raise ValueError("checksum mismatch")
            if path.stat().st_mode & 0o077:
                raise ValueError("artifact permissions are not private")
            total += entry["size"]
        with (pending / "MANIFEST.json").open("w") as f:
            json.dump(request, f, indent=2)
        print(json.dumps({"verified_files": len(request["files"]), "verified_bytes": total}))
    elif action == "publish":
        receipt = json.load(sys.stdin)
        pending = root / (".pending-" + receipt["date"])
        # Only the caller's successful read-back pg_restore permits publication.
        if not receipt["nas_pg_restore_list"] or not (pending / "MANIFEST.json").is_file():
            raise ValueError("verification incomplete")
        with (pending / "SUCCESS.json").open("w") as f:
            json.dump(receipt, f, indent=2)
        pending.rename(root / receipt["date"])
        dates = [p.name for p in root.iterdir()
                 if DATE.fullmatch(p.name) and not p.is_symlink()
                 and (p / "SUCCESS.json").is_file()]
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
