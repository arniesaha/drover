"""Validation and projection for the stateless Factory observer launch bridge.

This module deliberately has no store and makes no Factory calls.  Factory's
TaskFlow ledger remains the only authority for run lifecycle; Drover only
normalizes a constrained request into its existing host-local ``/sessions``
launch contract and projects the resulting harness session back to the UI.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

_MODE = "factory_observer"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,190}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,190}$")


class FactoryObserverRequestError(ValueError):
    """The bridge request is malformed or asks Drover to control Factory."""


@dataclass(frozen=True)
class FactoryObserverLaunch:
    run_id: str
    expected_revision: int
    idempotency_key: str
    target_hostname: str
    repo_owner: str
    repo_name: str
    branch: str
    cwd: str

    @property
    def source_session_id(self) -> str:
        # Reuse the existing generic handoff reference.  It is only a display
        # and correlation reference here, never a Factory lifecycle record.
        return f"factory/{self.run_id}@{self.expected_revision}"

    @property
    def client_session_id(self) -> str:
        # The existing harness-session unique index is the idempotency fence.
        # Hashing keeps its generic field bounded and never stores a Factory
        # key in the control-plane session row.
        material = f"{self.run_id}\0{self.expected_revision}\0{self.idempotency_key}"
        return "factory-observer-" + hashlib.sha256(material.encode()).hexdigest()

    def wire_projection(
        self, *, status: str, worktree: dict[str, str] | None
    ) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "run_id": self.run_id,
            "expected_revision": self.expected_revision,
            "target_hostname": self.target_hostname,
            "status": status,
            "authority": "taskflow",
        }
        if worktree is not None:
            projection["worktree"] = worktree
        return projection


def parse_factory_observer_launch(
    body: Mapping[str, Any], *, host_id: str
) -> tuple[FactoryObserverLaunch, dict[str, Any]] | None:
    """Return a normalized existing structured-launch payload, if requested.

    The only accepted Factory operation is a launch.  No Factory mutation
    vocabulary is accepted, and the command must come from the selected
    harness adapter rather than the caller.
    """
    raw = body.get("factory_observer")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise FactoryObserverRequestError("factory_observer must be an object")
    if set(body) - {"factory_observer", "harness", "model", "thinking_effort"}:
        raise FactoryObserverRequestError(
            "factory observer launch has unsupported fields"
        )
    forbidden = {
        "approve",
        "cancel",
        "advance",
        "resume",
        "finish",
        "action",
        "operation",
    }
    if forbidden & set(raw):
        raise FactoryObserverRequestError("Factory mutation controls are not supported")
    required = {
        "run_id",
        "expected_revision",
        "idempotency_key",
        "target_hostname",
        "repo",
        "worktree",
        "command",
    }
    if set(raw) != required:
        raise FactoryObserverRequestError(
            "factory observer request has missing or unsupported fields"
        )

    run_id = raw["run_id"]
    key = raw["idempotency_key"]
    revision = raw["expected_revision"]
    target = raw["target_hostname"]
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise FactoryObserverRequestError("factory run_id is invalid")
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise FactoryObserverRequestError("factory idempotency_key is invalid")
    if type(revision) is not int or revision < 0:
        raise FactoryObserverRequestError(
            "factory expected_revision must be a non-negative integer"
        )
    if not isinstance(target, str) or not target or target != host_id:
        raise FactoryObserverRequestError(
            "factory target_hostname must match this host"
        )

    repo = raw["repo"]
    worktree = raw["worktree"]
    if not isinstance(repo, Mapping) or set(repo) != {"owner", "name", "branch"}:
        raise FactoryObserverRequestError(
            "factory repo must contain owner, name, and branch"
        )
    if not isinstance(worktree, Mapping) or set(worktree) != {"cwd", "policy"}:
        raise FactoryObserverRequestError(
            "factory worktree must contain cwd and policy"
        )
    if raw["command"] != "harness_default":
        raise FactoryObserverRequestError("factory command must be harness_default")
    if worktree.get("policy") != "isolated_required":
        raise FactoryObserverRequestError(
            "factory worktree policy must be isolated_required"
        )
    owner, name, branch = repo.get("owner"), repo.get("name"), repo.get("branch")
    cwd = worktree.get("cwd")
    if not all(
        isinstance(value, str) and value.strip() for value in (owner, name, branch, cwd)
    ):
        raise FactoryObserverRequestError(
            "factory repo and worktree values must be non-empty strings"
        )
    harness = body.get("harness")
    model = body.get("model")
    thinking_effort = body.get("thinking_effort")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (harness, model, thinking_effort)
    ):
        raise FactoryObserverRequestError(
            "factory harness, model, and thinking_effort are required"
        )

    launch = FactoryObserverLaunch(
        run_id=run_id,
        expected_revision=revision,
        idempotency_key=key,
        target_hostname=target,
        repo_owner=owner.strip(),
        repo_name=name.strip(),
        branch=branch.strip(),
        cwd=cwd.strip(),
    )
    return launch, {
        "mode": "structured",
        "harness": harness.strip(),
        "model": model.strip(),
        "thinking_effort": thinking_effort.strip(),
        "repo_owner": launch.repo_owner,
        "repo_name": launch.repo_name,
        "branch": launch.branch,
        "cwd": launch.cwd,
        "source_session_id": launch.source_session_id,
        "handoff_mode": _MODE,
        "client_session_id": launch.client_session_id,
        "_factory_observer_launch": launch,
    }


def factory_observer_projection(session: Any) -> dict[str, Any] | None:
    """Derive a bounded, observer-only projection from an existing session."""
    if getattr(session, "handoff_mode", None) != _MODE:
        return None
    source = getattr(session, "source_session_id", None) or ""
    if not source.startswith("factory/") or "@" not in source:
        return None
    run_id, _, raw_revision = source.removeprefix("factory/").rpartition("@")
    try:
        revision = int(raw_revision)
    except ValueError:
        return None
    if not _RUN_ID.fullmatch(run_id) or revision < 0:
        return None
    return {
        "run_id": run_id,
        "expected_revision": revision,
        "target_hostname": getattr(session, "host_id", ""),
        "status": getattr(session, "status", "unknown"),
        "authority": "taskflow",
    }
