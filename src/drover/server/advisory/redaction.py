"""Secret redaction for ephemeral advisory content.

The functions in this module never retain the original content. Callers must
redact before deriving content identities or constructing backend requests.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_SOURCE = (
    r"(?:authorization|cookie|credential|pass(?:word|wd)|private[_-]?key|"
    r"client[_-]?secret|access[_-]?key|secret|token|api[_-]?key)"
)
_SENSITIVE_KEY = re.compile(_SENSITIVE_KEY_SOURCE, re.IGNORECASE)
_JSON_SCALAR = re.compile(
    r'(?P<prefix>"[^"\\]*(?:\\.[^"\\]*)*"\s*:\s*)'
    r'(?P<value>"(?:\\.|[^"\\])*"|-?(?:\d+(?:\.\d+)?)|true|false|null)',
    re.IGNORECASE,
)
_TOML_ASSIGNMENT = re.compile(
    rf'(?P<prefix>(?:"[^"\r\n]*{_SENSITIVE_KEY_SOURCE}[^"\r\n]*"|'
    rf"'[^'\r\n]*{_SENSITIVE_KEY_SOURCE}[^'\r\n]*'|"
    rf"[A-Za-z0-9_.-]*{_SENSITIVE_KEY_SOURCE}[A-Za-z0-9_.-]*)\s*=\s*)"
    r'(?P<value>"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|'
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^,}\r\n#]+)',
    re.IGNORECASE | re.MULTILINE,
)
_AUTHORIZATION = re.compile(
    r"(?im)(\bauthorization\s*:\s*)(?:(?:basic|bearer|digest)\s+)?[^\s,;\r\n]+"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[^\s,;]+")
_PEM_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"(?:-----END [^-\r\n]*PRIVATE KEY-----|$)"
)
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:"
    r"sk-(?:ant-)?[a-z0-9_-]{8,}|"
    r"gh[pousr]_[a-z0-9]{8,}|"
    r"xox[baprs]-[a-z0-9-]{8,}|"
    r"AKIA[A-Z0-9]{16}"
    r")\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")


def redact_content(content: str) -> str:
    """Return content with credential-shaped values and tokens removed."""

    if not isinstance(content, str):
        raise TypeError("content must be a string")

    redacted = _redact_json_document(content)
    redacted = _PEM_PRIVATE_KEY.sub(REDACTED, redacted)
    redacted = _JSON_SCALAR.sub(_redact_json_scalar, redacted)
    redacted = _TOML_ASSIGNMENT.sub(_redact_toml_assignment, redacted)
    redacted = _AUTHORIZATION.sub(rf"\1{REDACTED}", redacted)
    redacted = _BEARER.sub(rf"\1 {REDACTED}", redacted)
    redacted = _KNOWN_TOKEN.sub(REDACTED, redacted)
    return _JWT.sub(REDACTED, redacted)


def _redact_json_document(content: str) -> str:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return content
    return json.dumps(_redact_json_value(value), ensure_ascii=False, sort_keys=True)


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if _SENSITIVE_KEY.search(str(key))
                else _redact_json_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _redact_json_scalar(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    key = prefix.split(":", 1)[0]
    if not _SENSITIVE_KEY.search(key):
        return match.group(0)
    return f'{prefix}"{REDACTED}"'


def _redact_toml_assignment(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    key = prefix.rsplit("=", 1)[0]
    if not _SENSITIVE_KEY.search(key):
        return match.group(0)
    return f'{prefix}"{REDACTED}"'
