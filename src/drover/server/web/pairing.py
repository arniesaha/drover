"""Short-lived, single-use pairing codes.

Codes live in hub memory only: no file, no database row. A hub restart
invalidates every outstanding code, which is the behaviour we want and costs
nothing to implement. Roughly 40 bits of entropy is ample given the ten-minute
lifetime and the per-source failure throttle below.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# Crockford base32: no I, L, O or U, so a code read aloud or off a screen
# cannot be mistyped into a different valid code.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
DEVICE_TTL_SECONDS = 600.0
HOST_TTL_SECONDS = 900.0
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60.0

# Crockford's documented input aliases. U has no alias: it is simply not in
# the alphabet, so a code containing one cannot match.
_ALIASES = {"I": "1", "L": "1", "O": "0"}


class PairingError(Exception):
    """Base class for pairing failures."""


class UnknownCode(PairingError):
    """The code is unknown, already burned, or expired."""


class ThrottledSource(PairingError):
    """Too many failed attempts from one source."""


def normalize_code(raw: str) -> str:
    characters = []
    for character in raw.strip().upper():
        if character in {"-", " "}:
            continue
        characters.append(_ALIASES.get(character, character))
    return "".join(characters)


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


@dataclass(frozen=True)
class PairingCode:
    code: str
    scope: str
    label: str
    host_id: str | None
    expires_at: float

    @property
    def formatted(self) -> str:
        return format_code(self.code)


class PairingCodes:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._codes: dict[str, PairingCode] = {}
        self._failures: dict[str, list[float]] = {}

    def mint(
        self, *, scope: str, label: str, host_id: str | None = None
    ) -> PairingCode:
        ttl = HOST_TTL_SECONDS if scope == "host" else DEVICE_TTL_SECONDS
        with self._lock:
            self._purge()
            code = generate_code()
            while code in self._codes:
                code = generate_code()
            entry = PairingCode(
                code=code,
                scope=scope,
                label=label,
                host_id=host_id,
                expires_at=self._clock() + ttl,
            )
            self._codes[code] = entry
            return entry

    def redeem(self, raw: str, *, source: str) -> PairingCode:
        with self._lock:
            entry = self._lookup(raw, source=source)
            del self._codes[entry.code]
            return entry

    def peek(self, raw: str, *, source: str) -> PairingCode:
        """Validate without burning. Used by the join-mode reachability probe."""
        with self._lock:
            return self._lookup(raw, source=source)

    def _lookup(self, raw: str, *, source: str) -> PairingCode:
        """Caller holds the lock."""
        self._purge()
        if self._throttled(source):
            raise ThrottledSource("too many failed pairing attempts")
        entry = self._codes.get(normalize_code(raw))
        if entry is None:
            self._record_failure(source)
            raise UnknownCode("unknown or expired pairing code")
        return entry

    def _purge(self) -> None:
        now = self._clock()
        for code, entry in list(self._codes.items()):
            if entry.expires_at <= now:
                del self._codes[code]

    def _throttled(self, source: str) -> bool:
        now = self._clock()
        recent = [
            stamp
            for stamp in self._failures.get(source, [])
            if now - stamp < FAILURE_WINDOW_SECONDS
        ]
        self._failures[source] = recent
        return len(recent) >= MAX_FAILURES

    def _record_failure(self, source: str) -> None:
        self._failures.setdefault(source, []).append(self._clock())
