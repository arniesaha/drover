"""Explicit configuration registry for central serving-store backends.

The registry is intentionally keyed by path. A hub can opt into PostgreSQL
without making another host that happens to inherit its environment stop using
its local DuckDB control plane.
"""

from __future__ import annotations

import re
from pathlib import Path
from threading import Lock

from drover.config import ControlStoreConfig

_CONTROL_PLANE_SUFFIX = ".registry.duckdb"
_CONFIGS: dict[str, ControlStoreConfig] = {}
_POSTGRES_STORES: dict[str, object] = {}
_LOCK = Lock()


def _key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _aliases(path: str | Path) -> tuple[str, ...]:
    resolved = Path(path).expanduser().resolve()
    aliases = {_key(resolved)}
    if not resolved.name.endswith(_CONTROL_PLANE_SUFFIX):
        aliases.add(_key(resolved.with_name(resolved.stem + _CONTROL_PLANE_SUFFIX)))
    return tuple(aliases)


def _central_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.name.endswith(_CONTROL_PLANE_SUFFIX):
        resolved = resolved.with_name(resolved.stem + _CONTROL_PLANE_SUFFIX)
    return _key(resolved)


def bind_qmark_parameters(sql: str) -> str:
    """Adapt qmark parameters to psycopg without touching SQL syntax.

    Only a question mark in normal SQL text is a bind marker. Quotes, line and
    block comments, and PostgreSQL dollar-quoted literals remain byte-for-byte
    unchanged. Literal percent signs are doubled because psycopg uses `%s`
    placeholders when parameters are present.
    """

    out: list[str] = []
    index = 0
    state = "normal"
    dollar_delimiter = ""

    def append_literal(char: str) -> None:
        # psycopg's percent-style binding parses percent signs even in quoted
        # SQL literals. Doubling preserves the SQL text that PostgreSQL sees.
        out.append("%%" if char == "%" else char)

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "normal":
            if char == "'":
                state = "single"
                out.append(char)
            elif char == '"':
                state = "double"
                out.append(char)
            elif char == "-" and next_char == "-":
                state = "line_comment"
                out.append("--")
                index += 1
            elif char == "/" and next_char == "*":
                state = "block_comment"
                out.append("/*")
                index += 1
            elif char == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
                candidate = match.group(0) if match is not None else ""
                if candidate:
                    state = "dollar"
                    dollar_delimiter = candidate
                    out.append(candidate)
                    index += len(candidate) - 1
                else:
                    append_literal(char)
            elif char == "?":
                if next_char == "?":
                    # `??` is the explicit escape for PostgreSQL's JSON
                    # existence operator. A bare qmark is this adapter's bind
                    # marker, so callers never need heuristic SQL rewriting.
                    out.append("?")
                    index += 1
                elif next_char in {"|", "&"}:
                    out.append(char)
                else:
                    out.append("%s")
            else:
                append_literal(char)
        elif state == "single":
            append_literal(char)
            if char == "'" and next_char == "'":
                append_literal(next_char)
                index += 1
            elif char == "'":
                state = "normal"
        elif state == "double":
            append_literal(char)
            if char == '"' and next_char == '"':
                append_literal(next_char)
                index += 1
            elif char == '"':
                state = "normal"
        elif state == "line_comment":
            append_literal(char)
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            append_literal(char)
            if char == "*" and next_char == "/":
                append_literal(next_char)
                index += 1
                state = "normal"
        else:  # dollar-quoted string
            if sql.startswith(dollar_delimiter, index):
                out.append(dollar_delimiter)
                index += len(dollar_delimiter) - 1
                state = "normal"
            else:
                append_literal(char)
        index += 1
    return "".join(out)


def configure_control_store(path: str | Path, config: ControlStoreConfig) -> None:
    """Register one explicit central path for a backend configuration."""
    with _LOCK:
        for alias in _aliases(path):
            _CONFIGS[alias] = config


def control_store_config(path: str | Path) -> ControlStoreConfig | None:
    with _LOCK:
        return _CONFIGS.get(_key(path))


def is_postgres_control_store(path: str | Path) -> bool:
    config = control_store_config(path)
    return config is not None and config.backend == "postgres"


def close_control_store(path: str | Path) -> None:
    """Forget a path registration; later backends also release pool state here."""
    store = None
    with _LOCK:
        for alias in _aliases(path):
            _CONFIGS.pop(alias, None)
        store = _POSTGRES_STORES.pop(_central_key(path), None)
    if store is not None:
        store.close()  # type: ignore[attr-defined]


def postgres_control_store(path: str | Path):
    """Return the pooled PostgreSQL store registered for ``path``.

    Imported lazily so the legacy local install never imports or requires
    psycopg. The registration check is deliberate: callers may not create a
    PostgreSQL store just because a DSN happens to be in their environment.
    """

    config = control_store_config(path)
    if config is None or config.backend != "postgres":
        raise ValueError(f"no PostgreSQL control store is registered for {path}")
    key = _central_key(path)
    with _LOCK:
        store = _POSTGRES_STORES.get(key)
        if store is None:
            from drover.server.postgres_control_store import PostgresControlStore

            store = PostgresControlStore(config)
            _POSTGRES_STORES[key] = store
        return store


def close_all_postgres_control_stores() -> None:
    """Release pooled PostgreSQL resources during server shutdown."""
    with _LOCK:
        stores = tuple(_POSTGRES_STORES.values())
        _POSTGRES_STORES.clear()
    for store in stores:
        store.close()  # type: ignore[attr-defined]
