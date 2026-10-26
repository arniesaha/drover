"""Thin MCP proxy to the control-owned work tools using caller credentials."""

import json
from urllib.parse import urlsplit

import requests


def control_call(url, headers, operation, args):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
    ):
        raise ValueError("work control requires loopback HTTP")
    authorization = headers.get("Authorization", "") or ""
    if not authorization.startswith("Bearer "):
        raise PermissionError("authenticated bearer required")
    read = operation in {"list", "get"}
    if operation not in {
        "list",
        "get",
        "launch",
        "link",
        "update",
        "question",
        "decide",
    }:
        raise ValueError("unknown work operation")
    with requests.request(
        "GET" if read else "POST",
        url.rstrip("/") + "/work/" + operation,
        headers={"Authorization": authorization},
        params=args if read else None,
        json=None if read else args,
        timeout=30,
        allow_redirects=False,
        stream=True,
    ) as response:
        data = bytearray()
        for chunk in response.iter_content(8192):
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("work control response exceeds bound")
        if response.status_code != 200:
            raise RuntimeError(_failure(response.status_code, data))
        return json.loads(data)


def _failure(status, body):
    """Status plus the hub's own fixed ``error``/``reason`` strings, nothing else."""
    detail = []
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("error", "reason"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                detail.append(value[:200])
    return f"work control HTTP {status}: " + ("; ".join(detail) or "unavailable")
