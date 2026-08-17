"""HTTP host daemon for the Drover command plane."""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from drover.config import config_home, default_token_file, resolve_api_token_env
from drover.server.harness.auth import (
    AuthFlowInputError,
    AuthFlowLaunchError,
    AuthFlowManager,
    TerminalSignInRequired,
    default_auth_adapters,
    default_login_shell,
    executable_path_prefix,
    resolve_executable,
)
from drover.server.harness.content_consent import DurableContentConsent
from drover.server.harness.events import normalize_harness_event
from drover.server.harness.model_catalog import (
    CatalogSelectionError,
    ModelCatalogService,
    default_model_catalog_service,
)
from drover.server.harness.models import HarnessEvent
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.relay_client import RelayClient
from drover.server.harness.structured import agy as _structured_agy
from drover.server.harness.structured import claude as _structured_claude
from drover.server.harness.structured import codex as _structured_codex
from drover.server.harness.structured import deepseek as _structured_deepseek
from drover.server.harness.structured.manager import StructuredSessionManager
from drover.server.harness.structured.pusher import EventPusher, reconcile_unsent_events
from drover.server.harness.updater import (
    REGISTRATION_DEADLINE_SECONDS,
    HostUpdater,
    default_restarter,
    verify_after_restart,
)
from drover.server.harness.websocket import (
    WebSocketClosed,
    accept_key,
    recv_json,
    send_close,
    send_json,
)
from drover.server.harness.worktree import (
    SessionWorktree,
    cleanup_session_worktree,
    create_session_worktree,
)
from drover.server.providers.inventory import DetectedProvider, detect_provider_accounts
from drover.server.runtime import RuntimeLayout

if TYPE_CHECKING:
    from drover.config import AdvisoryContentConfig, DroverConfig
    from drover.server.providers.codex import CodexUsageProbe
    from drover.server.providers.types import (
        ProviderAccountSnapshot,
        ProviderUsageWindow,
    )

# Used only to compute a human-readable "command" label for the registry row
# when the caller didn't supply an explicit command -- the manager itself
# resolves the real default command independently via its own _FACTORIES.
_STRUCTURED_DEFAULT_COMMANDS: dict[str, Callable[[], list[str]]] = {
    "claude-code": _structured_claude.default_command,
    "codex": _structured_codex.default_command,
    "agy": _structured_agy.default_command,
    "deepseek-harness": _structured_deepseek.default_command,
}

# Harnesses whose structured drivers run full-auto with no wire-level
# approval channel (codex: --sandbox danger-full-access; agy:
# --dangerously-skip-permissions). These get a per-session git worktree so a broad
# `git add -A` inside the session can never sweep unrelated in-flight
# changes from the user's main checkout. Claude keeps its interactive
# approval flow and runs in place.
_WORKTREE_HARNESSES = frozenset({"codex", "agy", "deepseek-harness"})

log = logging.getLogger("drover.harnessd")


# Registry writes are best-effort by design -- an exception on a driver's
# pump thread would kill it and freeze the session. But "best effort" must
# not mean "silently lost forever": every permanently failed write bumps
# this counter, which the metrics endpoint exports.
_dropped_events_total = 0
_dropped_events_lock = threading.Lock()


# A different loss, on the other side of the wire: events this host recorded
# fine but could never hand to the hub. #99 lost ten of them mid-stream with
# _dropped_events_total sitting at 0, because nothing counted the push path at
# all -- the hub's write path never saw those events, so its counter could not.
_undelivered_events_total = 0
_undelivered_events_lock = threading.Lock()


_ATTACHMENT_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ADVISORY_BUNDLE_REQUEST_BYTES = 128 * 1024
# An OAuth code paste, with room to spare. Bounded so a stray body cannot be
# typed wholesale into a live terminal.
_MAX_AUTH_INPUT_CHARS = 4096


def save_turn_attachments(
    attachments_dir: Path, session_id: str, images: list
) -> list[dict[str, str]]:
    """Decode and persist per-turn images under
    ``<attachments_dir>/<session_id>/``; raises ValueError on any bad entry.
    Entries are validated before anything is written for them, so a rejected
    request leaves no file behind for the entry that failed."""
    saved: list[dict[str, str]] = []
    target = attachments_dir / session_id
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise ValueError(f"images[{index}] must be an object")
        media_type = str(image.get("media_type") or "")
        extension = _ATTACHMENT_EXTENSIONS.get(media_type)
        if extension is None:
            raise ValueError(f"unsupported media_type: {media_type!r}")
        encoded = str(image.get("data_base64") or "")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ValueError(f"invalid base64 in images[{index}]") from exc
        if not data:
            raise ValueError(f"images[{index}] is empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"images[{index}] exceeds {MAX_ATTACHMENT_BYTES} bytes decoded"
            )
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{uuid4().hex[:12]}-{index + 1}.{extension}"
        path.write_bytes(data)
        saved.append({"path": str(path), "media_type": media_type, "data_b64": encoded})
    return saved


def append_attachment_lines(text: str, saved: list[dict[str, str]]) -> str:
    for item in saved:
        line = f"[Attached image: {item['path']}]"
        text = f"{text}\n\n{line}" if text else line
    return text


def record_dropped_events(count: int = 1) -> None:
    global _dropped_events_total
    with _dropped_events_lock:
        _dropped_events_total += count


def dropped_event_count() -> int:
    with _dropped_events_lock:
        return _dropped_events_total


def reset_dropped_event_count() -> None:
    global _dropped_events_total
    with _dropped_events_lock:
        _dropped_events_total = 0


def record_undelivered_events(count: int = 1) -> None:
    global _undelivered_events_total
    with _undelivered_events_lock:
        _undelivered_events_total += count


def undelivered_event_count() -> int:
    with _undelivered_events_lock:
        return _undelivered_events_total


def reset_undelivered_event_count() -> None:
    global _undelivered_events_total
    with _undelivered_events_lock:
        _undelivered_events_total = 0


@dataclass(frozen=True)
class HarnessPreset:
    name: str
    command: tuple[str, ...]
    enabled: bool
    description: str
    # Startup gate: some CLIs open on an interactive prompt before reaching
    # their REPL (claude-code's trust-folder prompt). When any marker appears
    # in PTY output while a handoff seed is pending, the daemon types
    # `startup_gate_answer` and waits for the REPL to settle before delivering
    # the seed — otherwise the seed answers the gate and is discarded. The
    # markers are plural because claude-code has reworded the gate across
    # versions; every wording seen in the wild must stay matched. Empty means
    # the harness has no gate (shell, codex, agy).
    startup_gate_markers: tuple[str, ...] = ()
    startup_gate_answer: str = "1\n"
    # Absolute path to the resolved CLI. `command` wraps it in a login shell so
    # the harness inherits the user's environment, which hides the path from
    # anything that needs to spawn the binary directly (the Codex usage probe).
    # harnessd's own PATH comes from launchd and omits Homebrew/nvm prefixes, so
    # a bare binary name is not resolvable in this process.
    executable: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "enabled": self.enabled,
            "description": self.description,
        }


DEFAULT_PRESETS = {
    "shell": HarnessPreset(
        name="shell",
        command=("/bin/sh",),
        enabled=True,
        description="Interactive POSIX shell",
    ),
    "claude-code": HarnessPreset(
        name="claude-code",
        command=("claude",),
        enabled=False,
        description="Claude Code CLI",
        startup_gate_markers=(
            # Wordings by claude-code version, oldest first. The answer "1"
            # confirms the trust option in every known variant (verified live
            # 2026-07-22 on v2.1.217: bare "1" confirms immediately).
            "Do you trust the files in this folder?",
            "Is this a project you created or one you trust?",
        ),
        startup_gate_answer="1\n",
    ),
    "codex": HarnessPreset(
        name="codex",
        command=("codex",),
        enabled=False,
        description="Codex CLI",
    ),
    "agy": HarnessPreset(
        name="agy",
        command=("agy",),
        enabled=False,
        description="Antigravity CLI (agy)",
    ),
    # Retired as a session target: openclaw never had a structured driver, so
    # offering it here only produced "harness has no structured driver:
    # openclaw" at launch. It is still an *observed* agent -- the collect
    # source, parser and metrics stay -- it is simply not driven from Drover.
    # `test_every_offered_preset_can_actually_be_driven` keeps a driverless
    # preset from being added back.
    "deepseek-harness": HarnessPreset(
        name="deepseek-harness",
        command=("dsh",),
        enabled=False,
        description="DeepSeek Harness (local Web RPC)",
    ),
}


def resolve_harness_presets(
    presets: dict[str, HarnessPreset] | None = None,
    *,
    shell: str | None = None,
) -> dict[str, HarnessPreset]:
    """Enable CLI presets that are available in the user's login shell."""
    resolved: dict[str, HarnessPreset] = {}
    login_shell = shell or default_login_shell()
    for name, preset in (presets or DEFAULT_PRESETS).items():
        if name == "shell":
            resolved[name] = preset
            continue
        executable = resolve_executable(preset.command[0], login_shell=login_shell)
        if executable is None:
            resolved[name] = replace(
                preset,
                enabled=False,
                description=f"{preset.description}; executable not found on this host",
                executable=None,
            )
            continue
        path_prefix = executable_path_prefix(executable)
        shell_command = "exec " + " ".join(
            shlex.quote(part) for part in (executable, *preset.command[1:])
        )
        if path_prefix:
            shell_command = (
                f"export PATH={shlex.quote(path_prefix)}{os.pathsep}$PATH; "
                + shell_command
            )
        resolved[name] = replace(
            preset,
            command=(login_shell, "-lc", shell_command),
            enabled=True,
            description=f"{preset.description}; available at {executable}",
            executable=executable,
        )
    return resolved


def build_launch_command(
    preset: HarnessPreset,
    *,
    harness: str,
    native_resume: Any = None,
) -> list[str]:
    args = _native_resume_args(harness, native_resume)
    command = list(preset.command)
    if not args:
        return command
    if len(command) >= 3 and command[1] == "-lc" and command[2].startswith("exec "):
        command[2] = command[2] + " " + " ".join(shlex.quote(arg) for arg in args)
        return command
    return [*command, *args]


def apply_structured_preferences(
    command: list[str],
    *,
    harness: str,
    model: str | None,
    thinking_effort: str | None,
) -> list[str]:
    """Add startup preferences for a persistent structured CLI process."""
    preferred = list(command)
    # Claude owns one process for the whole session, so its preferences must
    # be fixed when that process starts. Codex and Gemini spawn per turn and
    # their drivers apply the current preferences to each child process.
    if harness != "claude-code":
        return preferred
    if model:
        preferred.extend(["--model", model])
    if thinking_effort:
        preferred.extend(["--effort", thinking_effort])
    return preferred


def _native_resume_args(harness: str, native_resume: Any) -> list[str]:
    if not isinstance(native_resume, dict):
        return []
    session_id = str(native_resume.get("session_id") or "").strip()
    latest = bool(native_resume.get("latest"))
    mode = str(native_resume.get("mode") or "").strip()
    if harness == "claude-code":
        if session_id:
            return ["--resume", session_id]
        if latest or mode in {"continue", "latest"}:
            return ["--continue"]
    if harness == "codex":
        if session_id:
            return ["resume", session_id]
        if latest or mode == "latest":
            return ["resume", "--last"]
        if mode == "resume":
            return ["resume"]
    if harness == "agy":
        # agy resumes by conversation ID only -- there is no bare "latest"
        # form, and `--continue` picks the most recent conversation in the
        # *current* directory, which is not the same promise the other
        # harnesses make here.
        if session_id:
            return ["--conversation", session_id]
    if harness == "openclaw":
        if session_id:
            return ["resume", session_id]
    return []


def _native_session_id(native_resume: Any) -> str | None:
    if not isinstance(native_resume, dict):
        return None
    return _optional_text(native_resume.get("session_id"))


def _native_resume_label(native_resume: Any) -> str | None:
    if not isinstance(native_resume, dict):
        return None
    if label := _optional_text(native_resume.get("label")):
        return label
    if session_id := _optional_text(native_resume.get("session_id")):
        return session_id
    if native_resume.get("latest"):
        return "latest"
    return _optional_text(native_resume.get("mode"))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _optional_client_turn_id(value: Any) -> str | None:
    turn_id = _optional_identifier(value)
    if turn_id is None:
        return None
    try:
        return str(UUID(turn_id))
    except ValueError as exc:
        raise ValueError("client_turn_id must be a UUID") from exc


