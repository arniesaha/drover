"""Anthropic API wrapper for session summarization.

Returns a dict with the keys the prompt template requests. Test code
injects ``_client`` to avoid hitting the live API.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger("drover.summarizer.client")


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 2000

_REQUIRED_KEYS = ("summary_md", "next_steps_md", "open_questions")
_OPTIONAL_KEYS = ("last_user_prompt", "last_assistant")
# Brief-prompt schema, used when call_claude_summary is invoked from the
# brief worker. Kept here so the validation list lives next to the rest of
# the JSON-shape contract.
BRIEF_REQUIRED_KEYS = ("brief_md", "recent_themes_md", "next_steps_md")
BRIEF_OPTIONAL_KEYS = ("key_files", "open_questions")
# Active-session brief schema (rolling handoff brief for OPEN sessions). The
# active-brief prompt asks for the same brief_md + a small set of fields the
# next agent needs to resume mid-task without waiting for SessionEnd.
ACTIVE_BRIEF_REQUIRED_KEYS = (
    "brief_md",
    "last_user_req",
    "current_objective",
    "suggested_next",
)
ACTIVE_BRIEF_OPTIONAL_KEYS = ("files_touched", "open_blockers")
LIVE_RECAP_REQUIRED_KEYS = ("recap",)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_ANY_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OPEN_FENCE_RE = re.compile(r"^```(?:json)?\s*", re.DOTALL)
_CLOSE_FENCE_RE = re.compile(r"\s*```\s*$", re.DOTALL)
_STRING_FIELDS = {
    "summary_md",
    "next_steps_md",
    "last_user_prompt",
    "last_assistant",
    "brief_md",
    "recent_themes_md",
    "last_user_req",
    "current_objective",
    "suggested_next",
    "open_blockers",
    "recap",
}
_STRING_LIST_FIELDS = {"open_questions", "key_files", "files_touched"}


class NoApiKeyError(RuntimeError):
    """No ANTHROPIC_API_KEY configured."""


class SummarizerClientError(RuntimeError):
    """LLM returned an unusable response."""


def _strip_fence(s: str) -> str:
    """Strip a markdown code fence, tolerating a missing closing fence.

    Truncated responses (hitting max_tokens mid-output) often lose the
    closing ``` — we still want to extract whatever JSON is there.
    """
    s = s.strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1)
    # Tolerant fallback: strip the opening fence + any trailing fence,
    # leaving whatever's in between.
    inner = _OPEN_FENCE_RE.sub("", s, count=1)
    inner = _CLOSE_FENCE_RE.sub("", inner)
    return inner


def call_claude_summary(
    prompt: str,
    *,
    api_key: Optional[str] = None,
    auth_token: Optional[str] = None,
    base_url: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    required_keys: tuple = _REQUIRED_KEYS,
    optional_keys: tuple = _OPTIONAL_KEYS,
    _client: Any = None,
) -> dict:
    """Call Anthropic's Messages API and parse the JSON response.

    Auth: pass either ``api_key`` (x-api-key) OR ``auth_token`` (Bearer,
    used for Claude.ai Pro/Max OAuth). When ``auth_token`` is supplied,
    we also set the ``anthropic-beta: oauth-2025-04-20`` header.
    ``base_url`` lets you route through a proxy like AgentWeave.
    """
    if not api_key and not auth_token:
        raise NoApiKeyError(
            "neither ANTHROPIC_API_KEY nor ANTHROPIC_OAUTH_TOKEN configured"
        )

    if _client is None:
        import anthropic

        client_kwargs: dict = {}
        if auth_token:
            client_kwargs["auth_token"] = auth_token
            client_kwargs["default_headers"] = {"anthropic-beta": "oauth-2025-04-20"}
        else:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        _client = anthropic.Anthropic(**client_kwargs)

    resp = _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    # Concatenate text blocks
    text_parts = []
    for block in resp.content:
        # SDK returns objects with .text; tests use the same shape
        text = getattr(block, "text", None)
        if text:
            text_parts.append(text)
    return parse_summary_response(
        "".join(text_parts),
        required_keys=required_keys,
        optional_keys=optional_keys,
    )


def parse_summary_response(
    text: str,
    *,
    required_keys: tuple = _REQUIRED_KEYS,
    optional_keys: tuple = _OPTIONAL_KEYS,
) -> dict:
    """Turn one model's raw text into the validated summary dict.

    Shared by every backend that gets text rather than parsed JSON back, so
    fence stripping, truncation repair and schema validation stay identical
    whichever path produced the answer.
    """
    raw = _strip_fence(text)
    parsed = _parse_json_response(raw, required_keys=required_keys)

    if not isinstance(parsed, dict):
        raise SummarizerClientError(f"LLM returned non-object: {type(parsed).__name__}")

    parsed = _normalize_summary_fields(parsed, required_keys)
    _validate_summary_fields(parsed, required_keys)

    missing = [k for k in required_keys if k not in parsed]
    if missing:
        raise SummarizerClientError(f"LLM response missing required keys: {missing}")

    out: dict = {k: parsed[k] for k in required_keys}
    for k in optional_keys:
        out[k] = parsed.get(k)
    return out


def _parse_json_response(raw: str, *, required_keys: tuple = _REQUIRED_KEYS) -> Any:
    """Parse an LLM response into JSON, salvaging common wrapper/truncation forms.

    Exact JSON responses are returned directly so the caller can reject arrays or
    scalars with a precise error. For prose-wrapped responses, we only accept an
    embedded/fenced object after it matches the expected schema. This avoids
    accidentally accepting unrelated or transcript-quoted JSON from the response.
    """
    s = raw.strip()
    if s:
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            repaired = _try_repair_truncated_json(s)
            if repaired is not None and _is_acceptable_json_candidate(
                repaired, required_keys
            ):
                return repaired

    candidates: list[str] = []
    # Fenced JSON can appear after leading prose or before trailing prose.
    candidates.extend(match.group(1) for match in _ANY_FENCE_RE.finditer(raw))
    candidates.extend(_extract_json_object_candidates(raw))

    acceptable: list[dict] = []
    for candidate in candidates:
        parsed = _parse_candidate(candidate)
        if parsed is None:
            continue
        if _is_acceptable_json_candidate(parsed, required_keys):
            acceptable.append(_normalize_summary_fields(parsed, required_keys))

    unique = _dedupe_json_objects(acceptable)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise SummarizerClientError("LLM response contains ambiguous JSON objects")

    raise SummarizerClientError(f"LLM response is not JSON; raw={raw[:200]!r}")


def _parse_candidate(candidate: str) -> Any | None:
    s = candidate.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return _try_repair_truncated_json(s)


def _extract_json_object_candidates(raw: str) -> list[str]:
    """Return top-level-looking JSON objects embedded in text.

    If the final object never closes (a truncation case), include the tail
    beginning at that ``{`` so the repair path can decide whether it is usable.
    """
    candidates: list[str] = []
    i = 0
    while i < len(raw):
        start = raw.find("{", i)
        if start < 0:
            break

        in_string = False
        escape = False
        depth = 0
        found_end = False
        for pos, ch in enumerate(raw[start:], start=start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start : pos + 1])
                    i = pos + 1
                    found_end = True
                    break
        if not found_end:
            candidates.append(raw[start:])
            break
    return candidates


def _is_acceptable_json_candidate(parsed: Any, required_keys: tuple) -> bool:
    if not isinstance(parsed, dict):
        return False
    normalized = _normalize_summary_fields(parsed, required_keys)
    if any(k not in normalized for k in required_keys):
        return False
    try:
        _validate_summary_fields(normalized, required_keys)
    except SummarizerClientError:
        return False
    return True


def _dedupe_json_objects(objects: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for obj in objects:
        key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(obj)
    return unique


def _normalize_summary_fields(parsed: dict, required_keys: tuple) -> dict:
    """Safely default malformed fields for an otherwise recognizable summary.

    We only default the default session-summary schema. At least one expected
    summary field must be present; otherwise an unrelated object like ``{}`` or
    ``{"unexpected": ...}`` would be converted into an empty completed summary.
    The session summarizer is intentionally tolerant because these are
    best-effort handoff summaries: a malformed ``next_steps_md`` should not
    leave a non-auth runtime job permanently errored if the response is clearly
    trying to be a session summary.
    """
    if tuple(required_keys) != _REQUIRED_KEYS:
        return parsed
    if not any(k in parsed for k in _REQUIRED_KEYS):
        return parsed

    out = dict(parsed)
    for field in ("summary_md", "next_steps_md"):
        if not isinstance(out.get(field), str):
            out[field] = ""

    questions = out.get("open_questions")
    if isinstance(questions, list):
        out["open_questions"] = [q for q in questions if isinstance(q, str)]
    else:
        out["open_questions"] = []

    for field in ("last_user_prompt", "last_assistant"):
        if field in out and out[field] is not None and not isinstance(out[field], str):
            out[field] = None
    return out


def _validate_summary_fields(parsed: dict, required_keys: tuple) -> None:
    """Validate known summarizer schema field types after normalization."""
    required = set(required_keys)
    for field in _STRING_FIELDS.intersection(parsed):
        value = parsed[field]
        if value is None and field not in required:
            continue
        if not isinstance(value, str):
            raise SummarizerClientError(f"LLM response {field} must be a string")

    for field in _STRING_LIST_FIELDS.intersection(parsed):
        value = parsed[field]
        if value is None and field not in required:
            continue
        if not isinstance(value, list) or not all(isinstance(q, str) for q in value):
            raise SummarizerClientError(
                f"LLM response {field} must be an array of strings"
            )


def _try_repair_truncated_json(raw: str) -> Optional[dict]:
    """Attempt to parse a truncated JSON object by closing the open string,
    open arrays, and open object. Returns the parsed dict or None if hopeless."""
    s = raw.strip()
    if not s.startswith("{"):
        return None
    # Walk the string and track bracket/quote state. When max_tokens fires,
    # we usually end mid-string-value.
    in_string = False
    escape = False
    stack: list[str] = []
    last_complete_idx = 0  # position of the last complete top-level key:value pair
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:  # back at top level
                last_complete_idx = i + 1
        elif ch == "," and len(stack) == 1:
            # Top-level pair just ended cleanly
            last_complete_idx = i

    # Build a tail that closes whatever's open and try repeatedly
    # collapsing back to the last clean comma if needed.
    candidates: list[str] = []
    # Strategy 1: close the partial string + remaining brackets
    suffix = '"' if in_string else ""
    for ch in reversed(stack):
        suffix += "}" if ch == "{" else "]"
    candidates.append(s + suffix)
    # Strategy 2: truncate to last complete pair, then close braces
    if last_complete_idx > 0:
        head = s[:last_complete_idx].rstrip().rstrip(",")
        candidates.append(head + "}")
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None
