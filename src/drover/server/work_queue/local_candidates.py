"""Explicit local-file candidate ingress for an isolated work-queue canary.

GitHub remains the default work source.  This adapter is selected only by an
explicit ``local_file`` goal configuration and binds the manifest to the exact
workspace and goal before projecting or mutating a candidate.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from .github import ProviderUnavailable
from .provider import ItemRef


class LocalCandidateProvider:
    """Durable local candidate manifest with no credential or network fallback."""

    _lock = threading.RLock()

    def __init__(self, workspace_id, scope):
        self.workspace_id = workspace_id
        self.scope = scope
        self.path = Path(scope.local_candidates_path or "")
        self.capabilities = frozenset(scope.credential.capabilities)
        self.actor_principal = (
            "local-candidates:"
            + hashlib.sha256(str(self.path).encode()).hexdigest()[:16]
        )

    def authenticate(self):
        self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.is_file() or self.path.is_symlink():
                raise ValueError
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "workspace_id", "goal_id", "items"}
                or data["version"] != 1
                or data["workspace_id"] != self.workspace_id
                or data["goal_id"] != self.scope.goal_id
                or not isinstance(data["items"], list)
            ):
                raise ValueError
            numbers = set()
            for item in data["items"]:
                self._item(item)
                if item["number"] in numbers:
                    raise ValueError
                numbers.add(item["number"])
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ProviderUnavailable("local_candidate_manifest_invalid") from None

    @staticmethod
    def _item(item: Any) -> None:
        required = {
            "number",
            "state",
            "body",
            "created_at",
            "labels",
        }
        optional = {"expected_effect_artifact"}
        if not isinstance(item, dict) or not (
            required <= set(item) <= required | optional
        ):
            raise ValueError
        if (
            isinstance(item["number"], bool)
            or not isinstance(item["number"], int)
            or item["number"] <= 0
            or item["state"] not in {"open", "closed"}
            or not isinstance(item["body"], str)
            or len(item["body"]) > 16_384
            or not isinstance(item["created_at"], str)
            or not isinstance(item["labels"], list)
            or not all(
                isinstance(label, str) and label and len(label) <= 191
                for label in item["labels"]
            )
            or (
                "expected_effect_artifact" in item
                and (
                    not isinstance(item["expected_effect_artifact"], str)
                    or not item["expected_effect_artifact"].strip()
                    or len(item["expected_effect_artifact"]) > 2000
                )
            )
        ):
            raise ValueError

    def _ref(self, ref: ItemRef) -> None:
        if (
            ref.workspace_id != self.workspace_id
            or ref.provider != "local_file"
            or ref.repo != self.scope.repo
            or ref.url != f"local://{self.scope.goal_id}/{ref.number}"
            or isinstance(ref.number, bool)
            or not isinstance(ref.number, int)
            or ref.number <= 0
        ):
            raise PermissionError("cross-workspace or local candidate ref")

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        public = {
            "number": item["number"],
            "state": item["state"],
            "body": item["body"],
            "created_at": item["created_at"],
            "labels": [{"name": label} for label in item["labels"]],
            "operator_authority_labels": list(item["labels"]),
        }
        if "expected_effect_artifact" in item:
            public["expected_effect_artifact"] = item["expected_effect_artifact"]
        return public

    def list_items(self) -> list[dict[str, Any]]:
        return [self._public(item) for item in self._load()["items"]]

    def get_item(self, ref: ItemRef) -> dict[str, Any]:
        self._ref(ref)
        for item in self._load()["items"]:
            if item["number"] == ref.number:
                return self._public(item)
        raise ProviderUnavailable("local_candidate_not_found")

    def _replace(self, data: dict[str, Any]) -> None:
        encoded = (
            json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProviderUnavailable("local_candidate_manifest_write_failed") from None

    def _change(self, ref: ItemRef, change) -> None:
        self._ref(ref)
        with self._lock:
            data = self._load()
            for item in data["items"]:
                if item["number"] == ref.number:
                    change(item)
                    self._item(item)
                    self._replace(data)
                    return
        raise ProviderUnavailable("local_candidate_not_found")

    def set_labels(self, ref: ItemRef, labels: list[str]) -> None:
        if not isinstance(labels, list) or not all(
            isinstance(label, str) and label and len(label) <= 191 for label in labels
        ):
            raise ValueError("invalid local candidate labels")
        self._change(ref, lambda item: item.__setitem__("labels", list(labels)))

    def close(self, ref: ItemRef) -> None:
        self._change(ref, lambda item: item.__setitem__("state", "closed"))

    def reopen(self, ref: ItemRef) -> None:
        self._change(ref, lambda item: item.__setitem__("state", "open"))

    def comment(self, ref: ItemRef, public_text: str) -> None:
        self._ref(ref)
        raise ProviderUnavailable("local_candidate_comments_unsupported")

    def list_attention(self) -> list[dict[str, Any]]:
        return []

    def inspect_action(self, ref: ItemRef, action: str) -> dict[str, Any]:
        self._ref(ref)
        return {"supported": False}

    def execute_action(self, ref: ItemRef, action: str) -> None:
        self._ref(ref)
        raise ProviderUnavailable("local_candidate_actions_unsupported")
