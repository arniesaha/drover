"""Read-only, descriptor-relative folder browsing within operator allowed roots."""

from __future__ import annotations

import errno
import os
import stat
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
                    raise FolderError(403, "permission_denied") from None
                raise
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def list_folders(typed: str, query: str = "") -> dict:
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
        }
    if len(typed) > 4096 or "\x00" in typed or len(query) > 256:
        raise FolderError(400, "invalid_path")
    expanded = (
        os.path.expanduser(typed) if typed == "~" or typed.startswith("~/") else typed
    )
    if ".." in expanded.split("/"):
        raise FolderError(403, "permission_denied")
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
    if root is None or any(
        part.startswith(".") for part in path.relative_to(root).parts
    ):
        raise FolderError(403, "permission_denied")
    entries = []
    truncated = False
    try:
        with _directory(path) as fd, os.scandir(fd) as children:
            for index, child in enumerate(children):
                if index >= MAX_SCANNED:
                    truncated = True
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
        "entries": entries[:MAX_ENTRIES],
        "truncated": truncated or len(entries) > MAX_ENTRIES,
    }
