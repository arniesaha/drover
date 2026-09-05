"""Killable, no-redirect HTTP reads used only by ``setup-check``."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from contextlib import contextmanager
from typing import Mapping

import httpx

_PROCESS_NAME = "drover-setup-check-request"
_MAX_REAP_RESERVE_SECONDS = 0.1


def run_setup_check_http_request(
    url: str,
    method: str,
    data: bytes | None,
    headers: Mapping[str, str],
    *,
    timeout: float,
    max_response_bytes: int | None,
) -> tuple[int, bytes]:
    """Run one bounded request in a process that can be killed after DNS stalls."""
    if timeout <= 0:
        raise TimeoutError("setup-check request timed out")

    deadline = time.monotonic() + timeout
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    process = context.Process(
        name=_PROCESS_NAME,
        target=_setup_check_request_worker,
        args=(child_connection,),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        child_connection.close()
        if _response_wait_seconds(deadline, timeout) <= 0:
            raise TimeoutError("setup-check request timed out")
        parent_connection.send(
            (url, method, data, dict(headers), timeout, max_response_bytes)
        )
        if not parent_connection.poll(_response_wait_seconds(deadline, timeout)):
            raise TimeoutError("setup-check request timed out")
        try:
            result = parent_connection.recv()
        except EOFError as exc:
            raise RuntimeError("setup-check transport failed") from exc
        return _decode_result(result)
    except (TimeoutError, ValueError, RuntimeError):
        raise
    except (OSError, multiprocessing.ProcessError) as exc:
        raise RuntimeError("setup-check transport failed") from exc
    finally:
        parent_connection.close()
        if started:
            _kill_and_reap(process)
        else:
            child_connection.close()


def _response_wait_seconds(deadline: float, timeout: float) -> float:
    reserve = min(_MAX_REAP_RESERVE_SECONDS, timeout / 10)
    return max(0.0, deadline - time.monotonic() - reserve)


def _kill_and_reap(process: multiprocessing.Process) -> None:
    """Ensure no resolver thread or socket read survives the parent request."""
    if process.is_alive():
        process.kill()
    process.join()


def _decode_result(result: object) -> tuple[int, bytes]:
    if not isinstance(result, tuple) or not result:
        raise RuntimeError("setup-check transport failed")
    if result[0] == "ok" and len(result) == 3:
        status, body = result[1:]
        if isinstance(status, int) and isinstance(body, bytes):
            return status, body
    if result[0] == "timeout":
        raise TimeoutError("setup-check request timed out")
    if result[0] == "response-bound":
        raise ValueError("setup-check response exceeds the configured bound")
    raise RuntimeError("setup-check transport failed")


def _setup_check_request_worker(connection) -> None:
    """Receive private request data over a pipe and return only typed results."""
    try:
        request = connection.recv()
        with suppress_setup_check_transport_logs():
            status, body = asyncio.run(_request_async(*request))
        result = ("ok", status, body)
    except TimeoutError:
        result = ("timeout",)
    except ValueError:
        result = ("response-bound",)
    except Exception:  # noqa: BLE001 - parent keeps all transport detail private
        result = ("error",)
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


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
