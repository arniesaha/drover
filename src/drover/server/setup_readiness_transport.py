"""Killable, no-redirect HTTP reads used only by ``setup-check``."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Mapping

import httpx

_MAX_REAP_RESERVE_SECONDS = 0.1
_MAX_REQUEST_BODY_BYTES = 32 * 1024
_MAX_REQUEST_TEXT_CHARACTERS = 32 * 1024
_MAX_SERIALIZED_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_SERIALIZED_RESULT_BYTES = 2 * _MAX_RESPONSE_BYTES


def run_setup_check_http_request(
    url: str,
    method: str,
    data: bytes | None,
    headers: Mapping[str, str],
    *,
    timeout: float,
    max_response_bytes: int | None,
) -> tuple[int, bytes]:
    """Run one bounded request in a subprocess that can be killed after stalls."""
    if timeout <= 0:
        raise TimeoutError("setup-check request timed out")

    deadline = time.monotonic() + timeout
    request = _encode_request(url, method, data, headers, timeout, max_response_bytes)
    if _communicate_timeout_seconds(deadline, timeout) <= 0:
        raise TimeoutError("setup-check request timed out")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "drover.server.setup_readiness_transport"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = process.communicate(
            input=request,
            timeout=_communicate_timeout_seconds(deadline, timeout),
        )
    except subprocess.TimeoutExpired:
        _kill_and_reap(process)
        raise TimeoutError("setup-check request timed out") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("setup-check transport failed") from exc
    return _decode_result(stdout)


def _communicate_timeout_seconds(deadline: float, timeout: float) -> float:
    reserve = min(_MAX_REAP_RESERVE_SECONDS, timeout / 10)
    return max(0.0, deadline - time.monotonic() - reserve)


def _kill_and_reap(process: subprocess.Popen) -> None:
    """Ensure a stalled stdin writer, resolver, or socket read cannot survive."""
    if process.poll() is None:
        process.kill()
    process.communicate()


def _encode_request(
    url: str,
    method: str,
    data: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
    max_response_bytes: int | None,
) -> bytes:
    if data is not None and len(data) > _MAX_REQUEST_BODY_BYTES:
        raise ValueError("setup-check request exceeds the configured bound")
    if (
        not isinstance(url, str)
        or not isinstance(method, str)
        or len(url) > _MAX_REQUEST_TEXT_CHARACTERS
        or len(method) > _MAX_REQUEST_TEXT_CHARACTERS
    ):
        raise ValueError("setup-check request exceeds the configured bound")
    serialized_headers = dict(headers)
    if not _headers_within_bound(serialized_headers):
        raise ValueError("setup-check request exceeds the configured bound")
    if not _response_bound_is_valid(max_response_bytes):
        raise ValueError("setup-check request exceeds the configured bound")
    try:
        encoded = json.dumps(
            {
                "url": url,
                "method": method,
                "data": base64.b64encode(data).decode("ascii") if data else None,
                "headers": serialized_headers,
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("setup-check request exceeds the configured bound") from exc
    if len(encoded) > _MAX_SERIALIZED_REQUEST_BYTES:
        raise ValueError("setup-check request exceeds the configured bound")
    return encoded


def _decode_request(
    raw: bytes,
) -> tuple[str, str, bytes | None, dict[str, str], float, int | None]:
    if len(raw) > _MAX_SERIALIZED_REQUEST_BYTES:
        raise ValueError("setup-check request exceeds the configured bound")
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError
        url = request["url"]
        method = request["method"]
        headers = request["headers"]
        encoded_data = request["data"]
        timeout = float(request["timeout"])
        max_response_bytes = request["max_response_bytes"]
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("setup-check request exceeds the configured bound") from exc
    if (
        not isinstance(url, str)
        or not isinstance(method, str)
        or not isinstance(headers, dict)
        or not _headers_within_bound(headers)
        or not isinstance(encoded_data, (str, type(None)))
        or timeout <= 0
        or not _response_bound_is_valid(max_response_bytes)
    ):
        raise ValueError("setup-check request exceeds the configured bound")
    try:
        data = base64.b64decode(encoded_data, validate=True) if encoded_data else None
    except (TypeError, ValueError) as exc:
        raise ValueError("setup-check request exceeds the configured bound") from exc
    if data is not None and len(data) > _MAX_REQUEST_BODY_BYTES:
        raise ValueError("setup-check request exceeds the configured bound")
    return url, method, data, headers, timeout, max_response_bytes


def _headers_within_bound(headers: Mapping[object, object]) -> bool:
    total = 0
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            return False
        total += len(name) + len(value)
        if total > _MAX_REQUEST_TEXT_CHARACTERS:
            return False
    return True


def _response_bound_is_valid(max_response_bytes: object) -> bool:
    return max_response_bytes is None or (
        isinstance(max_response_bytes, int)
        and 0 <= max_response_bytes <= _MAX_RESPONSE_BYTES
    )


def _encode_result(result: tuple[object, ...]) -> bytes:
    if result[0] == "ok" and len(result) == 3:
        status, body = result[1:]
        if isinstance(status, int) and isinstance(body, bytes):
            payload = {
                "outcome": "ok",
                "status": status,
                "body": base64.b64encode(body).decode("ascii"),
            }
        else:
            payload = {"outcome": "error"}
    else:
        payload = {"outcome": result[0]}
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_SERIALIZED_RESULT_BYTES:
        return b'{"outcome":"response-bound"}'
    return encoded


def _decode_result(raw: bytes) -> tuple[int, bytes]:
    if len(raw) > _MAX_SERIALIZED_RESULT_BYTES:
        raise RuntimeError("setup-check transport failed")
    try:
        result = json.loads(raw)
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("setup-check transport failed") from exc
    if not isinstance(result, dict):
        raise RuntimeError("setup-check transport failed")
    if result.get("outcome") == "ok":
        status = result.get("status")
        encoded_body = result.get("body")
        if isinstance(status, int) and isinstance(encoded_body, str):
            try:
                body = base64.b64decode(encoded_body, validate=True)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("setup-check transport failed") from exc
            return status, body
    if result.get("outcome") == "timeout":
        raise TimeoutError("setup-check request timed out")
    if result.get("outcome") == "response-bound":
        raise ValueError("setup-check response exceeds the configured bound")
    raise RuntimeError("setup-check transport failed")


def _worker_main() -> None:
    """Receive private stdin and write one bounded typed result to stdout."""
    try:
        request = _decode_request(
            sys.stdin.buffer.read(_MAX_SERIALIZED_REQUEST_BYTES + 1)
        )
        with suppress_setup_check_transport_logs():
            status, body = asyncio.run(_request_async(*request))
        result = ("ok", status, body)
    except TimeoutError:
        result = ("timeout",)
    except ValueError:
        result = ("response-bound",)
    except Exception:  # noqa: BLE001 - parent keeps all transport detail private
        result = ("error",)
    sys.stdout.buffer.write(_encode_result(result))
    sys.stdout.buffer.flush()


async def _request_async(
    url: str,
    method: str,
    data: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
    max_response_bytes: int | None,
) -> tuple[int, bytes]:
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
        ) as client:
            async with client.stream(
                method, url, content=data, headers=headers
            ) as response:
                status = response.status_code
                if not 200 <= status < 300 or max_response_bytes is None:
                    return status, b""
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_response_bytes:
                        raise ValueError(
                            "setup-check response exceeds the configured bound"
                        )
                    body.extend(chunk)
                return status, bytes(body)


@contextmanager
def suppress_setup_check_transport_logs():
    """Suppress transport logger handlers only for a setup-check request."""
    logger_dict = logging.Logger.manager.loggerDict
    loggers = [
        logger
        for name, logger in logger_dict.items()
        if isinstance(logger, logging.Logger)
        and (
            name == "httpx"
            or name.startswith("httpx.")
            or name == "httpcore"
            or name.startswith("httpcore.")
        )
    ]
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger not in loggers:
            loggers.append(logger)
    previous = [
        (logger, tuple(logger.handlers), logger.propagate) for logger in loggers
    ]
    try:
        for logger, _, _ in previous:
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
        yield
    finally:
        for logger, handlers, propagate in previous:
            logger.handlers.clear()
            logger.handlers.extend(handlers)
            logger.propagate = propagate


if __name__ == "__main__":
    _worker_main()