def discover_native_resume_sessions(
    *,
    home: Path | None = None,
    harness: str | None = None,
    cwd: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return safe, metadata-only native resume candidates from local CLIs."""
    root = home or Path.home()
    # agy is absent on purpose: it keeps conversations in its own store under
    # ``~/.gemini/antigravity-cli/conversations`` in a format nothing here
    # reads yet. Drover's own per-session ``--conversation`` continuation is
    # unaffected -- this list only feeds the "resume a session the CLI
    # started outside Drover" picker.
    requested = {harness} if harness else {"claude-code", "codex"}
    candidates: list[dict[str, Any]] = []
    if "claude-code" in requested:
        candidates.extend(_discover_claude_sessions(root))
    if "codex" in requested:
        candidates.extend(_discover_codex_sessions(root))
    if cwd:
        wanted = str(Path(cwd).expanduser())
        exact = [item for item in candidates if item.get("cwd") == wanted]
        candidates = exact or [
            item for item in candidates if _cwd_matches(item.get("cwd"), wanted)
        ]
    candidates.sort(key=lambda item: item.get("updated_at_ts") or 0, reverse=True)
    safe = []
    for item in candidates[: max(1, min(limit, 100))]:
        item = dict(item)
        item.pop("updated_at_ts", None)
        item.pop("_path", None)
        safe.append(item)
    return safe


def native_transcript_for_session(
    *,
    harness: str | None,
    cwd: str | None,
    native_session_id: str | None = None,
    home: Path | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    """Return provider-native transcript messages for a Harness session."""
    if harness == "claude-code":
        root = home or Path.home()
        path = _claude_transcript_path(
            root,
            cwd=cwd,
            native_session_id=native_session_id,
        )
        if path is None:
            return {
                "source": "claude jsonl",
                "messages": [],
                "reason": "no Claude JSONL transcript found for this workspace",
            }
        return _read_claude_transcript(path, limit=limit)
    if harness == "codex":
        root = home or Path.home()
        path = _codex_transcript_path(
            root,
            cwd=cwd,
            native_session_id=native_session_id,
        )
        if path is None:
            return {
                "source": "codex jsonl",
                "messages": [],
                "reason": "no Codex JSONL transcript found for this workspace",
            }
        return _read_codex_transcript(path, limit=limit)
    return {
        "source": None,
        "messages": [],
        "reason": f"native transcript is not supported for harness: {harness}",
    }


def _candidate_path(
    candidates: list[dict[str, Any]],
    *,
    cwd: str | None,
    native_session_id: str | None,
) -> Path | None:
    if native_session_id:
        exact_session = [
            item for item in candidates if item.get("session_id") == native_session_id
        ]
        candidates = exact_session or candidates
    if cwd:
        wanted = str(Path(cwd).expanduser())
        exact = [item for item in candidates if item.get("cwd") == wanted]
        candidates = exact or [
            item for item in candidates if _cwd_matches(item.get("cwd"), wanted)
        ]
    candidates.sort(key=lambda item: item.get("updated_at_ts") or 0, reverse=True)
    for item in candidates:
        path = item.get("_path")
        if path:
            return Path(str(path))
    return None


def _codex_transcript_path(
    home: Path,
    *,
    cwd: str | None,
    native_session_id: str | None,
) -> Path | None:
    return _candidate_path(
        _discover_codex_sessions(home),
        cwd=cwd,
        native_session_id=native_session_id,
    )


def _discover_claude_sessions(home: Path) -> list[dict[str, Any]]:
    sessions = []
    for path in (home / ".claude/projects").glob("*/*.jsonl"):
        if not path.is_file():
            continue
        metadata = _jsonl_metadata(path)
        session_id = _optional_text(metadata.get("sessionId")) or path.stem
        cwd = _optional_text(metadata.get("cwd"))
        updated_at_ts = path.stat().st_mtime
        sessions.append(
            _candidate(
                harness="claude-code",
                session_id=session_id,
                cwd=cwd,
                path=path,
                updated_at_ts=updated_at_ts,
                source="claude jsonl",
            )
        )
    return sessions


def _discover_codex_sessions(home: Path) -> list[dict[str, Any]]:
    sessions = []
    for path in (home / ".codex/sessions").glob("**/*.jsonl"):
        if not path.is_file():
            continue
        metadata = _jsonl_metadata(path)
        session_id = _codex_session_id(path) or _optional_text(
            metadata.get("session_id")
        )
        if not session_id:
            continue
        cwd = _optional_text(metadata.get("cwd"))
        sessions.append(
            _candidate(
                harness="codex",
                session_id=session_id,
                cwd=cwd,
                path=path,
                updated_at_ts=path.stat().st_mtime,
                source="codex jsonl",
            )
        )
    return sessions


def _candidate(
    *,
    harness: str,
    session_id: str,
    cwd: str | None,
    path: Path,
    updated_at_ts: float,
    source: str,
) -> dict[str, Any]:
    label_root = Path(cwd).name if cwd else path.parent.parent.name
    short_id = session_id[:8]
    return {
        "harness": harness,
        "session_id": session_id,
        "label": f"{label_root or harness} · {short_id}",
        "cwd": cwd,
        "updated_at": datetime.fromtimestamp(updated_at_ts, timezone.utc).isoformat(),
        "updated_at_ts": updated_at_ts,
        "_path": str(path),
        "source": source,
        "path_hint": _path_hint(path),
        "native_resume": {
            "session_id": session_id,
            "label": f"{label_root or harness} · {short_id}",
        },
    }


def _claude_transcript_path(
    home: Path,
    *,
    cwd: str | None,
    native_session_id: str | None,
) -> Path | None:
    root = home / ".claude/projects"
    if native_session_id:
        matches = sorted(root.glob(f"*/{native_session_id}.jsonl"))
        if matches:
            return matches[-1]
    candidates = _discover_claude_sessions(home)
    if cwd:
        wanted = str(Path(cwd).expanduser())
        exact = [item for item in candidates if item.get("cwd") == wanted]
        candidates = exact or [
            item for item in candidates if _cwd_matches(item.get("cwd"), wanted)
        ]
    candidates.sort(key=lambda item: item.get("updated_at_ts") or 0, reverse=True)
    for item in candidates:
        path = item.get("_path")
        if path:
            return Path(str(path))
    return None


def _read_claude_transcript(path: Path, *, limit: int = 80) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    try:
        with path.open(errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                messages.extend(_claude_transcript_messages(item))
    except OSError as exc:
        return {
            "source": "claude jsonl",
            "session_id": path.stem,
            "path_hint": _path_hint(path),
            "messages": [],
            "reason": str(exc),
        }
    safe_limit = max(1, min(int(limit or 80), 200))
    updated_at_ts = path.stat().st_mtime if path.exists() else None
    return {
        "source": "claude jsonl",
        "session_id": path.stem,
        "path_hint": _path_hint(path),
        "updated_at": (
            datetime.fromtimestamp(updated_at_ts, timezone.utc).isoformat()
            if updated_at_ts
            else None
        ),
        "messages": messages[-safe_limit:],
    }


def _read_codex_transcript(path: Path, *, limit: int = 80) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    session_id = _codex_session_id(path) or path.stem
    try:
        with path.open(errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                messages.extend(_codex_transcript_messages(item, session_id=session_id))
    except OSError as exc:
        return {
            "source": "codex jsonl",
            "session_id": session_id,
            "path_hint": _path_hint(path),
            "messages": [],
            "reason": str(exc),
        }
    safe_limit = max(1, min(int(limit or 80), 200))
    updated_at_ts = path.stat().st_mtime if path.exists() else None
    return {
        "source": "codex jsonl",
        "session_id": session_id,
        "path_hint": _path_hint(path),
        "updated_at": (
            datetime.fromtimestamp(updated_at_ts, timezone.utc).isoformat()
            if updated_at_ts
            else None
        ),
        "messages": messages[-safe_limit:],
    }


def _claude_transcript_messages(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    item_type = str(item.get("type") or "")
    if item_type not in {"user", "assistant"}:
        return []
    message = item.get("message")
    if not isinstance(message, dict):
        return []
    role = str(message.get("role") or item_type)
    content = message.get("content")
    timestamp = _optional_text(item.get("timestamp"))
    uuid = _optional_text(item.get("uuid"))
    session_id = _optional_text(item.get("sessionId"))
    records: list[dict[str, Any]] = []
    if isinstance(content, str):
        if text := _clip_transcript_text(content):
            records.append(
                _native_transcript_message(
                    role="user" if role == "user" else "assistant",
                    text=text,
                    timestamp=timestamp,
                    uuid=uuid,
                    session_id=session_id,
                )
            )
        return records
    if not isinstance(content, list):
        return records
    text_parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type == "text":
            if text := _clip_transcript_text(part.get("text")):
                text_parts.append(text)
            continue
        if part_type == "tool_use":
            if text_parts:
                records.append(
                    _native_transcript_message(
                        role="assistant",
                        text="\n\n".join(text_parts),
                        timestamp=timestamp,
                        uuid=uuid,
                        session_id=session_id,
                    )
                )
                text_parts = []
            records.append(
                _native_transcript_message(
                    role="tool_use",
                    text=_format_claude_tool_use(part),
                    timestamp=timestamp,
                    uuid=uuid,
                    session_id=session_id,
                    title=f"Tool: {_optional_text(part.get('name')) or 'action'}",
                )
            )
            continue
        if part_type == "tool_result":
            records.append(
                _native_transcript_message(
                    role="tool_result",
                    text=_format_claude_tool_result(part),
                    timestamp=timestamp,
                    uuid=uuid,
                    session_id=session_id,
                    title="Tool result",
                )
            )
            continue
    if text_parts:
        records.append(
            _native_transcript_message(
                role="user" if role == "user" else "assistant",
                text="\n\n".join(text_parts),
                timestamp=timestamp,
                uuid=uuid,
                session_id=session_id,
            )
        )
    return [record for record in records if record.get("text")]


def _codex_transcript_messages(
    item: Mapping[str, Any], *, session_id: str | None
) -> list[dict[str, Any]]:
    payload = _coerce_mapping(item.get("payload"))
    if item.get("type") != "response_item" or not payload:
        return []
    timestamp = _optional_text(item.get("timestamp"))
    payload_type = _optional_text(payload.get("type"))
    if payload_type == "message":
        role = _optional_text(payload.get("role")) or "message"
        if role in {"developer", "system"}:
            return []
        text = _content_text(payload.get("content"))
        if not text:
            return []
        return [
            _native_transcript_message(
                role="assistant" if role == "assistant" else "user",
                text=text,
                timestamp=timestamp,
                uuid=_optional_text(payload.get("id")),
                session_id=session_id,
            )
        ]
    if payload_type == "function_call":
        name = _optional_text(payload.get("name")) or "tool"
        return [
            _native_transcript_message(
                role="tool_use",
                text=_format_codex_function_call(payload),
                timestamp=timestamp,
                uuid=_optional_text(payload.get("call_id"))
                or _optional_text(payload.get("id")),
                session_id=session_id,
                title=f"Tool: {name}",
            )
        ]
    if payload_type == "function_call_output":
        return [
            _native_transcript_message(
                role="tool_result",
                text=_clip_transcript_text(payload.get("output")),
                timestamp=timestamp,
                uuid=_optional_text(payload.get("call_id"))
                or _optional_text(payload.get("id")),
                session_id=session_id,
                title="Tool result",
            )
        ]
    return []


def _native_transcript_message(
    *,
    role: str,
    text: str,
    timestamp: str | None,
    uuid: str | None,
    session_id: str | None,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "title": title,
        "text": text,
        "created_at": timestamp,
        "uuid": uuid,
        "native_session_id": session_id,
    }


def _format_claude_tool_use(part: Mapping[str, Any]) -> str:
    name = _optional_text(part.get("name")) or "tool"
    tool_input = part.get("input")
    if isinstance(tool_input, dict):
        description = _optional_text(tool_input.get("description"))
        command = _optional_text(tool_input.get("command"))
        if command:
            body = f"```sh\n{command}\n```"
            return f"{description}\n\n{body}" if description else body
        return (
            _clip_transcript_text(json.dumps(tool_input, indent=2, sort_keys=True))
            or name
        )
    return _clip_transcript_text(tool_input) or name


def _format_codex_function_call(payload: Mapping[str, Any]) -> str:
    name = _optional_text(payload.get("name")) or "tool"
    arguments = payload.get("arguments")
    text = _clip_transcript_text(arguments)
    if not text:
        return name
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        command = _optional_text(parsed.get("cmd") or parsed.get("command"))
        if command:
            return f"```sh\n{command}\n```"
        return "```json\n" + json.dumps(parsed, indent=2, sort_keys=True) + "\n```"
    return text


def _format_claude_tool_result(part: Mapping[str, Any]) -> str:
    content = part.get("content")
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            else:
                texts.append(str(item))
        content = "\n".join(texts)
    prefix = "Error\n\n" if part.get("is_error") else ""
    return prefix + (_clip_transcript_text(content) or "")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return _clip_transcript_text(content)
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if text is None:
            text = part.get("input_text") or part.get("output_text")
        if text is not None:
            texts.append(str(text))
    return _clip_transcript_text("\n\n".join(texts))


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _clip_transcript_text(value: Any, *, max_chars: int = 12000) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n...[truncated]"


def _jsonl_metadata(path: Path, *, max_lines: int = 250) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        with path.open(errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= max_lines:
                    break
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _collect_metadata(item, metadata)
                if metadata.get("sessionId") and metadata.get("cwd"):
                    break
    except OSError:
        return metadata
    return metadata


def _collect_metadata(item: Any, metadata: dict[str, Any]) -> None:
    if isinstance(item, dict):
        for key in ("sessionId", "session_id", "cwd", "timestamp", "lastUpdated"):
            if key in item and item[key] and key not in metadata:
                metadata[key] = item[key]
        for value in item.values():
            if isinstance(value, dict | list):
                _collect_metadata(value, metadata)
    elif isinstance(item, list):
        for value in item[:20]:
            _collect_metadata(value, metadata)


def _codex_session_id(path: Path) -> str | None:
    matches = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        path.stem,
    )
    return matches[-1] if matches else None


def _timestamp_to_epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cwd_matches(candidate: Any, wanted: str) -> bool:
    if not candidate:
        return False
    candidate_path = Path(str(candidate)).expanduser()
    wanted_path = Path(wanted).expanduser()
    try:
        candidate_path.relative_to(wanted_path)
        return True
    except ValueError:
        pass
    try:
        wanted_path.relative_to(candidate_path)
        return True
    except ValueError:
        pass
    candidate_text = str(candidate_path)
    wanted_text = str(wanted_path)
    return candidate_text.endswith(wanted_text) or wanted_text.endswith(candidate_text)


def _path_hint(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


# Handoff-seed delivery timing. The seed is typed into the harness once its
# startup output settles, instead of after a blind fixed delay at spawn (which
# raced the CLI's cold start and was frequently swallowed). "Settled" = the
# PTY has been quiet for _SEED_SETTLE_S after producing at least some output;
# if the CLI stays silent, deliver anyway _SEED_COLD_QUIET_S after attach so a
# genuinely quiet-but-ready REPL still gets seeded.
_SEED_SETTLE_S = 0.4
_SEED_COLD_QUIET_S = 1.5
_RECOVERY_HARNESSES = {"claude-code", "codex", "deepseek-harness"}
_RECOVERY_UNAVAILABLE = (
    "Session cannot be resumed after the harness restart. "
    "Continue it in a new session."
)


# claude-code's ink renderer emits UI text word-by-word with cursor-position
# escapes between words, so a gate sentence never appears contiguously in the
# raw PTY stream. Strip escape sequences AND whitespace from both sides before
# substring-matching gate markers.
_TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"  # CSI sequences (colors, cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (titles)
    r"|\x1b[=>()][0-9A-Za-z]?"  # charset / keypad-mode selects
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # other control bytes (keep \n via \s below)
)


def _normalize_gate_text(text: str) -> str:
    return re.sub(r"\s+", "", _TERMINAL_ESCAPE_RE.sub("", text))


@dataclass
class PendingSeed:
    """A queued handoff seed plus the startup-gate handling its harness needs
    (from the preset at session create). `gate_answered` flips once the gate
    has been typed at, so a redrawn marker is never answered twice."""

    text: str
    gate_markers: tuple[str, ...] = ()
    gate_answer: str = "1\n"
    gate_answered: bool = False


@dataclass
class HarnessDaemonState:
    host_id: str
    display_name: str
    kind: str
    registry: HarnessRegistry
    pty: PtySessionManager
    presets: dict[str, HarnessPreset]
    auth: AuthFlowManager = field(
        default_factory=lambda: AuthFlowManager(default_auth_adapters())
    )
    structured: StructuredSessionManager = field(
        default_factory=StructuredSessionManager
    )
    local_url: str | None = None
    tailscale_url: str | None = None
    central_url: str | None = None
    host_token: str | None = None
    api_token: str = ""
    # True when this daemon dials the hub instead of waiting to be dialled;
    # visible here so the registration payload can advertise which it is.
    relay: bool = False
    # Set after construction rather than passed in: HostUpdater takes the
    # state itself, to ask whether this host is idle enough to activate.
    # None whenever [update] is disabled, which is what makes the config
    # kill switch reach the host and not just the hub.
    updater: Any = None
    # Whether we have ever reached the hub since this process started. The
    # rollback watchdog reads it to decide whether a freshly activated
    # version can talk to the hub at all, so it must never be reset.
    registered_at_least_once: bool = False
    terminated_session_ids: set[str] = field(default_factory=set)
    worktrees_dir: Path = field(
        default_factory=lambda: Path.home() / ".drover" / "worktrees"
    )
    # Per-turn image attachments land here (never inside the session cwd or
    # its worktree, so user repos stay clean).
    attachments_dir: Path = field(
        default_factory=lambda: Path.home() / ".drover" / "attachments"
    )
    # Live sessions running in a per-session worktree, for cleanup at
    # finalize/terminate time. Lost on daemon restart, in which case the
    # worktree simply stays on disk (git worktree list still shows it).
    session_worktrees: dict[str, SessionWorktree] = field(default_factory=dict)
    # Handoff seeds ("initial_input") waiting to be typed into a PTY session
    # once its harness CLI has actually started. Keyed by session_id; consumed
    # (pop) by the terminal loop when the CLI's startup output settles. See
    # _create_session / _maybe_deliver_pending_seed for why this is deferred
    # rather than written with a fixed delay at spawn time.
    pending_initial_input: dict[str, PendingSeed] = field(default_factory=dict)
    recovery_locks: dict[str, threading.Lock] = field(default_factory=dict)
    recovery_locks_guard: threading.Lock = field(default_factory=threading.Lock)
    # run_harnessd() replaces this with EventPusher.push when a central URL
    # and token are configured; otherwise structured sessions still work
    # locally and events simply aren't pushed anywhere.
    push_event: Callable[[str, dict[str, Any]], None] = lambda session_id, event: None
    pusher: EventPusher | None = None
    # Startup reconciliation reads from the durable DuckDB ledger. Keep the
    # pass pending until it reaches the end successfully so a transient hub
    # outage is retried after the next successful heartbeat.
    event_reconciliation_pending: bool = True
    event_reconciliation_lock: threading.Lock = field(default_factory=threading.Lock)
    event_reconciliation_schedule_lock: threading.Lock = field(
        default_factory=threading.Lock
    )
    event_reconciliation_thread: threading.Thread | None = None
    provider_usage_probe: CodexUsageProbe | None = None
    claude_usage_probe: Any | None = None
    agy_usage_probe: Any | None = None
    advisory_content: "AdvisoryContentConfig | None" = None
    content_consent: DurableContentConsent | None = None
    model_catalog_service: ModelCatalogService | None = None
    model_catalog_service_lock: threading.Lock = field(default_factory=threading.Lock)

    def recovery_lock_for(self, session_id: str) -> threading.Lock:
        with self.recovery_locks_guard:
            return self.recovery_locks.setdefault(session_id, threading.Lock())

    def capabilities(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "harnesses": [preset.as_json() for preset in self.presets.values()],
        }


class HarnessHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, state: HarnessDaemonState):
        super().__init__(server_address, RequestHandlerClass)
        self.state = state


class HarnessRequestHandler(BaseHTTPRequestHandler):
    server: HarnessHTTPServer

    def _model_catalog_service(self) -> ModelCatalogService:
        state = self.server.state
        with state.model_catalog_service_lock:
            if state.model_catalog_service is None:
                state.model_catalog_service = default_model_catalog_service(
                    state.host_id, state.presets
                )
            return state.model_catalog_service

    def _invalidate_model_catalog(self, harness: str) -> None:
        service = self.server.state.model_catalog_service
        if service is not None:
            service.invalidate(harness)

    def _authorized(self) -> bool:
        token = self.server.state.api_token
        if not token:
            return True  # auth off (warned at startup)
        authorization = self.headers.get("Authorization", "") or ""
        return authorization.startswith("Bearer ") and hmac.compare_digest(
            authorization.removeprefix("Bearer ").strip(), token
        )

    def _gate(self) -> bool:
        if self._authorized():
            return True
        self._write_json(
            {"error": "authentication required"}, status=HTTPStatus.UNAUTHORIZED
        )
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/healthz" and not self._gate():
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/terminal"):
            self._attach_terminal(parsed.path)
            return
        if parsed.path == "/healthz":
            self._reconcile_exited_sessions()
            self._write_json(
                {
                    "ok": True,
                    "host_id": self.server.state.host_id,
                    "active_sessions": len(self.server.state.pty.list_sessions()),
                }
            )
            return
        if parsed.path == "/capabilities":
            self._write_json(self.server.state.capabilities())
            return
        if parsed.path == "/model-catalog":
            self._model_catalog(parsed.query)
            return
        if parsed.path == "/providers/usage":
            self._provider_usage()
            return
        if parsed.path == "/native-sessions":
            self._list_native_sessions(parsed.query)
            return
        auth_route = _parse_auth_route(parsed.path)
        if auth_route and auth_route[2] == "status":
            self._auth_status(auth_route[0])
            return
        if auth_route and auth_route[2] == "flow":
            self._auth_flow(auth_route[0], auth_route[1] or "")
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith(
            "/native-transcript"
        ):
            session_id = (
                parsed.path.removeprefix("/sessions/")
                .removesuffix("/native-transcript")
                .strip("/")
            )
            self._get_native_transcript(session_id, parsed.query)
            return
        if parsed.path == "/sessions":
            self._list_sessions()
            return
        if parsed.path.startswith("/sessions/"):
            session_id = parsed.path.removeprefix("/sessions/").strip("/")
            self._get_session(session_id)
            return
        self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/healthz" and not self._gate():
            return
        auth_route = _parse_auth_route(parsed.path)
        if auth_route and auth_route[2] == "start":
            self._auth_start(auth_route[0])
            return
        if auth_route and auth_route[2] == "cancel":
            self._auth_cancel(auth_route[0], auth_route[1] or "")
            return
        if auth_route and auth_route[2] == "input":
            self._auth_input(auth_route[0], auth_route[1] or "")
            return
        if parsed.path == "/advisory/content-bundle":
            self._advisory_content_bundle()
            return
        if parsed.path == "/advisory/content-version":
            self._advisory_content_version()
            return
        if parsed.path == "/advisory/content-consent":
            self._advisory_content_consent()
            return
        if parsed.path == "/sessions":
            self._create_session()
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/recover"):
            session_id = (
                parsed.path.removeprefix("/sessions/")
                .removesuffix("/recover")
                .strip("/")
            )
            self._recover_structured_session(session_id)
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/terminate"):
            session_id = (
                parsed.path.removeprefix("/sessions/")
                .removesuffix("/terminate")
                .strip("/")
            )
            self._terminate_session(session_id)
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/turns"):
            session_id = (
                parsed.path.removeprefix("/sessions/").removesuffix("/turns").strip("/")
            )
            self._create_turn(session_id)
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/permission"):
            session_id = (
                parsed.path.removeprefix("/sessions/")
                .removesuffix("/permission")
                .strip("/")
            )
            self._answer_permission(session_id)
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/interrupt"):
            session_id = (
                parsed.path.removeprefix("/sessions/")
                .removesuffix("/interrupt")
                .strip("/")
            )
            self._interrupt_session(session_id)
            return
        self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _auth_status(self, harness: str) -> None:
        try:
            status = self.server.state.auth.status(harness)
        except KeyError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        status["host_id"] = self.server.state.host_id
        if status.get("state") == "unavailable":
            status.setdefault(
                "error", status.get("detail") or f"auth is not supported for {harness}"
            )
            self._write_json(status, status=HTTPStatus.NOT_FOUND)
            return
        self._write_json(status)

    def _auth_start(self, harness: str) -> None:
        if self._read_json() is None:
            self._write_json({"error": "invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            snapshot = self.server.state.auth.start(harness)
        except KeyError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except TerminalSignInRequired as exc:
            # Not a failure to report as one: the client is expected to open
            # a PTY session instead, so say which mode applies.
            self._write_json(
                {"error": str(exc), "harness": harness, "sign_in": "terminal"},
                status=HTTPStatus.CONFLICT,
            )
            return
        except AuthFlowLaunchError as exc:
            self._write_json(
                {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )
            return
        except RuntimeError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        snapshot["host_id"] = self.server.state.host_id
        self._invalidate_model_catalog(harness)
        self._write_json(snapshot)

    def _auth_flow(self, harness: str, flow_id: str) -> None:
        try:
            snapshot = self.server.state.auth.snapshot(harness, flow_id)
        except KeyError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        snapshot["host_id"] = self.server.state.host_id
        if snapshot.get("state") == "authenticated":
            self._invalidate_model_catalog(harness)
        self._write_json(snapshot)

    def _auth_cancel(self, harness: str, flow_id: str) -> None:
        if self._read_json() is None:
            self._write_json({"error": "invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            snapshot = self.server.state.auth.cancel(harness, flow_id)
        except KeyError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        snapshot["host_id"] = self.server.state.host_id
        self._invalidate_model_catalog(harness)
        self._write_json(snapshot)

    def _auth_input(self, harness: str, flow_id: str) -> None:
        body = self._read_json()
        if body is None:
            self._write_json({"error": "invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
            return
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._write_json(
                {"error": "text must be a non-empty string"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if len(text) > _MAX_AUTH_INPUT_CHARS:
            self._write_json(
                {"error": "text is too long"}, status=HTTPStatus.BAD_REQUEST
            )
            return
        try:
            snapshot = self.server.state.auth.send_input(harness, flow_id, text)
        except KeyError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except AuthFlowInputError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        snapshot["host_id"] = self.server.state.host_id
        self._invalidate_model_catalog(harness)
        self._write_json(snapshot)

    def _model_catalog(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        if set(params) - {"harness", "refresh"}:
            self._write_json(
                {"error": "invalid model catalog query"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        harness_values = params.get("harness", [])
        refresh_values = params.get("refresh", [])
        if (
            len(harness_values) != 1
            or not harness_values[0].strip()
            or len(refresh_values) > 1
            or (refresh_values and refresh_values[0] not in {"0", "1"})
        ):
            self._write_json(
                {"error": "invalid model catalog query"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        harness = harness_values[0]
        preset = self.server.state.presets.get(harness)
        if preset is None or not preset.enabled:
            self._write_json(
                {"error": f"unknown or disabled harness preset: {harness}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        envelope = self._model_catalog_service().read(
            harness, force=refresh_values == ["1"]
        )
        self._write_json(envelope.to_wire())

    def _provider_usage(self) -> None:
        observed_at = datetime.now(timezone.utc)
        accounts: list[dict[str, Any]] = []
        for detected in detect_provider_accounts(self.server.state.capabilities()):
            if detected.provider == "openai":
                snapshot = self._codex_usage_snapshot()
                accounts.append(_provider_snapshot_json(snapshot, detected))
                continue
            if detected.provider == "anthropic":
                detected = _with_claude_plan_label(detected, self.server.state.auth)
                probe = self.server.state.claude_usage_probe
                if probe is None:
                    from drover.server.providers.claude import ClaudeUsageProbe

                    probe = ClaudeUsageProbe()
                    self.server.state.claude_usage_probe = probe
                snapshot = probe.read(host_id=self.server.state.host_id)
                accounts.append(_provider_snapshot_json(snapshot, detected))
                continue
            if detected.provider == "google":
                from drover.server.providers.agy import AgyUsageProbe

                probe = self.server.state.agy_usage_probe
                if probe is None:
                    probe = AgyUsageProbe()
                    self.server.state.agy_usage_probe = probe
                snapshot = probe.read(host_id=self.server.state.host_id)
                accounts.append(_provider_snapshot_json(snapshot, detected))
                continue
            accounts.append(_unavailable_provider_json(detected, observed_at))
        self._write_json({"accounts": accounts, "observed_at": observed_at.isoformat()})

    def _codex_usage_snapshot(self) -> ProviderAccountSnapshot:
        """Probe Codex at the path its preset already resolved."""
        from drover.server.providers.codex import CodexUsageProbe

        preset = self.server.state.presets.get("codex")
        executable = preset.executable if preset is not None else None
        command = (executable, "app-server", "--stdio") if executable else None
        probe = self.server.state.provider_usage_probe
        if probe is None or probe.command != command:
            probe = CodexUsageProbe(command=command)
            self.server.state.provider_usage_probe = probe
        return probe.read(host_id=self.server.state.host_id)

    def _advisory_content_bundle(self) -> None:
        if not self.server.state.api_token or not self._authorized():
            self._write_json(
                {"error": "authentication required"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        config = self.server.state.advisory_content
        live_consent = self.server.state.content_consent
        enabled = (
            live_consent.snapshot()["enabled"]
            if live_consent is not None
            else bool(config is not None and config.enabled)
        )
        if config is None or not enabled:
            self._write_json(
                {"error": "content analysis is disabled"},
                status=HTTPStatus.FORBIDDEN,
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._write_json(
                {"error": "invalid Content-Length"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if content_length > MAX_ADVISORY_BUNDLE_REQUEST_BYTES:
            self._write_json(
                {"error": "content bundle request exceeds byte limit"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        body = self._read_json()
        if body is None or set(body) != {"target_ids"}:
            self._write_json(
                {"error": "request must contain only target_ids"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        target_ids = body.get("target_ids")
        if not _valid_content_target_ids(target_ids):
            self._write_json(
                {"error": "target_ids must be a non-empty list of unique IDs"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        from drover.server.advisory.content_targets import (
            ContentTarget,
            ContentTargetError,
            build_content_bundle,
        )

        configured: dict[str, ContentTarget] = {}
        for configured_path in config.targets:
            target = ContentTarget(Path(configured_path))
            if target.target_id in configured:
                self._write_json(
                    {"error": "configured advisory target IDs must be unique"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            configured[target.target_id] = target
        if any(target_id not in configured for target_id in target_ids):
            self._write_json(
                {"error": "unknown advisory content target ID"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        bundle = None
        payload = None
        selected = [configured[target_id] for target_id in target_ids]
        try:
            bundle = build_content_bundle(
                selected,
                allowed_roots=config.allowed_roots,
                host_id=self.server.state.host_id,
                max_file_bytes=config.max_file_bytes,
                max_bundle_bytes=config.max_bundle_bytes,
            )
            payload = {
                "bundle_hash": bundle.bundle_hash,
                "created_at": bundle.created_at.isoformat(),
                "targets": [
                    {
                        "target_id": target.target_id,
                        "content_hash": target.content_hash,
                        "redacted_content": target.redacted_content,
                    }
                    for target in bundle.targets
                ],
            }
            self._write_json(payload, headers={"Cache-Control": "no-store"})
        except ContentTargetError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        finally:
            # Content is intentionally scoped to this request. Explicitly sever
            # the largest references as soon as serialization has completed.
            selected.clear()
            bundle = None
            payload = None

    def _advisory_content_version(self) -> None:
        """Return only redacted target hashes behind the live consent epoch."""

        if not self.server.state.api_token or not self._authorized():
            self._write_json(
                {"error": "authentication required"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        config = self.server.state.advisory_content
        live_consent = self.server.state.content_consent
        snapshot = live_consent.snapshot() if live_consent is not None else None
        if (
            config is None
            or snapshot is None
            or not snapshot["enabled"]
            or int(snapshot["epoch"]) <= 0
        ):
            self._write_json(
                {"error": "content analysis is disabled"},
                status=HTTPStatus.FORBIDDEN,
            )
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._write_json(
                {"error": "invalid Content-Length"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if content_length > MAX_ADVISORY_BUNDLE_REQUEST_BYTES:
            self._write_json(
                {"error": "content version request exceeds byte limit"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        body = self._read_json()
        if body is None or set(body) != {"target_ids"}:
            self._write_json(
                {"error": "request must contain only target_ids"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        target_ids = body.get("target_ids")
        if not _valid_content_target_ids(target_ids):
            self._write_json(
                {"error": "target_ids must be a non-empty list of unique IDs"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        from drover.server.advisory.content_targets import (
            ContentTarget,
            ContentTargetError,
            build_content_version,
        )

        configured: dict[str, ContentTarget] = {}
        for configured_path in config.targets:
            target = ContentTarget(Path(configured_path))
            if target.target_id in configured:
                self._write_json(
                    {"error": "configured advisory target IDs must be unique"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            configured[target.target_id] = target
        if any(target_id not in configured for target_id in target_ids):
            self._write_json(
                {"error": "unknown advisory content target ID"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        selected = [configured[target_id] for target_id in target_ids]
        version = None
        payload = None
        try:
            version = build_content_version(
                selected,
                allowed_roots=config.allowed_roots,
                host_id=self.server.state.host_id,
                max_file_bytes=config.max_file_bytes,
                max_bundle_bytes=config.max_bundle_bytes,
            )
            payload = {
                "bundle_hash": version.bundle_hash,
                "targets": [
                    {
                        "target_id": target.target_id,
                        "content_hash": target.content_hash,
                    }
                    for target in version.targets
                ],
            }
            self._write_json(payload, headers={"Cache-Control": "no-store"})
        except ContentTargetError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        finally:
            selected.clear()
            version = None
            payload = None

    def _advisory_content_consent(self) -> None:
        # Unlike the daemon's ordinary endpoints, auth-off mode is never
        # accepted for consent mutation: an empty token is fail-closed.
        if not self.server.state.api_token or not self._authorized():
            self._write_json(
                {"error": "authentication required"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        body = self._read_json()
        if body is None or set(body) != {"enabled", "epoch"}:
            self._write_json(
                {"error": "request must contain only enabled and epoch"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        consent = self.server.state.content_consent
        if consent is None:
            self._write_json(
                {"error": "content consent storage is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            applied = consent.apply(enabled=body["enabled"], epoch=body["epoch"])
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self._write_json(applied)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/healthz" and not self._gate():
            return
        if parsed.path.startswith("/sessions/"):
            session_id = parsed.path.removeprefix("/sessions/").strip("/")
            self._terminate_session(session_id)
            return
        self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._write_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return

    def _create_session(self) -> None:
        body = self._read_json()
        if body is None:
            self._write_json(
                {"error": "request body must be valid JSON"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        mode = str(body.get("mode") or "pty")
        if mode == "structured":
            self._create_structured_session(body)
            return
        harness = str(body.get("harness") or "shell")
        preset = self.server.state.presets.get(harness)
        if preset is None:
            self._write_json(
                {"error": f"unknown harness preset: {harness}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if not preset.enabled:
            self._write_json(
                {"error": f"harness preset is not enabled yet: {harness}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        cwd = body.get("cwd")
        if cwd is not None and not Path(str(cwd)).expanduser().is_dir():
            self._write_json(
                {"error": f"cwd does not exist: {cwd}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        command = body.get("command") or build_launch_command(
            preset,
            harness=harness,
            native_resume=body.get("native_resume"),
        )
        session_id = f"harness-{uuid4()}"
        registry_created = False
        try:
            session = self.server.state.registry.create_session(
                host_id=self.server.state.host_id,
                harness=harness,
                command=_command_label(command),
                session_id=session_id,
                repo_owner=body.get("repo_owner"),
                repo_name=body.get("repo_name"),
                branch=body.get("branch"),
                cwd=str(cwd) if cwd is not None else None,
                status="starting",
                started_at=datetime.now(timezone.utc),
                native_session_id=_native_session_id(body.get("native_resume")),
                native_resume_label=_native_resume_label(body.get("native_resume")),
                source_session_id=_optional_text(body.get("source_session_id")),
                handoff_mode=_optional_text(body.get("handoff_mode")),
            )
            session_id = session.session_id
            registry_created = True
        except Exception:
            registry_created = False
        try:
            pty_session = self.server.state.pty.start(
                session_id=session_id,
                command=command,
                cwd=cwd,
                rows=int(body.get("rows") or 24),
                cols=int(body.get("cols") or 80),
            )
        except Exception as exc:
            if registry_created:
                self._safe_update_session_status(
                    session_id,
                    "errored",
                    last_error=str(exc),
                    ended_at=datetime.now(timezone.utc),
                )
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._safe_update_session_status(
            session_id,
            "running",
        )
        self._safe_append_event(
            session_id=session_id,
            event_type="session.started",
            payload={"pid": pty_session.pid, "command": list(pty_session.command)},
        )
        initial_input = body.get("initial_input")
        if initial_input:
            # Don't type the handoff seed here: the harness CLI has only just
            # been spawned and isn't reading its stdin yet, so a fixed-delay
            # write races its cold start and is usually lost. Queue it instead;
            # the terminal loop types it once the CLI's startup output settles
            # (answering the preset's startup gate first, if one appears).
            self.server.state.pending_initial_input[session_id] = PendingSeed(
                text=str(initial_input),
                gate_markers=preset.startup_gate_markers,
                gate_answer=preset.startup_gate_answer,
            )
        self._write_json(
            {
                "session_id": session_id,
                "host_id": self.server.state.host_id,
                "harness": harness,
                "status": "running",
                "pid": pty_session.pid,
                "registry_synced": registry_created,
            },
            status=HTTPStatus.CREATED,
        )

    def _maybe_deliver_pending_seed(
        self,
        session_id: str,
        *,
        attach_ts: float,
        last_output_ts: float | None,
        recent_output: str = "",
    ) -> None:
        """Type a queued handoff seed into the PTY once the harness CLI looks
        ready — i.e. its startup output has been quiet for _SEED_SETTLE_S, or
        (if it produced nothing) _SEED_COLD_QUIET_S have passed since attach.

        If the settled output is the harness's startup gate (claude-code's
        trust-folder prompt on an untrusted cwd), typing the seed now would
        answer the gate with garbage and discard the context — so the gate is
        answered instead, and delivery waits for the next settle once the REPL
        behind it has drawn.

        Called each terminal-loop iteration. The seed is claimed with an atomic
        ``pop`` so that exactly one delivery happens even if several clients
        attach to the same session.
        """
        pending = self.server.state.pending_initial_input.get(session_id)
        if pending is None:
            return
        now = monotonic()
        if last_output_ts is not None:
            ready = (now - last_output_ts) >= _SEED_SETTLE_S
        else:
            ready = (now - attach_ts) >= _SEED_COLD_QUIET_S
        if not ready:
            return
        watched = _normalize_gate_text(recent_output) if pending.gate_markers else ""
        if (
            pending.gate_markers
            and not pending.gate_answered
            and any(
                _normalize_gate_text(marker) in watched
                for marker in pending.gate_markers
            )
        ):
            pending.gate_answered = True
            try:
                self.server.state.pty.write(session_id, pending.gate_answer)
            except Exception:
                pass
            return
        seed = self.server.state.pending_initial_input.pop(session_id, None)
        if seed is None:
            return
        try:
            # Type as a keyboard would: Enter is CR, not LF. Raw-mode ink
            # inputs (claude-code) never submit on "\n" — the seed would sit
            # unsent in the input box — while canonical-mode shells map CR
            # back to NL via icrnl, so "\r" submits everywhere.
            self.server.state.pty.write(session_id, seed.text.replace("\n", "\r"))
        except Exception:
            return
        self._safe_append_event(
            session_id=session_id,
            event_type="terminal.initial_input",
            payload={
                "byte_count": len(seed.text.encode("utf-8")),
                "text": _redact_terminal_text(seed.text),
            },
            normalized_type="handoff_marker",
            normalized_source="inferred_terminal",
            content_preview=_redact_terminal_text(seed.text),
        )

    def _live_handoff_session(self, source_session_id: str | None, harness: str):
        """A live session this host already made for that source, if any.

        Only for handoffs: an ordinary launch carries no source, and two
        launches into the same directory are a thing a user can legitimately
        want. A handoff is different -- it names the session it continues, and
        continuing one session twice into two agents is never the intent.

        Bounded to sessions the daemon still considers live. A finished
        handoff should not stop the user starting another one later from the
        same source.
        """

        if not source_session_id:
            return None
        try:
            sessions = self.server.state.registry.list_sessions(
                host_id=self.server.state.host_id
            )
        except Exception:  # noqa: BLE001 - dedupe is best effort, never fatal
            return None
        for session in sessions:
            if (
                session.source_session_id == source_session_id
                and session.harness == harness
                and session.status in {"starting", "running"}
                and self.server.state.structured.has(session.session_id)
            ):
                return session
        return None

    def _create_structured_session(self, body: dict[str, Any]) -> None:
        harness = str(body.get("harness") or "")
        cwd = body.get("cwd")
        if cwd is not None and not Path(str(cwd)).expanduser().is_dir():
            self._write_json(
                {"error": f"cwd does not exist: {cwd}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        permission_mode = str(body.get("permission_mode") or "auto")
        if permission_mode == "ask":
            self._write_json(
                {
                    "error": "permission_mode 'ask' is not supported yet "
                    "(approval surfacing is a follow-up); use 'auto'"
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if permission_mode != "auto":
            self._write_json(
                {"error": f"unknown permission_mode: {permission_mode}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        model = _optional_identifier(body.get("model"))
        thinking_effort = _optional_identifier(body.get("thinking_effort"))
        if model is not None or thinking_effort is not None:
            try:
                self._model_catalog_service().validate(harness, model, thinking_effort)
            except CatalogSelectionError as exc:
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
        command = body.get("command")
        default_command_fn = _STRUCTURED_DEFAULT_COMMANDS.get(harness)
        if command is None and default_command_fn:
            command = default_command_fn()
        if command is not None:
            command = apply_structured_preferences(
                list(command),
                harness=harness,
                model=model,
                thinking_effort=thinking_effort,
            )
        label_source = command
        # A handoff already carries its own idempotency key: the session it
        # came from. The hub stops waiting for a create after
        # CREATE_SESSION_TIMEOUT_S and cannot tell a slow success from a
        # failure, so the honest advice to a user is to check rather than
        # retry -- but a retry still has to be survivable. One handoff means
        # one session, so a repeat for a source that already has a live
        # session here adopts it instead of starting a second agent in the
        # same repository.
        existing = self._live_handoff_session(
            _optional_text(body.get("source_session_id")), harness
        )
        if existing is not None:
            self._write_json(
                {
                    "session_id": existing.session_id,
                    "host_id": self.server.state.host_id,
                    "harness": harness,
                    "status": existing.status,
                    "mode": "structured",
                    "model": existing.model,
                    "thinking_effort": existing.thinking_effort,
                    # Named so the caller can tell "made you one" from "you
                    # already had one", rather than inferring it from ids.
                    "deduplicated": True,
                },
                status=HTTPStatus.OK,
            )
            return
        session_id = f"harness-{uuid4()}"

        session_cwd = str(cwd) if cwd is not None else None
        session_worktree: SessionWorktree | None = None
        if harness in _WORKTREE_HARNESSES and session_cwd is not None:
            session_worktree = create_session_worktree(
                session_cwd, session_id, self.server.state.worktrees_dir
            )
            if session_worktree is not None:
                session_cwd = session_worktree.path

        registry_created = False
        try:
            session = self.server.state.registry.create_session(
                host_id=self.server.state.host_id,
                harness=harness,
                command=_command_label(label_source) if label_source else harness,
                session_id=session_id,
                repo_owner=body.get("repo_owner"),
                repo_name=body.get("repo_name"),
                branch=body.get("branch"),
                cwd=session_cwd,
                status="starting",
                started_at=datetime.now(timezone.utc),
                source_session_id=_optional_text(body.get("source_session_id")),
                handoff_mode=_optional_text(body.get("handoff_mode")),
                mode="structured",
                permission_mode=permission_mode,
                model=model,
                thinking_effort=thinking_effort,
            )
            session_id = session.session_id
            registry_created = True
        except Exception:
            registry_created = False
        if session_worktree is not None:
            self.server.state.session_worktrees[session_id] = session_worktree

        # Write the "running" status and session.started event BEFORE the
        # driver starts: once structured.start() returns, the driver's pump
        # thread may already be emitting messages, and each registry write
        # opens its own DuckDB connection -- concurrent writers to the same
        # session row raise TransactionException. At this point no pump
        # thread exists yet, so these writes cannot race the manager's
        # emit() path. If start() raises, the except handler below still
        # overwrites the status with "errored".
        self._safe_update_session_status(session_id, "running")
        started_payload: dict[str, Any] = {
            "command": command or [],
            "mode": "structured",
        }
        if model is not None:
            started_payload["model"] = model
        if thinking_effort is not None:
            started_payload["thinking_effort"] = thinking_effort
        if session_worktree is not None:
            started_payload["worktree"] = {
                "path": session_worktree.path,
                "branch": session_worktree.branch,
                "repo_root": session_worktree.repo_root,
            }
        self._safe_append_event(
            session_id=session_id,
            event_type="session.started",
            payload=started_payload,
            seq=0,
        )

        try:
            self.server.state.structured.start(
                session_id,
                harness=harness,
                cwd=session_cwd,
                command=command,
                registry=self.server.state.registry,
                on_message=self.server.state.push_event,
                finalize=self._finalize_structured_session,
            )
        except Exception as exc:
            if registry_created:
                self._safe_update_session_status(
                    session_id,
                    "errored",
                    last_error=str(exc),
                    ended_at=datetime.now(timezone.utc),
                )
            self._cleanup_session_worktree(session_id)
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        prompt = body.get("prompt")
        prompt_text = prompt.strip() if isinstance(prompt, str) else ""
        images = body.get("images") or []
        if prompt_text or images:
            try:
                saved = save_turn_attachments(
                    self.server.state.attachments_dir, session_id, images
                )
                text = append_attachment_lines(prompt_text, saved)
                self.server.state.structured.send_turn(
                    session_id,
                    text,
                    images=saved or None,
                    model=model,
                    thinking_effort=thinking_effort,
                )
            except ValueError as exc:
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:
                # Best-effort initial turn; caller can retry via /turns.
                # Log it so a failed first turn isn't completely traceless
                # (no prompt text -- it may contain sensitive content).
                log.debug(
                    "initial structured turn failed for session %s: %s",
                    session_id,
                    exc,
                )

        self._write_json(
            {
                "session_id": session_id,
                "host_id": self.server.state.host_id,
                "harness": harness,
                "status": "running",
                "mode": "structured",
                "model": model,
                "thinking_effort": thinking_effort,
                "registry_synced": registry_created,
            },
            status=HTTPStatus.CREATED,
        )

    def _create_turn(self, session_id: str) -> None:
        if not self.server.state.structured.has(session_id):
            self._write_json(
                {"error": f"unknown structured session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        body = self._read_json() or {}
        text = str(body.get("text") or "").strip()
        images = body.get("images") or []
        if not text and not images:
            self._write_json(
                {"error": "text or images required"}, status=HTTPStatus.BAD_REQUEST
            )
            return
        model = _optional_identifier(body.get("model"))
        thinking_effort = _optional_identifier(body.get("thinking_effort"))
        try:
            client_turn_id = _optional_client_turn_id(body.get("client_turn_id"))
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        harness = self.server.state.structured.harness_for(session_id)
        # Claude owns one persistent process, so later turn preferences cannot
        # affect the running model. Silently ignore overrides from older/direct
        # clients and preserve the startup preferences stored in the registry.
        if harness == "claude-code":
            model = None
            thinking_effort = None
        if model is not None or thinking_effort is not None:
            try:
                self._model_catalog_service().validate(
                    harness or "", model, thinking_effort
                )
            except CatalogSelectionError as exc:
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
        try:
            saved = save_turn_attachments(
                self.server.state.attachments_dir, session_id, images
            )
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        text = append_attachment_lines(text, saved)
        try:
            turn_id = self.server.state.structured.send_turn(
                session_id,
                text,
                images=saved or None,
                model=model,
                thinking_effort=thinking_effort,
                client_turn_id=client_turn_id,
            )
            self.server.state.registry.update_session_preferences(
                session_id,
                model=model,
                thinking_effort=thinking_effort,
            )
        except KeyError:
            # Session was closed by a concurrent /terminate between the has()
            # check above and this call -- treat it as "no longer there".
            self._write_json(
                {"error": f"unknown structured session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        except (PermissionError, RuntimeError) as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self._write_json({"turn_id": turn_id}, status=HTTPStatus.ACCEPTED)

    def _recover_structured_session(self, session_id: str) -> None:
        body = self._read_json()
        native_session_id = _optional_text(
            body.get("native_session_id") if body is not None else None
        )
        with self.server.state.recovery_lock_for(session_id):
            if self.server.state.structured.is_alive(session_id):
                session = self.server.state.registry.get_session(session_id)
                self._write_json(
                    {
                        "session_id": session_id,
                        "status": "running",
                        "recovered": False,
                        "native_session_id": (
                            session.native_session_id if session else native_session_id
                        ),
                    }
                )
                return
            if self.server.state.structured.has(session_id):
                self.server.state.structured.close(session_id)

            session = self.server.state.registry.get_session(session_id)
            if (
                session is None
                or session.mode != "structured"
                or session.status != "errored"
                or session.last_error != _ORPHANED_STRUCTURED_ERROR
                or session.harness not in _RECOVERY_HARNESSES
                or not native_session_id
                or (
                    session.native_session_id
                    and session.native_session_id != native_session_id
                )
                or not session.cwd
                or not Path(session.cwd).is_dir()
            ):
                self._write_json(
                    {"error": _RECOVERY_UNAVAILABLE},
                    status=HTTPStatus.CONFLICT,
                )
                return

            default_command_fn = _STRUCTURED_DEFAULT_COMMANDS.get(session.harness)
            if default_command_fn is None:
                self._write_json(
                    {"error": _RECOVERY_UNAVAILABLE},
                    status=HTTPStatus.CONFLICT,
                )
                return
            command = apply_structured_preferences(
                default_command_fn(),
                harness=session.harness,
                model=session.model,
                thinking_effort=session.thinking_effort,
            )
            try:
                self.server.state.structured.start(
                    session_id,
                    harness=session.harness,
                    cwd=session.cwd,
                    command=command,
                    registry=self.server.state.registry,
                    on_message=self.server.state.push_event,
                    finalize=self._finalize_structured_session,
                    native_session_id=native_session_id,
                )
                if not self.server.state.structured.is_alive(session_id):
                    raise RuntimeError("recovered driver exited during startup")
                self.server.state.registry.mark_session_recovered(
                    session_id, native_session_id
                )
                self.server.state.structured.record_recovered(
                    session_id, native_session_id
                )
            except Exception:
                self.server.state.structured.close(session_id)
                self._safe_update_session_status(
                    session_id,
                    "errored",
                    last_error=_ORPHANED_STRUCTURED_ERROR,
                    ended_at=datetime.now(timezone.utc),
                )
                self._write_json(
                    {"error": _RECOVERY_UNAVAILABLE},
                    status=HTTPStatus.CONFLICT,
                )
                return

            self._write_json(
                {
                    "session_id": session_id,
                    "status": "running",
                    "recovered": True,
                    "native_session_id": native_session_id,
                }
            )

    def _answer_permission(self, session_id: str) -> None:
        if not self.server.state.structured.has(session_id):
            self._write_json(
                {"error": f"unknown structured session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        body = self._read_json() or {}
        request_id = str(body.get("request_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        if not request_id or decision not in {"allow", "deny"}:
            self._write_json(
                {"error": "request_id and decision (allow/deny) are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        note = _optional_text(body.get("note"))
        try:
            self.server.state.structured.answer_permission(
                session_id, request_id, decision, note
            )
        except KeyError:
            self._write_json(
                {"error": f"unknown structured session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        except RuntimeError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._write_json({"ok": True})

    def _interrupt_session(self, session_id: str) -> None:
        if not self.server.state.structured.has(session_id):
            self._write_json(
                {"error": f"unknown structured session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        try:
            self.server.state.structured.interrupt(session_id)
        except KeyError:
            self._write_json(
                {"error": f"unknown structured session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self._write_json({"ok": True})

    def _finalize_structured_session(self, session_id: str, returncode: int) -> None:
        if session_id in self.server.state.terminated_session_ids:
            return  # /terminate already finalized this session; avoid a race
        status = "completed" if returncode == 0 else "errored"
        self._safe_update_session_status(
            session_id,
            status,
            last_error=None if returncode == 0 else f"exited with code {returncode}",
            ended_at=datetime.now(timezone.utc),
        )
        self._safe_append_event(
            session_id=session_id,
            event_type="session.exited",
            payload={"exited": returncode},
            normalized_source="structured",
        )
        self._cleanup_session_worktree(session_id)

    def _cleanup_session_worktree(self, session_id: str) -> None:
        """Reclaim an untouched session worktree; keep one holding work."""
        wt = self.server.state.session_worktrees.pop(session_id, None)
        if wt is None:
            return
        outcome = cleanup_session_worktree(wt)
        log.info(
            "session %s worktree %s (%s): %s",
            session_id,
            wt.path,
            wt.branch,
            outcome,
        )

    def _safe_get_session(self, session_id: str) -> Any:
        try:
            return self.server.state.registry.get_session(session_id)
        except Exception:
            return None

    def _augment_pty_session_json(
        self,
        session: Any,
        registry_rows: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = _pty_session_json(session)
        registry_session = (
            None if registry_rows is None else registry_rows.get(session.session_id)
        )
        if registry_session is None:
            registry_session = self._safe_get_session(session.session_id)
        if registry_session is not None:
            data["mode"] = registry_session.mode or "pty"
            data["awaiting"] = registry_session.awaiting
            data["last_activity"] = (
                registry_session.last_activity.isoformat()
                if registry_session.last_activity
                else None
            )
            data["model"] = registry_session.model
            data["thinking_effort"] = registry_session.thinking_effort
        else:
            data.setdefault("mode", "pty")
            data.setdefault("awaiting", None)
            data.setdefault("last_activity", None)
            data.setdefault("model", None)
            data.setdefault("thinking_effort", None)
        return data

    def _structured_session_json(self, session_id: str) -> dict[str, Any] | None:
        registry_session = self._safe_get_session(session_id)
        if registry_session is None:
            return None
        return _structured_session_row_json(registry_session)

    def _list_sessions(self) -> None:
        self._reconcile_exited_sessions()
        pty_sessions = self.server.state.pty.list_sessions()
        pty_ids = {session.session_id for session in pty_sessions}
        # One registry window for the whole listing. list_sessions() already
        # returns the complete rows, and re-fetching each id with get_session()
        # opened another window per session -- harnessd deliberately does not
        # pin the control-plane connection (see server/db.py), so every window
        # is a full DuckDB instance create/teardown costing 300-500ms on the
        # live hub. A 114-session host spent ~45s in GET /sessions doing 115 of
        # them; this does 1.
        registry_rows: dict[str, Any] = {}
        try:
            registry_rows = {
                row.session_id: row
                for row in self.server.state.registry.list_sessions(
                    host_id=self.server.state.host_id
                )
            }
        except Exception:
            registry_rows = {}
        sessions = [
            self._augment_pty_session_json(session, registry_rows)
            for session in pty_sessions
        ]
        # Union the live in-memory manager with the registry's own view of
        # structured sessions for this host: a session reconciled (or
        # otherwise finalized) after a restart has no manager entry anymore,
        # but its registry row -- now e.g. "errored" -- should still show up
        # here rather than silently disappearing from the listing.
        structured_ids = set(self.server.state.structured.session_ids())
        structured_ids.update(
            session_id
            for session_id, row in registry_rows.items()
            if row.mode == "structured"
        )
        for session_id in structured_ids:
            if session_id in pty_ids:
                continue
            row = registry_rows.get(session_id)
            if row is not None:
                sessions.append(_structured_session_row_json(row))
                continue
            # Manager-only ids: a live structured session whose row the
            # host-scoped listing did not return (or an empty map because the
            # listing failed). Bounded by the live session count, not by the
            # archive, so a per-id lookup here stays cheap.
            structured_json = self._structured_session_json(session_id)
            if structured_json is not None:
                sessions.append(structured_json)
        self._write_json(
            {
                "host_id": self.server.state.host_id,
                "sessions": sessions,
            }
        )

    def _list_native_sessions(self, query: str) -> None:
        params = parse_qs(query)
        limit = int((params.get("limit") or ["20"])[0] or 20)
        harness = _optional_text((params.get("harness") or [None])[0])
        cwd = _optional_text((params.get("cwd") or [None])[0])
        sessions = discover_native_resume_sessions(
            harness=harness,
            cwd=cwd,
            limit=limit,
        )
        self._write_json(
            {
                "host_id": self.server.state.host_id,
                "sessions": sessions,
            }
        )

    def _get_native_transcript(self, session_id: str, query: str) -> None:
        if not session_id:
            self._write_json(
                {"error": "missing session id"}, status=HTTPStatus.NOT_FOUND
            )
            return
        self._reconcile_exited_sessions()
        session = self.server.state.pty.get(session_id)
        if session is None:
            self._write_json(
                {"error": f"unknown terminal session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        params = parse_qs(query)
        limit = int((params.get("limit") or ["80"])[0] or 80)
        native_session_id = _optional_text(
            (params.get("native_session_id") or [None])[0]
        )
        harness = _harness_name_for_command(session.command)
        payload = native_transcript_for_session(
            harness=harness,
            cwd=str(session.cwd) if session.cwd else None,
            native_session_id=native_session_id,
            limit=limit,
        )
        provider_session_id = payload.get("session_id")
        payload.update(
            {
                "host_id": self.server.state.host_id,
                "session_id": session_id,
                "native_session_id": provider_session_id,
                "harness": harness,
                "cwd": str(session.cwd) if session.cwd else None,
            }
        )
        self._write_json(payload)

    def _get_session(self, session_id: str) -> None:
        if not session_id:
            self._write_json(
                {"error": "missing session id"}, status=HTTPStatus.NOT_FOUND
            )
            return
        self._reconcile_exited_sessions()
        session = self.server.state.pty.get(session_id)
        if session is not None:
            self._write_json(self._augment_pty_session_json(session))
            return
        structured_json = self._structured_session_json(session_id)
        if structured_json is not None:
            self._write_json(structured_json)
            return
        self._write_json(
            {"error": f"unknown terminal session: {session_id}"},
            status=HTTPStatus.NOT_FOUND,
        )

    def _terminate_session(self, session_id: str) -> None:
        if not session_id:
            self._write_json(
                {"error": "missing session id"}, status=HTTPStatus.NOT_FOUND
            )
            return
        self._reconcile_exited_sessions()
        pty_session = self.server.state.pty.get(session_id)
        if pty_session is None:
            with self.server.state.recovery_lock_for(session_id):
                try:
                    registry_session = self.server.state.registry.get_session(
                        session_id
                    )
                except Exception:
                    registry_session = None
                if self.server.state.structured.has(session_id) or (
                    registry_session is not None
                    and registry_session.mode == "structured"
                    and registry_session.status not in {"completed", "terminated"}
                ):
                    self._terminate_structured_session(session_id)
                    return
            self._write_json(
                {"error": f"unknown terminal session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        terminated = self.server.state.pty.terminate(session_id)
        if terminated:
            self.server.state.terminated_session_ids.add(session_id)
            self._finalize_session(session_id, "terminated", "session.terminated")
        self._write_json(
            {
                "session_id": session_id,
                "host_id": self.server.state.host_id,
                "status": "terminated",
                "terminated": terminated,
            }
        )

    def _terminate_structured_session(self, session_id: str) -> None:
        # Mark terminated BEFORE closing the driver: closing may (via the
        # driver's own pump thread) synchronously emit a "process exited"
        # status message, which would otherwise race
        # _finalize_structured_session into re-finalizing a session this
        # method is already finalizing.
        self.server.state.terminated_session_ids.add(session_id)
        self.server.state.structured.close(session_id)
        self._safe_update_session_status(
            session_id,
            "terminated",
            last_error=None,
            ended_at=datetime.now(timezone.utc),
        )
        self._safe_append_event(
            session_id=session_id,
            event_type="session.terminated",
            normalized_source="structured",
        )
        self._cleanup_session_worktree(session_id)
        self._write_json(
            {
                "session_id": session_id,
                "host_id": self.server.state.host_id,
                "status": "terminated",
                "terminated": True,
            }
        )

    def _attach_terminal(self, path: str) -> None:
        session_id = (
            path.removeprefix("/sessions/").removesuffix("/terminal").strip("/")
        )
        if not session_id:
            self._write_json(
                {"error": "missing session id"}, status=HTTPStatus.NOT_FOUND
            )
            return
        if self.server.state.pty.get(session_id) is None:
            self._write_json(
                {"error": f"unknown terminal session: {session_id}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._write_json(
                {"error": "terminal attach requires websocket upgrade"},
                status=HTTPStatus.UPGRADE_REQUIRED,
            )
            return
        websocket_key = self.headers.get("Sec-WebSocket-Key")
        if not websocket_key:
            self._write_json(
                {"error": "missing Sec-WebSocket-Key"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        # HTTP/1.1 status line required by strict WebSocket clients
        # (URLSessionWebSocketTask rejects "HTTP/1.0 101"). Scoped to this
        # hijacked upgrade response only.
        self.protocol_version = "HTTP/1.1"
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(websocket_key))
        self.end_headers()
        self.close_connection = True
        self._terminal_loop(session_id)

    def _terminal_loop(self, session_id: str) -> None:
        sock = self.connection
        previous_timeout = sock.gettimeout()
        sock.settimeout(0.05)
        self._safe_append_event(
            session_id=session_id,
            event_type="terminal.attached",
        )
        # Resolved once: the hot loop must not pay a registry read (a
        # connect-lock window) per keystroke just to re-learn a harness
        # name that never changes for the session's lifetime. Same
        # swallow stance as _safe_append_event: a failing registry only
        # costs normalization hints, never the terminal.
        try:
            session = self.server.state.registry.get_session(session_id)
        except Exception:
            session = None
        session_harness = session.harness if session else None
        mirror = _TerminalMirror(self.server.state.registry)
        try:
            send_json(sock, {"type": "attached", "session_id": session_id})
            # Replay buffered scrollback so a reattach isn't a blank screen
            # until the process emits its next byte. Not appended to the
            # transcript/events — it already went through both when first
            # read; this is purely a client-side repaint.
            scrollback = self.server.state.pty.scrollback(session_id)
            if scrollback:
                send_json(
                    sock,
                    {
                        "type": "output",
                        "data": scrollback.decode("utf-8", errors="replace"),
                    },
                )
            last_exit_check = monotonic()
            attach_ts = monotonic()
            last_output_ts: float | None = None
            # Rolling tail of startup output, kept only while a handoff seed
            # is pending so _maybe_deliver_pending_seed can spot a startup
            # gate (e.g. claude-code's trust prompt). Bounded: the gate text
            # always fits well inside the last 4 KiB of a startup screen.
            seed_watch = ""
            while True:
                output = self.server.state.pty.read(
                    session_id,
                    max_bytes=8192,
                    timeout_s=0.02,
                )
                if output:
                    last_output_ts = monotonic()
                    text = output.decode("utf-8", errors="replace")
                    if session_id in self.server.state.pending_initial_input:
                        seed_watch = (seed_watch + text)[-4096:]
                    content = _redact_terminal_text(text)
                    # Echo first — wire delivery never waits on registry
                    # bookkeeping (the whole point of _TerminalMirror).
                    send_json(sock, {"type": "output", "data": text})
                    event = _build_mirror_event(
                        mirror,
                        session_id=session_id,
                        event_type="terminal.output",
                        harness=session_harness,
                        payload={
                            "byte_count": len(output),
                            "text": content,
                        },
                        normalized_source="inferred_terminal",
                        content_preview=content,
                    )
                    send_json(sock, {"type": "event", "event": _event_json(event)})

                # Type any queued handoff seed once the CLI's output settles
                # (answering a startup gate first if one is on screen).
                self._maybe_deliver_pending_seed(
                    session_id,
                    attach_ts=attach_ts,
                    last_output_ts=last_output_ts,
                    recent_output=seed_watch,
                )

                try:
                    message = recv_json(sock)
                except socket.timeout:
                    message = None
                if message is not None:
                    if not self._handle_terminal_message(
                        session_id, message, sock, mirror, session_harness
                    ):
                        return

                if monotonic() - last_exit_check >= 0.2:
                    last_exit_check = monotonic()
                    if not self.server.state.pty.is_alive(session_id):
                        if session_id not in self.server.state.terminated_session_ids:
                            self._finalize_session(
                                session_id, "completed", "session.exited"
                            )
                        send_json(sock, {"type": "exit"})
                        return
        except (BrokenPipeError, ConnectionResetError, KeyError, WebSocketClosed):
            if (
                session_id not in self.server.state.terminated_session_ids
                and not self.server.state.pty.is_alive(session_id)
            ):
                self._finalize_session(session_id, "completed", "session.exited")
            return
        finally:
            sock.settimeout(previous_timeout)
            # Flush queued mirror writes before the detach marker so the
            # recorded stream is complete when the detach event lands.
            mirror.stop()
            self._safe_append_event(
                session_id=session_id,
                event_type="terminal.detached",
            )

    def _handle_terminal_message(
        self,
        session_id: str,
        message: dict[str, Any],
        sock: socket.socket,
        mirror: _TerminalMirror,
        harness: str | None,
    ) -> bool:
        message_type = message.get("type")
        if message_type == "input":
            data = str(message.get("data") or "")
            self.server.state.pty.write(session_id, data)
            event = _build_mirror_event(
                mirror,
                session_id=session_id,
                event_type="terminal.input",
                harness=harness,
                payload={
                    "byte_count": len(data.encode("utf-8")),
                    "text": _redact_terminal_text(data),
                },
                normalized_source="inferred_terminal",
                content_preview=_redact_terminal_text(data),
            )
            send_json(sock, {"type": "event", "event": _event_json(event)})
            return True
        if message_type == "interrupt":
            self.server.state.pty.write(session_id, b"\x03")
            event = _build_mirror_event(
                mirror,
                session_id=session_id,
                event_type="terminal.interrupt",
                harness=harness,
                normalized_type="status",
                normalized_source="nexus_control",
                content_preview="Ctrl-C sent",
            )
            send_json(sock, {"type": "event", "event": _event_json(event)})
            return True
        if message_type == "resize":
            rows = int(message.get("rows") or 24)
            cols = int(message.get("cols") or 80)
            self.server.state.pty.resize(session_id, rows=rows, cols=cols)
            _build_mirror_event(
                mirror,
                session_id=session_id,
                event_type="terminal.resized",
                harness=harness,
                payload={"rows": rows, "cols": cols},
            )
            return True
        if message_type == "ping":
            send_json(sock, {"type": "pong"})
            return True
        if message_type in {"close", "detach"}:
            # Flush before acking: a client that has observed the close
            # frame may immediately read the registry and must find the
            # stream durably recorded. (stop() is idempotent; the loop's
            # finally calls it again on every exit path.)
            mirror.stop()
            send_close(sock)
            return False
        send_json(
            sock, {"type": "error", "error": f"unknown message type: {message_type}"}
        )
        return True

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length) if length else b"{}"
            loaded = json.loads(data.decode("utf-8"))
            return loaded if isinstance(loaded, dict) else None
        except Exception:
            return None

    def _write_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self._write_cors_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _safe_append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        normalized_type: str | None = None,
        normalized_source: str | None = None,
        content_preview: str | None = None,
        seq: int | None = None,
    ):
        try:
            session = self.server.state.registry.get_session(session_id)
            event = self.server.state.registry.append_event(
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                harness=session.harness if session else None,
                normalized_type=normalized_type,
                normalized_source=normalized_source,
                content_preview=content_preview,
                seq=seq,
            )
            return event
        except Exception:
            return None

    def _safe_update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        last_error: str | None = None,
        ended_at: datetime | None = None,
    ) -> None:
        try:
            self.server.state.registry.update_session_status(
                session_id,
                status,
                last_error=last_error,
                ended_at=ended_at,
            )
        except Exception:
            return

    def _finalize_session(
        self,
        session_id: str,
        status: str,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        # An undelivered handoff seed (session ended before any terminal
        # attached) has nowhere to go now — drop it so it can't leak.
        self.server.state.pending_initial_input.pop(session_id, None)
        self._safe_update_session_status(
            session_id,
            status,
            ended_at=datetime.now(timezone.utc),
        )
        self._safe_append_event(
            session_id=session_id,
            event_type=event_type,
            payload=payload,
        )

    def _reconcile_exited_sessions(self) -> None:
        for session in self.server.state.pty.reap_exited():
            self._finalize_session(
                session.session_id,
                "completed",
                "session.exited",
                payload={"pid": session.pid},
            )


_ORPHANED_STRUCTURED_ERROR = "daemon restarted; structured session lost"
_ORPHANED_PTY_ERROR = "daemon restarted; PTY session lost"
_ORPHANED_STATUSES = ("created", "starting", "running")


def reconcile_structured_sessions(state: HarnessDaemonState) -> None:
    """Finalize sessions orphaned by a killed daemon process.

    A daemon restart (crash, deploy, `systemctl restart`, ...) kills every
    driver subprocess and PTY child without any chance to finalize their
    registry rows: the in-memory StructuredSessionManager and
    PtySessionManager that owned them are gone too, so no /turns,
    /permission, or terminal attach can ever reach them again. Any row for
    this host still "created"/"starting"/"running" from before this process
    started is therefore stale:

    - mode="structured" rows are marked errored so they stop looking like a
      live session (see _list_sessions, which now surfaces registry-only
      structured rows precisely so a reconciled row like this stays visible
      instead of silently vanishing).
    - PTY rows (mode="pty" or unset) are finalized as completed -- the fresh
      PtySessionManager is always empty at boot, so none of them can have a
      live PTY behind them.
    """
    try:
        sessions = state.registry.list_sessions(host_id=state.host_id)
    except Exception:
        return
    for session in sessions:
        if session.status not in _ORPHANED_STATUSES:
            continue
        if session.mode == "structured":
            status, last_error = "errored", _ORPHANED_STRUCTURED_ERROR
        else:
            status, last_error = "completed", _ORPHANED_PTY_ERROR
        try:
            state.registry.update_session_status(
                session.session_id,
                status,
                last_error=last_error,
                ended_at=datetime.now(timezone.utc),
            )
        except Exception:
            continue


def _with_claude_plan_label(
    detected: DetectedProvider, auth: AuthFlowManager
) -> DetectedProvider:
    try:
        status = auth.status("claude-code")
    except (KeyError, RuntimeError):
        return detected
    plan_label = (
        status.get("detail") if status.get("state") == "authenticated" else None
    )
    return replace(
        detected,
        plan_label=plan_label if isinstance(plan_label, str) and plan_label else None,
    )


def _detected_provider_json(detected: DetectedProvider) -> dict[str, Any]:
    return {
        "provider": detected.provider,
        "account_label": detected.account_label,
        "host_id": detected.host_id,
        "harnesses": list(detected.harnesses),
        "plan_label": detected.plan_label,
        "usage_status": detected.usage_status,
    }


def _unavailable_provider_json(
    detected: DetectedProvider, observed_at: datetime
) -> dict[str, Any]:
    payload = _detected_provider_json(detected)
    fingerprint = json.dumps(
        {
            "provider": detected.provider,
            "account_label": detected.account_label,
            "host_id": detected.host_id,
            "plan_label": detected.plan_label,
            "status": "usage_unavailable",
            "source": "harness-inventory",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        **payload,
        "snapshot_id": str(uuid4()),
        "dedup_key": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(),
        "status": "usage_unavailable",
        "observed_at": observed_at.isoformat(),
        "source": "harness-inventory",
        "error_category": None,
        "windows": [],
    }


def _provider_snapshot_json(
    snapshot: ProviderAccountSnapshot, detected: DetectedProvider
) -> dict[str, Any]:
    # Every failure path in ClaudeUsageProbe.read() (token_expired, timeout,
    # unavailable, protocol_error, ...) hardcodes plan_label=None on the
    # snapshot -- including failures that happen before credentials are even
    # read, so there is nowhere upstream to plug this in once. Falling back
    # to detected.plan_label here keeps the plan (e.g. "max") on a degraded
    # Anthropic card instead of losing it on every failed refresh. This is a
    # no-op for the openai/Codex branch: inventory.py always sets
    # plan_label=None on DetectedProvider and _with_claude_plan_label only
    # enriches the anthropic branch, so detected.plan_label is None there too.
    plan_label = (
        snapshot.plan_label if snapshot.plan_label is not None else detected.plan_label
    )
    return {
        **_detected_provider_json(detected),
        "account_label": snapshot.account_label,
        "plan_label": plan_label,
        "status": snapshot.status,
        "snapshot_id": snapshot.snapshot_id,
        "dedup_key": snapshot.dedup_key,
        "observed_at": snapshot.observed_at.isoformat(),
        "source": snapshot.source,
        "error_category": snapshot.error_category,
        "windows": [_provider_window_json(window) for window in snapshot.windows],
    }


def _provider_window_json(window: ProviderUsageWindow) -> dict[str, Any]:
    return {
        "kind": window.kind,
        "used_percent": window.used_percent,
        "limit_value": window.limit_value,
        "remaining_value": window.remaining_value,
        "unit": window.unit,
        "window_minutes": window.window_minutes,
        "starts_at": window.starts_at.isoformat() if window.starts_at else None,
        "resets_at": window.resets_at.isoformat() if window.resets_at else None,
    }


def _parse_auth_route(path: str) -> tuple[str, str | None, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if len(parts) == 3 and parts[0] == "auth" and parts[2] in {"status", "start"}:
        return parts[1], None, parts[2]
    if len(parts) == 4 and parts[0] == "auth" and parts[2] == "flows":
        return parts[1], parts[3], "flow"
    if (
        len(parts) == 5
        and parts[0] == "auth"
        and parts[2] == "flows"
        and parts[4] in {"cancel", "input"}
    ):
        return parts[1], parts[3], parts[4]
    return None


def _valid_content_target_ids(value: Any) -> bool:
    if not isinstance(value, list) or not value or len(value) > 256:
        return False
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > 256
        or item.strip() != item
        or item in {".", ".."}
        or "/" in item
        or "\\" in item
        for item in value
    ):
        return False
    return len(set(value)) == len(value)


def create_harness_server(
    *,
    listen_host: str,
    listen_port: int,
    state: HarnessDaemonState,
) -> HarnessHTTPServer:
    # Run before serving a single request: reconciliation must complete
    # before any client can observe a stale "running" row that no driver
    # backs. Called here (not only from run_harnessd) so tests that build
    # the server directly against a pre-seeded registry -- simulating a
    # restart -- get the same reconciliation a real daemon boot would.
    reconcile_structured_sessions(state)
    return HarnessHTTPServer((listen_host, listen_port), HarnessRequestHandler, state)


def _updater_status(state: HarnessDaemonState) -> dict[str, Any] | None:
    """This host's update state, or None when [update] is switched off.

    `updater` is a declared field, not an optional attribute: it is None
    whenever the config kill switch is off, and that is the only case this
    guards. A missing attribute should raise, not silently disable reporting.
    """
    updater = state.updater
    return updater.status() if updater is not None else None


def register_daemon_host(state: HarnessDaemonState) -> None:
    try:
        from drover import __version__ as drover_version

        state.registry.register_host(
            host_id=state.host_id,
            display_name=state.display_name,
            kind=state.kind,
            local_url=state.local_url,
            tailscale_url=state.tailscale_url,
            capabilities=state.capabilities(),
            agent_version=drover_version,
            update=_updater_status(state),
        )
    except Exception:
        return


def register_daemon_host_remote(state: HarnessDaemonState) -> dict[str, Any] | None:
    """Register, and return the hub's response body, or None on failure.

    The body is the hub-to-host control channel: it already carries
    content_consent, and now target_version too.
    """
    if not state.central_url:
        return None
    from drover import __version__ as drover_version

    payload = {
        "host_id": state.host_id,
        "display_name": state.display_name,
        "kind": state.kind,
        "local_url": state.local_url,
        "tailscale_url": state.tailscale_url,
        "status": "online",
        "connection_kind": "relay" if state.relay else "direct",
        "capabilities": state.capabilities(),
        # So the hub can see version skew across the fleet without asking.
        "agent_version": drover_version,
        "update": _updater_status(state),
    }
    return _post_central_json(state, "/harness/hosts", payload)


def _heartbeat_once(state: HarnessDaemonState) -> None:
    """One beat: register, then act on whatever the hub said back.

    Split out of the loop so the wiring is testable without a thread and a
    fifteen-second sleep. That the loop discarded this body is precisely how
    fleet auto-update shipped inert.
    """
    body = register_daemon_host_remote(state)
    if body is None:
        return
    # An empty body is a successful registration with nothing to say, so this
    # tests `is None` rather than truthiness; the watchdog reads this flag to
    # decide whether a freshly activated version can talk to the hub at all.
    state.registered_at_least_once = True
    _schedule_event_reconciliation(state)
    updater = state.updater
    if updater is not None:
        updater.observe(body)
        updater.maybe_activate()


def _reconcile_persisted_events(state: HarnessDaemonState) -> bool:
    """Complete one durable-ledger replay pass, retrying after hub recovery."""
    pusher = getattr(state, "pusher", None)
    if pusher is None:
        return True
    if not state.event_reconciliation_pending:
        return True
    with state.event_reconciliation_lock:
        if not state.event_reconciliation_pending:
            return True
        result = reconcile_unsent_events(
            state.registry,
            pusher,
            host_id=state.host_id,
        )
        if result is None:
            log.warning("structured event reconciliation deferred until hub recovery")
            return False
        state.event_reconciliation_pending = False
        log.info("structured event reconciliation completed: offered=%d", result)
        return True


def _schedule_event_reconciliation(
    state: HarnessDaemonState,
) -> threading.Thread | None:
    """Wake one background replay worker without delaying heartbeat liveness."""
    pusher = getattr(state, "pusher", None)
    if pusher is None or not state.event_reconciliation_pending:
        return None
    with state.event_reconciliation_schedule_lock:
        if not state.event_reconciliation_pending:
            return None
        existing = getattr(state, "event_reconciliation_thread", None)
        if existing is not None and existing.is_alive():
            return existing
        thread = threading.Thread(
            target=_reconcile_persisted_events,
            args=(state,),
            name="drover-event-reconciliation",
            daemon=True,
        )
        state.event_reconciliation_thread = thread
        thread.start()
        return thread


def _rollback_watchdog(
    state: HarnessDaemonState,
    layout: RuntimeLayout,
    *,
    deadline_seconds: float = REGISTRATION_DEADLINE_SECONDS,
    sleep=time.sleep,
    restarter=default_restarter,
) -> None:
    """Undo a flip whose new version cannot reach the hub.

    Only ever does anything when a rollback marker is on disk, so on an
    ordinary start this reads the marker, finds none, and returns.
    """
    deadline = monotonic() + deadline_seconds
    while monotonic() < deadline and not state.registered_at_least_once:
        sleep(2)
    if verify_after_restart(layout, registered=state.registered_at_least_once):
        return
    # The symlink now points back at the previous version, but this process is
    # still the new one. The service manager owns the restart; asking it to
    # bounce us is what actually puts the old version back in memory.
    restarter()


def _start_rollback_watchdog(
    state: HarnessDaemonState, layout: RuntimeLayout
) -> threading.Thread:
    thread = threading.Thread(
        target=_rollback_watchdog,
        args=(state, layout),
        name="drover-harnessd-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def start_remote_heartbeat(state: HarnessDaemonState) -> threading.Thread | None:
    if not state.central_url:
        return None

    def loop() -> None:
        while True:
            _heartbeat_once(state)
            time.sleep(15)

    thread = threading.Thread(
        target=loop, name="drover-harnessd-heartbeat", daemon=True
    )
    thread.start()
    return thread


def _post_central_json(
    state: HarnessDaemonState,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """POST to the hub and return its parsed body, or None on any failure.

    The body is the hub-to-host control channel. It already carried
    content_consent; target_version rides the same path rather than opening a
    second one.

    Note that an empty body is ``{}`` and means success. Callers must test
    ``is None`` rather than truthiness, or a hub with nothing to say will read
    as an unreachable hub.
    """
    if not state.central_url:
        return None
    base = state.central_url.rstrip("/")
    request = Request(
        f"{base}{path}",
        data=json.dumps(payload, sort_keys=True).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if state.host_token:
        request.add_header("Authorization", f"Bearer {state.host_token}")
    try:
        with urlopen(request, timeout=5) as response:
            if not 200 <= response.status < 300:
                return None
            body = response.read(64 * 1024 + 1)
            if len(body) > 64 * 1024:
                return None
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(parsed, dict):
                return None
            remote_consent = parsed.get("content_consent")
            if remote_consent is not None:
                if (
                    not isinstance(remote_consent, dict)
                    or set(remote_consent) != {"enabled", "epoch"}
                    or state.content_consent is None
                ):
                    return None
                state.content_consent.apply(
                    enabled=remote_consent["enabled"], epoch=remote_consent["epoch"]
                )
            return parsed
    except (OSError, URLError, ValueError):
        return None


def resolve_daemon_token(host_token: str | None) -> str:
    """--host-token flag > DROVER_API_TOKEN env > ~/.drover/api_token file > ''."""
    if host_token:
        return host_token
    env = resolve_api_token_env()
    if env:
        return env
    token_file = default_token_file()
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def wire_event_pusher(state: HarnessDaemonState) -> EventPusher | None:
    """Point state.push_event at a started EventPusher when configured.

    Requires both a central URL and an API token; otherwise the no-op
    default stays and structured sessions still work locally (events just
    aren't pushed anywhere).
    """
    if not (state.central_url and state.api_token):
        return None
    pusher = EventPusher(state.central_url, state.api_token)
    pusher.start()
    state.push_event = pusher.push
    state.pusher = pusher
    _schedule_event_reconciliation(state)
    return pusher


def run_harnessd(
    *,
    host_id: str,
    display_name: str,
    kind: str,
    duckdb_path: Path,
    listen_host: str,
    listen_port: int,
    local_url: str | None = None,
    tailscale_url: str | None = None,
    central_url: str | None = None,
    host_token: str | None = None,
    relay: bool = False,
    advisory_content: "AdvisoryContentConfig | None" = None,
    content_consent_path: Path | None = None,
    cfg: "DroverConfig | None" = None,
) -> None:
    consent_path = content_consent_path or (
        Path(duckdb_path).parent / ".harness-content-consent.json"
    )
    content_consent = DurableContentConsent(
        consent_path,
        initial_enabled=bool(
            not central_url
            and advisory_content is not None
            and advisory_content.enabled
        ),
    )
    state = HarnessDaemonState(
        host_id=host_id,
        display_name=display_name,
        kind=kind,
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=resolve_harness_presets(),
        local_url=local_url,
        tailscale_url=tailscale_url,
        central_url=central_url,
        host_token=host_token,
        relay=relay,
        advisory_content=advisory_content,
        content_consent=content_consent,
    )
    state.api_token = resolve_daemon_token(host_token)
    state.host_token = state.api_token
    if not state.api_token:
        log.warning(
            "harnessd running WITHOUT auth: set DROVER_API_TOKEN or --host-token"
        )
    if cfg is not None and cfg.update_enabled:
        # Constructed here rather than passed into the state, because it takes
        # the state itself to ask whether this host is idle.
        layout = RuntimeLayout(config_home())
        state.updater = HostUpdater(state, layout, cfg)
        _start_rollback_watchdog(state, layout)
    server = create_harness_server(
        listen_host=listen_host,
        listen_port=listen_port,
        state=state,
    )
    # Bind before any historical replay starts. A large durable ledger must
    # never hold the daemon socket closed or trip the updater's liveness
    # watchdog during an otherwise healthy restart.
    pusher = wire_event_pusher(state)
    register_daemon_host(state)
    _heartbeat_once(state)
    start_remote_heartbeat(state)
    # After create_harness_server, and on its *bound* port: announcing a live
    # relay before the socket is bound gives the hub a window in which every
    # proxied call 502s, and listen_port is 0 whenever the port is ephemeral.
    # Binding is enough - connections queue in the backlog until serve_forever.
    relay_client: RelayClient | None = None
    if relay:
        if state.central_url and state.api_token:
            relay_client = RelayClient(
                state.central_url, state.host_id, state.api_token, server.server_port
            )
            relay_client.start()
        else:
            # Serving locally is still useful; the hub just cannot reach us.
            log.error("--relay ignored: it needs both --central-url and an API token")
    try:
        server.serve_forever()
    finally:
        state.pty.close_all()
        state.auth.close_all()
        if relay_client is not None:
            relay_client.stop()
        if pusher is not None:
            pusher.stop()
        server.server_close()


def _command_label(command: Any) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list | tuple):
        return " ".join(str(part) for part in command)
    return str(command)


def _pty_session_json(session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "command": list(session.command),
        "cwd": str(session.cwd) if session.cwd else None,
        "pid": session.pid,
        "status": "running",
    }


def _structured_session_row_json(registry_session: Any) -> dict[str, Any]:
    """The single definition of a structured session's listing shape.

    Both the per-id lookup and the batched listing format rows through here so
    the two paths cannot drift.
    """
    return {
        "session_id": registry_session.session_id,
        "command": registry_session.command,
        "cwd": registry_session.cwd,
        "pid": None,
        "status": registry_session.status,
        "mode": registry_session.mode or "structured",
        "awaiting": registry_session.awaiting,
        "model": registry_session.model,
        "thinking_effort": registry_session.thinking_effort,
        "last_activity": (
            registry_session.last_activity.isoformat()
            if registry_session.last_activity
            else None
        ),
    }


def _harness_name_for_command(command: tuple[str, ...]) -> str:
    command_text = " ".join(str(part) for part in command)
    if "claude" in command_text:
        return "claude-code"
    if "codex" in command_text:
        return "codex"
    if "agy" in command_text or "antigravity" in command_text:
        return "agy"
    if "openclaw" in command_text:
        return "openclaw"
    return "shell"


def _event_json(event: Any) -> dict[str, Any]:
    item = dict(event.__dict__)
    created_at = item.get("created_at")
    if hasattr(created_at, "isoformat"):
        item["created_at"] = created_at.isoformat()
    return item


class _TerminalMirror:
    """Off-loop writer for the terminal loop's audit bookkeeping.

    Recording used to happen inline in the echo path: every keystroke paid
    an input-event append, then its echo paid a transcript append plus an
    output-event append — each a fresh DuckDB connect window under the
    registry's process-wide lock (~300-500ms per key on a busy host,
    measured; see ``append_events_if_new``'s docstring for the lock
    economics). The loop now sends wire frames immediately and queues
    records here; one worker thread per attachment drains the queue and
    pays a single connect window per batch. Recording stays durable and
    idempotent (caller-generated event_ids), just not on the echo's clock.
    """

    def __init__(self, registry: HarnessRegistry) -> None:
        self._registry = registry
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="terminal-mirror", daemon=True
        )
        self._thread.start()

    def record_event(self, record: dict[str, Any]) -> None:
        self._queue.put(("event", record))

    def stop(self, timeout_s: float = 5.0) -> None:
        """Flush queued records and stop the worker (bounded wait)."""
        self._closing.set()
        self._queue.put(None)  # wake the worker if it's blocked on get()
        self._thread.join(timeout=timeout_s)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            batch = [] if item is None else [item]
            while True:
                try:
                    extra = self._queue.get_nowait()
                except queue.Empty:
                    break
                if extra is not None:
                    batch.append(extra)
            self._flush(batch)
            if self._closing.is_set() and self._queue.empty():
                return

    def _flush(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        events = [record for kind, record in batch if kind == "event"]
        if not events:
            return
        # Same never-raise stance as _safe_append_event: a locked or failing
        # registry must never take the terminal down with it. But a bare
        # swallow lost the events forever and invisibly, so retry first --
        # DuckDB write-write conflicts under concurrent writers are
        # transient -- and count whatever still cannot be written.
        for attempt in range(3):
            try:
                self._registry.append_events_if_new(events)
                return
            except Exception:
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
        record_dropped_events(len(events))
        # Leave a marker so the transcript shows a hole instead of quietly
        # reading as complete. Best-effort by definition: if the registry is
        # down hard, this fails too and only the counter records the loss.
        try:
            self._registry.append_event(
                session_id=events[0].get("session_id", ""),
                event_type="transcript.gap",
                payload={"dropped": len(events)},
                normalized_type="status",
            )
        except Exception:
            pass


def _build_mirror_event(
    mirror: _TerminalMirror,
    *,
    session_id: str,
    event_type: str,
    harness: str | None,
    payload: dict[str, Any] | None = None,
    normalized_type: str | None = None,
    normalized_source: str | None = None,
    content_preview: str | None = None,
) -> HarnessEvent:
    """Build a terminal event locally and queue its durable write.

    The returned event feeds the wire mirror frame without a DB read-back;
    ``append_events_if_new`` re-runs the same ``normalize_harness_event``
    on the queued record, so the stored row matches the wire frame.
    """
    normalized = normalize_harness_event(
        event_type=event_type,
        payload=payload,
        harness=harness,
        normalized_type=normalized_type,
        normalized_source=normalized_source,
        content_preview=content_preview,
    )
    event = HarnessEvent(
        event_id=f"harness-event-{uuid4()}",
        session_id=session_id,
        event_type=event_type,
        normalized_type=normalized["normalized_type"],
        normalized_source=normalized["normalized_source"],
        content_preview=normalized["content_preview"],
        payload=payload or {},
        created_at=datetime.now(timezone.utc),
    )
    mirror.record_event(
        {
            "event_id": event.event_id,
            "session_id": session_id,
            "event_type": event_type,
            "payload": payload,
            "harness": harness,
            "normalized_type": normalized_type,
            "normalized_source": normalized_source,
            "content_preview": content_preview,
            "created_at": event.created_at,
        }
    )
    return event


def _redact_terminal_text(text: str) -> str:
    # Minimal first-pass redaction. Richer secret scanning belongs in a later slice.
    redacted = text
    for marker in ("TOKEN=", "API_KEY=", "SECRET=", "PASSWORD="):
        index = redacted.find(marker)
        if index == -1:
            continue
        start = index + len(marker)
        end = start
        while end < len(redacted) and not redacted[end].isspace():
            end += 1
        redacted = f"{redacted[:start]}[REDACTED]{redacted[end:]}"
    return redacted
