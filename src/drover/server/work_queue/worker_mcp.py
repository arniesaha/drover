"""Bound stdio MCP surface for one Work-coordinator structured session.

This process has no listener.  Codex starts it as a stdio child and it proxies
only the one signed work item through the already-authenticated loopback hub
control API.  The paired host bearer stays in the child environment and is
never encoded in Codex configuration or returned by a tool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from drover.server.work_queue.client import control_call


class BoundWorkError(ValueError):
    """The worker attempted to use a missing or cross-item work binding."""


@dataclass(frozen=True)
class BoundWorkControl:
    """One immutable hub-issued worker item and its paired host transport."""

    control_url: str
    bearer: str
    run_key: str
    goal_id: str
    number: int

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None
    ) -> "BoundWorkControl":
        values = os.environ if env is None else env
        control_url = values.get("DROVER_WORK_CONTROL_URL")
        bearer = values.get("DROVER_MCP_TOKEN")
        run_key = values.get("DROVER_WORK_RUN_KEY")
        goal_id = values.get("DROVER_WORK_GOAL_ID")
        number_text = values.get("DROVER_WORK_ITEM_NUMBER")
        if not all(
            isinstance(value, str) and value
            for value in (control_url, bearer, run_key, goal_id, number_text)
        ):
            raise BoundWorkError("bound work control is unavailable")
        try:
            parsed = urlsplit(control_url)
        except ValueError as exc:
            raise BoundWorkError("bound work control is invalid") from exc
        try:
            port = parsed.port
        except ValueError as exc:
            raise BoundWorkError("bound work control is invalid") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or port is None
            or not 1 <= port <= 65535
        ):
            raise BoundWorkError("bound work control is invalid")
        try:
            number = int(number_text)
        except ValueError as exc:
            raise BoundWorkError("bound work item is invalid") from exc
        if number <= 0:
            raise BoundWorkError("bound work item is invalid")
        return cls(control_url.rstrip("/"), bearer, run_key, goal_id, number)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.bearer}

    def _call(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        return control_call(self.control_url, self._headers, operation, args)

    def get(self) -> dict[str, Any]:
        return self._call("get", {"goal_id": self.goal_id, "number": self.number})

    def update(
        self,
        action: str,
        *,
        evidence_key: str | None = None,
        estimated_cost: float | None = None,
    ) -> dict[str, Any]:
        if action not in {"complete", "failed", "progress"}:
            raise BoundWorkError("bound worker action is invalid")
        return self._call(
            "update",
            {
                "goal_id": self.goal_id,
                "number": self.number,
                "run_key": self.run_key,
                "action": action,
                "evidence_key": evidence_key,
                "estimated_cost": estimated_cost,
            },
        )


def build_server(control: BoundWorkControl) -> FastMCP:
    """Expose only the bound read and worker-update operations over stdio."""
    mcp = FastMCP("drover-bound-work")

    @mcp.tool()
    def drover_work_get() -> dict[str, Any]:
        """Read the single signed work item assigned to this worker."""
        return control.get()

    @mcp.tool()
    def drover_work_update(
        action: str,
        evidence_key: str | None = None,
        estimated_cost: float | None = None,
    ) -> dict[str, Any]:
        """Record one allowed update for the single signed worker item."""
        return control.update(
            action, evidence_key=evidence_key, estimated_cost=estimated_cost
        )

    return mcp


def main() -> None:
    build_server(BoundWorkControl.from_environment()).run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - executed as Codex MCP child
    main()
