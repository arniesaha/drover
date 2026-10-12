"""Descriptor-relative folder operations within operator allowed roots."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path

MAX_ENTRIES = 100
MAX_SCANNED = 5_000
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class FolderError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def allowed_roots() -> list[Path]:
    configured = os.environ.get("DROVER_FOLDER_ROOTS")
    candidates = (
        configured.split(os.pathsep) if configured is not None else [str(Path.home())]
    )
    roots = []
    for candidate in candidates:
        if not candidate.strip():
            continue
        try:
            root = Path(candidate).expanduser()
            if not root.is_absolute() or ".." in root.parts:
                continue
            # Never canonicalize through a link: a replaced root must not
            # turn its new target into an authorized location.
            with _directory(root):
                if root not in roots:
                    roots.append(root)
        except (OSError, ValueError, RuntimeError, FolderError):
            continue
    return roots


@contextmanager
def _directory(path: Path):
    """Open every component without following links, including root ancestors."""
    fd = os.open("/", _FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(component, _FLAGS, dir_fd=fd)
            except OSError:
                # O_NOFOLLOW|O_DIRECTORY reports ENOTDIR for a symlink on Linux.
                if stat.S_ISLNK(
                    os.stat(component, dir_fd=fd, follow_symlinks=False).st_mode
                ):
                    raise FolderError(403, "symlink_not_allowed") from None
                raise
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _authorized_path(
    typed: str, roots: list[Path], query: str = ""
) -> tuple[Path, Path]:
    if len(typed) > 4096 or "\x00" in typed or len(query) > 256:
        raise FolderError(400, "invalid_path")
    expanded = (
        os.path.expanduser(typed) if typed == "~" or typed.startswith("~/") else typed
    )
    if ".." in expanded.split("/"):
        raise FolderError(403, "outside_allowed_folders")
    path = Path(expanded)
    if not path.is_absolute():
        raise FolderError(400, "invalid_path")
    root = next(
        (
            r
            for r in sorted(roots, key=lambda r: len(r.parts), reverse=True)
            if path.is_relative_to(r)
        ),
        None,
    )
    if root is None:
        raise FolderError(403, "outside_allowed_folders")
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        raise FolderError(403, "hidden_folder_not_allowed")
    return path, root


def list_folders(
    typed: str, query: str = "", cursor: str = "", limit: int = MAX_ENTRIES
) -> dict:
    if not isinstance(limit, int) or not 1 <= limit <= MAX_ENTRIES:
        raise FolderError(400, "invalid_limit")
    if len(cursor) > 2048:
        raise FolderError(400, "invalid_cursor")
    roots = allowed_roots()
    locations = [
        {
            "name": "Home" if root == Path.home() else root.name or "Root",
            "path": str(root),
        }
        for root in roots
    ]
    if not typed:
        return {
            "roots": locations,
            "path": None,
            "parent": None,
            "entries": [],
            "truncated": False,
            "next_cursor": None,
        }
    path, root = _authorized_path(typed, roots, query)
    entries = []
    truncated = False
    next_cursor = None
    try:
        with _directory(path) as fd, os.scandir(fd) as children:
            info = os.fstat(fd)
            identity = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
            binding = hashlib.sha256(
                json.dumps([str(path), query, identity]).encode()
            ).hexdigest()
            offset = 0
            if cursor:
                try:
                    data = json.loads(base64.urlsafe_b64decode(cursor))
                    offset = data["offset"]
                    if (
                        type(offset) is not int
                        or offset < 0
                        or data["binding"] != binding
                    ):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise FolderError(400, "invalid_cursor") from None
            # Resume enumeration without storing the entire directory. Earlier
            # names are skipped without stat calls; only this page gets metadata.
            for index, child in enumerate(children):
                if index < offset:
                    continue
                if index - offset >= MAX_SCANNED or len(entries) >= limit:
                    truncated = True
                    next_cursor = base64.urlsafe_b64encode(
                        json.dumps({"offset": index, "binding": binding}).encode()
                    ).decode()
                    break
                name = child.name
                if name.startswith(".") or query.casefold() not in name.casefold():
                    continue
                if not child.is_dir(follow_symlinks=False):
                    continue
                try:
                    child_fd = os.open(name, _FLAGS, dir_fd=fd)
                except OSError:
                    # Unreadable folders still appear, but unsafe links never do.
                    try:
                        mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                    except OSError:
                        continue  # deleted or inaccessible during enumeration
                    if not stat.S_ISDIR(mode):
                        continue
                    is_git = False
                else:
                    try:
                        try:
                            git_mode = os.stat(
                                ".git", dir_fd=child_fd, follow_symlinks=False
                            ).st_mode
                            is_git = stat.S_ISDIR(git_mode) or stat.S_ISREG(git_mode)
                        except OSError:
                            is_git = False
                    finally:
                        os.close(child_fd)
                entries.append(
                    {
                        "name": name,
                        "path": str(path / name),
                        "is_dir": True,
                        "is_git_repo": is_git,
                        "hidden": False,
                    }
                )
    except FolderError:
        raise
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM, errno.ELOOP}:
            raise FolderError(403, "permission_denied") from None
        if exc.errno == errno.ENOTDIR:
            raise FolderError(400, "not_directory") from None
        if exc.errno == errno.ENOENT:
            raise FolderError(400, "not_found") from None
        raise FolderError(400, "unavailable") from None
    entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    return {
        "roots": locations,
        "path": str(path),
        "parent": str(path.parent) if path != root else None,
        "entries": entries,
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def create_folder(typed: str, name: str) -> dict:
    if (
        not isinstance(typed, str)
        or not isinstance(name, str)
        or not name
        or name != name.strip()
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in name)
        or len(name.encode("utf-8")) > 255
    ):
        raise FolderError(400, "invalid_name")
    path, _ = _authorized_path(typed, allowed_roots())
    try:
        with _directory(path) as fd:
            # mkdir is atomic and never replaces a file, directory, or link.
            os.mkdir(name, mode=0o700, dir_fd=fd)
    except FolderError:
        raise
    except FileExistsError:
        raise FolderError(409, "folder_exists") from None
    except PermissionError:
        raise FolderError(403, "permission_denied") from None
    except OSError as exc:
        code = "not_found" if exc.errno == errno.ENOENT else "unavailable"
        raise FolderError(400, code) from None
    return {"name": name, "path": str(path / name)}
