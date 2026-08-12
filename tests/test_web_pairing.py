"""Tests for drover.server.web.pairing -- mint, redeem, expire, throttle."""

from __future__ import annotations

import pytest

from drover.server.web.pairing import (
    ALPHABET,
    CODE_LENGTH,
    DEVICE_TTL_SECONDS,
    HOST_TTL_SECONDS,
    MAX_FAILURES,
    PairingCodes,
    ThrottledSource,
    UnknownCode,
    format_code,
    normalize_code,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_minted_code_uses_the_unambiguous_alphabet():
    entry = PairingCodes().mint(scope="device", label="Phone")
    assert len(entry.code) == CODE_LENGTH
    assert set(entry.code) <= set(ALPHABET)
    for ambiguous in "ILOU":
        assert ambiguous not in entry.code


def test_formatted_code_is_grouped():
    assert format_code("K7QP2M4X") == "K7QP-2M4X"
    assert PairingCodes().mint(scope="device", label="Phone").formatted[4] == "-"


def test_normalize_accepts_lowercase_dashes_and_crockford_aliases():
    assert normalize_code("k7qp-2m4x") == "K7QP2M4X"
    assert normalize_code(" K7QP 2M4X ") == "K7QP2M4X"
    assert normalize_code("I7QP-2M4X") == "17QP2M4X"
    assert normalize_code("O7QP-2M4X") == "07QP2M4X"


def test_redeem_returns_the_minted_entry():
    codes = PairingCodes()
    entry = codes.mint(scope="device", label="Phone")
    assert codes.redeem(entry.formatted, source="1.2.3.4").label == "Phone"


def test_redeem_burns_the_code():
    codes = PairingCodes()
    entry = codes.mint(scope="device", label="Phone")
    codes.redeem(entry.code, source="1.2.3.4")
    with pytest.raises(UnknownCode):
        codes.redeem(entry.code, source="1.2.3.4")


def test_scope_travels_on_the_code_not_the_request():
    codes = PairingCodes()
    entry = codes.mint(scope="host", label="build-mac", host_id="build-mac")
    redeemed = codes.redeem(entry.code, source="1.2.3.4")
    assert redeemed.scope == "host"
    assert redeemed.host_id == "build-mac"


def test_device_code_expires_after_its_ttl():
    clock = _Clock()
    codes = PairingCodes(clock=clock)
    entry = codes.mint(scope="device", label="Phone")
    clock.advance(DEVICE_TTL_SECONDS + 1)
    with pytest.raises(UnknownCode):
        codes.redeem(entry.code, source="1.2.3.4")


def test_host_code_lives_longer_than_a_device_code():
    clock = _Clock()
    codes = PairingCodes(clock=clock)
    entry = codes.mint(scope="host", label="build-mac")
    clock.advance(DEVICE_TTL_SECONDS + 1)
    assert codes.redeem(entry.code, source="1.2.3.4").scope == "host"

    clock2 = _Clock()
    codes2 = PairingCodes(clock=clock2)
    other = codes2.mint(scope="host", label="build-mac")
    clock2.advance(HOST_TTL_SECONDS + 1)
    with pytest.raises(UnknownCode):
        codes2.redeem(other.code, source="1.2.3.4")


def test_failed_attempts_are_throttled_per_source():
    codes = PairingCodes()
    for _ in range(MAX_FAILURES):
        with pytest.raises(UnknownCode):
            codes.redeem("XXXXXXXX", source="1.2.3.4")
    with pytest.raises(ThrottledSource):
        codes.redeem("XXXXXXXX", source="1.2.3.4")


def test_throttle_is_scoped_to_one_source():
    codes = PairingCodes()
    for _ in range(MAX_FAILURES):
        with pytest.raises(UnknownCode):
            codes.redeem("XXXXXXXX", source="1.2.3.4")
    with pytest.raises(UnknownCode):
        codes.redeem("XXXXXXXX", source="5.6.7.8")


def test_throttle_window_rolls_off():
    clock = _Clock()
    codes = PairingCodes(clock=clock)
    for _ in range(MAX_FAILURES):
        with pytest.raises(UnknownCode):
            codes.redeem("XXXXXXXX", source="1.2.3.4")
    clock.advance(61.0)
    with pytest.raises(UnknownCode):
        codes.redeem("XXXXXXXX", source="1.2.3.4")


def test_peek_validates_without_burning():
    codes = PairingCodes()
    entry = codes.mint(scope="host", label="build-mac")
    assert codes.peek(entry.code, source="1.2.3.4").scope == "host"
    assert codes.redeem(entry.code, source="1.2.3.4").scope == "host"
