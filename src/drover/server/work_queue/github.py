"""GitHub REST adapter with bounded conditional reads and credential refs."""

from __future__ import annotations

import hashlib
import os
import time
from collections import OrderedDict
from typing import Callable

import requests

from .config import GoalScope
from .provider import ItemRef

API = "https://api.github.com"


class ProviderUnavailable(RuntimeError):
    """Safe diagnostic without upstream bodies, credentials or private text."""


class GitHubProvider:
    def __init__(
        self,
        workspace_id: str,
        scope: GoalScope,
        *,
        session=None,
        clock=time.time,
        public_text_validator: Callable[[str], bool] | None = None,
    ):
        if not workspace_id.strip():
            raise ValueError("workspace required")
        self.workspace_id = workspace_id
        self.scope = scope
        self.capabilities = frozenset(scope.credential.capabilities)
        self.actor_principal = ""
        self.session = session or requests.Session()
        self.clock = clock
        self.public_text_validator = public_text_validator
        self._cache = OrderedDict()
        self._credential_digest = ""
        self.retry_at = 0.0

    def _require(self, capability):
        if capability not in self.capabilities:
            raise ProviderUnavailable("github_permission_unavailable:" + capability)

    def _request(self, method, path, *, params=None, body=None):
        token = os.environ.get(self.scope.credential.token_env, "").strip()
        if not token:
            raise ProviderUnavailable("github_credential_unavailable")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if digest != self._credential_digest:
            self._cache.clear()
            self.actor_principal = ""
            self._credential_digest = digest
        now = self.clock()
        if now < self.retry_at:
            raise ProviderUnavailable("github_rate_limit_backoff")
        key = (path, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached and now - cached[2] > 300:
            self._cache.pop(key)
            cached = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if method == "GET" and cached and cached[0]:
            headers["If-None-Match"] = cached[0]
        try:
            response = self.session.request(
                method,
                API + path,
                params=params,
                json=body,
                headers=headers,
                timeout=(5, 20),
                allow_redirects=False,
            )
            if response.status_code in (403, 429) and (
                response.status_code == 429
                or response.headers.get("Retry-After")
                or response.headers.get("X-RateLimit-Remaining") == "0"
            ):
                delay = float(response.headers.get("Retry-After", "60"))
                reset = float(response.headers.get("X-RateLimit-Reset", "0"))
                self.retry_at = max(now + delay, reset)
                raise ProviderUnavailable("github_rate_limit_backoff")
            if response.status_code == 304 and cached:
                self._cache[key] = (cached[0], cached[1], now)
                return cached[1]
            if not 200 <= response.status_code < 300:
                raise ProviderUnavailable(f"github_http_{response.status_code}")
            if len(response.content) > 2_000_000:
                raise ProviderUnavailable("github_response_too_large")
            data = response.json() if response.content else None
        except (requests.RequestException, ValueError) as exc:
            raise ProviderUnavailable("github_read_failed") from None
        if method == "GET":
            self._cache[key] = (response.headers.get("ETag"), data, now)
            self._cache.move_to_end(key)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        else:
            self._cache.clear()
        return data

    def _pages(self, path, params=None, collection=None):
        result = []
        for page in range(1, 101):
            data = self._request(
                "GET", path, params={**(params or {}), "per_page": 100, "page": page}
            )
            rows = data[collection] if collection else data
            if not isinstance(rows, list):
                raise ProviderUnavailable("github_invalid_collection")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ProviderUnavailable("github_pagination_limit")

    @property
    def base(self):
        return "/repos/" + self.scope.repo

    def authenticate(self):
        credential = self.scope.credential
        if credential.mode == "app":
            repos = self._pages("/installation/repositories", collection="repositories")
            if not any(
                r["id"] == credential.repository_id
                and r["full_name"] == self.scope.repo
                for r in repos
            ):
                raise ProviderUnavailable("github_app_repository_mismatch")
            self.actor_principal = f"github:installation:{credential.installation_id}"
        else:
            user = self._request("GET", "/user")
            self.actor_principal = f"github:user:{user['id']}"

    def _ref(self, ref):
        if (
            ref.workspace_id != self.workspace_id
            or ref.provider != "github"
            or ref.repo != self.scope.repo
        ):
            raise PermissionError("cross-workspace or repository ref")
        if not isinstance(ref.number, int) or ref.number <= 0:
            raise ValueError("invalid item number")
        return self.base + f"/issues/{ref.number}"

    def list_items(self):
        self._require("issues_read")
        params = {"state": "all"}
        if self.scope.goal_label:
            params["labels"] = self.scope.goal_label
        else:
            params["milestone"] = str(self.scope.milestone)
        return [
            r
            for r in self._pages(self.base + "/issues", params)
            if "pull_request" not in r
        ]

    def exact_head_check_state(self, sha, branch):
        """Return green/failed/pending for this branch's required checks at SHA."""
        self._require("checks_read")
        required = self._request(
            "GET", self.base + f"/branches/{branch}/protection/required_status_checks"
        )
        contexts = (
            set(required.get("contexts", [])) if isinstance(required, dict) else set()
        )
        contexts.update(
            check.get("context")
            for check in (
                required.get("checks", []) if isinstance(required, dict) else []
            )
            if isinstance(check, dict) and isinstance(check.get("context"), str)
        )
        if not contexts:
            return "pending"
        data = self._request(
            "GET", self.base + f"/commits/{sha}/check-runs", params={"filter": "latest"}
        )
        runs = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list) or not runs:
            return "pending"
        by_name = {run.get("name"): run for run in runs if isinstance(run, dict)}
        conclusions = []
        for context in contexts:
            run = by_name.get(context)
            if not isinstance(run, dict) or run.get("status") != "completed":
                return "pending"
            conclusions.append(run.get("conclusion"))
        # Be conservative if an incomplete non-required run is present: this
        # avoids announcing green while the suite itself still runs.
        for run in runs:
            if not isinstance(run, dict) or run.get("status") != "completed":
                return "pending"
        return "green" if all(value == "success" for value in conclusions) else "failed"

    def get_item(self, ref):
        self._require("issues_read")
        return self._request("GET", self._ref(ref))

    def list_labels(self):
        self._require("issues_read")
        return self._pages(self.base + "/labels")

    def list_milestones(self):
        self._require("issues_read")
        return self._pages(self.base + "/milestones", {"state": "all"})

    def list_comments(self, ref):
        self._require("issues_read")
        return self._pages(self._ref(ref) + "/comments")

    def get_pull(self, ref):
        self._require("pulls_read")
        self._ref(ref)
        return self._request("GET", self.base + f"/pulls/{ref.number}")

    def list_checks(self, sha):
        import re

        self._require("checks_read")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise ValueError("invalid commit SHA")
        return self._pages(
            self.base + f"/commits/{sha}/check-runs", collection="check_runs"
        )

    def list_commit_statuses(self, sha):
        self.list_checks(sha)
        return self._pages(self.base + f"/commits/{sha}/statuses")

    def list_attention(self):
        rows = []
        if "pulls_read" in self.capabilities:
            for pr in self._pages(self.base + "/pulls", {"state": "open"}):
                kind = "draft_pr" if pr.get("draft") else "pr_review"
                rows.append(
                    {
                        "source_key": f"pr:{pr['number']}",
                        "kind": kind,
                        "url": f"https://github.com/{self.scope.repo}/pull/{pr['number']}",
                        "updated_at": pr["updated_at"],
                    }
                )
                if "checks_read" in self.capabilities:
                    checks = self.list_checks(pr["head"]["sha"])
                    for check in checks:
                        if check.get("conclusion") in {
                            "failure",
                            "timed_out",
                            "cancelled",
                            "action_required",
                            "startup_failure",
                        }:
                            rows.append(
                                {
                                    "source_key": f"check:{check['id']}",
                                    "kind": "failed_check",
                                    "url": f"https://github.com/{self.scope.repo}/pull/{pr['number']}/checks",
                                    "updated_at": pr["updated_at"],
                                }
                            )
        if "actions_read" in self.capabilities:
            runs = self._pages(
                self.base + "/actions/runs", {"status": "waiting"}, "workflow_runs"
            )
            for run in runs:
                pending = self._request(
                    "GET", self.base + f"/actions/runs/{run['id']}/pending_deployments"
                )
                for gate in pending:
                    rows.append(
                        {
                            "source_key": f"environment:{run['id']}:{run.get('run_attempt', 1)}:{gate['environment']['id']}",
                            "kind": "environment_approval",
                            "url": f"https://github.com/{self.scope.repo}/actions/runs/{run['id']}",
                            "updated_at": run["updated_at"],
                            "actions": ["open"],
                        }
                    )
        return rows

    def _write(self, ref, method, suffix="", body=None):
        path = self._ref(ref)
        self._require("issues_write")
        self.authenticate()
        self._request(method, path + suffix, body=body)

    def set_labels(self, ref, labels):
        self._write(ref, "PUT", "/labels", {"labels": labels})

    def comment(self, ref, public_text):
        if not self.public_text_validator or not self.public_text_validator(
            public_text
        ):
            raise PermissionError("public comment requires explicit sanitization")
        self._write(ref, "POST", "/comments", {"body": public_text})

    def close(self, ref):
        self._write(ref, "PATCH", body={"state": "closed"})

    def reopen(self, ref):
        self._write(ref, "PATCH", body={"state": "open"})

    def inspect_action(self, ref, action):
        self._ref(ref)
        return {
            "workspace_id": self.workspace_id,
            "actor_principal": self.actor_principal,
            "action": "open",
            "supported": False,
            "url": f"https://github.com/{self.scope.repo}/issues/{ref.number}",
        }

    def execute_action(self, ref, action):
        self._ref(ref)
        raise PermissionError(
            "decision execution requires phase 2 authorization and audit"
        )
