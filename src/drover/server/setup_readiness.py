"""Read-only first-computer readiness checks for the local server CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import quote

from drover.config import DroverConfig

_LOCAL_LIVENESS_ACTION = (
    "Start drover-server and confirm its configured listener is reachable."
)
_ADVERTISED_LIVENESS_ACTION = (
    "Configure a reachable private advertised address, then rerun setup-check."
)
_CONTROL_API_ACTION = (
    "Ensure the local Drover API is available, then rerun setup-check."
)
_HOST_ACTION = "Start the selected host daemon and wait for it to appear online."
_HARNESS_ACTION = "Sign in to the selected harness, then rerun setup-check."
_HARNESS_UNSUPPORTED_ACTION = (
    "Enable a supported harness on the selected host, then rerun setup-check."
)
_HARNESS_DISABLED_ACTION = (
    "Enable the selected harness on the selected host, then rerun setup-check."
)
_PROJECT_ACTION = "Choose an existing project directory, then rerun setup-check."
_PROJECT_DEPENDENCY_ACTION = (
    "Complete the selected host and harness checks, then rerun setup-check."
)
_NO_ACTION = "No action needed."


@dataclass(frozen=True)
class SetupTarget:
    host_id: str
    harness: str
    project: str


@dataclass(frozen=True)
class SetupCheck:
    key: str
    state: str
    action: str


@dataclass(frozen=True)
class SetupReadinessReport:
    checks: tuple[SetupCheck, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.state == "pass" for check in self.checks)

    def check(self, key: str) -> SetupCheck:
        return next(check for check in self.checks if check.key == key)

    def as_dict(self) -> dict:
        return {"ready": self.ready, "checks": [asdict(check) for check in self.checks]}


def unavailable_setup_report() -> SetupReadinessReport:
    """Return fixed recovery guidance when the command cannot load configuration."""
    return SetupReadinessReport(
        checks=(
            _check("local_liveness", False, _LOCAL_LIVENESS_ACTION),
            _check("advertised_liveness", False, _ADVERTISED_LIVENESS_ACTION),
            _check("control_api", False, _CONTROL_API_ACTION),
            _check("host", False, _HOST_ACTION),
            _check("harness_auth", False, _CONTROL_API_ACTION),
            _check("project", False, _PROJECT_DEPENDENCY_ACTION),
        )
    )


def _check(key: str, passed: bool, action: str) -> SetupCheck:
    return SetupCheck(
        key=key,
        state="pass" if passed else "fail",
        action=_NO_ACTION if passed else action,
    )


def _local_listener_url(cfg: DroverConfig) -> str:
    host = (cfg.server_metrics_host or "").strip()
    if not host or host in {"0.0.0.0", "::", "*"}:
        host = "127.0.0.1"
    return f"http://{host}:{cfg.metrics_http_port}/healthz"


def _advertised_listener_url(cfg: DroverConfig) -> str:
    address = cfg.server_advertised_url.strip()
    for scheme in ("http://", "https://"):
        if address.startswith(scheme):
            address = address[len(scheme) :]
    address = address.rstrip("/") or f"127.0.0.1:{cfg.metrics_http_port}"
    return f"http://{address}/healthz"


def _enabled_harness(host: dict, harness: str) -> bool:
    capabilities = host.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    advertised = capabilities.get("harnesses")
    return isinstance(advertised, list) and any(
        isinstance(item, dict)
        and item.get("name") == harness
        and item.get("enabled") is True
        for item in advertised
    )


def _failed_dependents(checks: list[SetupCheck], action: str) -> SetupReadinessReport:
    checks.extend(
        (
            _check("harness_auth", False, action),
            _check("project", False, _PROJECT_DEPENDENCY_ACTION),
        )
    )
    return SetupReadinessReport(checks=tuple(checks))


def evaluate_setup(
    cfg: DroverConfig,
    target: SetupTarget,
    *,
    liveness: Callable[[str], bool],
    request_json: Callable[[str, str, dict | None], dict],
) -> SetupReadinessReport:
    """Evaluate bounded existing reads without surfacing transport details."""
    checks = [
        _check(
            "local_liveness",
            _safe_liveness(liveness, _local_listener_url(cfg)),
            _LOCAL_LIVENESS_ACTION,
        ),
        _check(
            "advertised_liveness",
            _safe_liveness(liveness, _advertised_listener_url(cfg)),
            _ADVERTISED_LIVENESS_ACTION,
        ),
    ]

    try:
        hosts_response = request_json("GET", "/harness/hosts", None)
    except Exception:  # noqa: BLE001 - recovery output is deliberately fixed
        checks.append(_check("control_api", False, _CONTROL_API_ACTION))
        checks.append(_check("host", False, _HOST_ACTION))
        return _failed_dependents(checks, _CONTROL_API_ACTION)

    hosts = hosts_response.get("hosts") if isinstance(hosts_response, dict) else None
    if not isinstance(hosts, list):
        checks.append(_check("control_api", False, _CONTROL_API_ACTION))
        checks.append(_check("host", False, _HOST_ACTION))
        return _failed_dependents(checks, _CONTROL_API_ACTION)

    checks.append(_check("control_api", True, _CONTROL_API_ACTION))
    host = next(
        (
            item
            for item in hosts
            if isinstance(item, dict) and item.get("host_id") == target.host_id
        ),
        None,
    )
    if host is None or host.get("status") != "online":
        checks.append(_check("host", False, _HOST_ACTION))
        return _failed_dependents(checks, _HOST_ACTION)

    checks.append(_check("host", True, _HOST_ACTION))
    if not _enabled_harness(host, target.harness):
        return _failed_dependents(checks, _HARNESS_DISABLED_ACTION)

    host_id = quote(target.host_id, safe="")
    harness = quote(target.harness, safe="")
    try:
        auth_response = request_json(
            "GET", f"/harness/hosts/{host_id}/auth/{harness}/status", None
        )
    except Exception:  # noqa: BLE001 - recovery output is deliberately fixed
        checks.append(_check("harness_auth", False, _HARNESS_ACTION))
        checks.append(_check("project", False, _PROJECT_DEPENDENCY_ACTION))
        return SetupReadinessReport(checks=tuple(checks))

    auth_state = auth_response.get("state") if isinstance(auth_response, dict) else None
    if auth_state != "authenticated":
        action = (
            _HARNESS_UNSUPPORTED_ACTION
            if auth_state == "unsupported"
            else _HARNESS_ACTION
        )
        checks.append(_check("harness_auth", False, action))
        checks.append(_check("project", False, _PROJECT_DEPENDENCY_ACTION))
        return SetupReadinessReport(checks=tuple(checks))

    checks.append(_check("harness_auth", True, _HARNESS_ACTION))
    try:
        project_response = request_json(
            "POST",
            f"/harness/hosts/{host_id}/fs/exists",
            {"paths": [target.project]},
        )
    except Exception:  # noqa: BLE001 - recovery output is deliberately fixed
        checks.append(_check("project", False, _PROJECT_ACTION))
        return SetupReadinessReport(checks=tuple(checks))

    exists = (
        project_response.get("exists") if isinstance(project_response, dict) else None
    )
    project_exists = isinstance(exists, dict) and exists.get(target.project) is True
    checks.append(_check("project", project_exists, _PROJECT_ACTION))
    return SetupReadinessReport(checks=tuple(checks))


def _safe_liveness(liveness: Callable[[str], bool], url: str) -> bool:
    try:
        return liveness(url) is True
    except Exception:  # noqa: BLE001 - recovery output is deliberately fixed
        return False
