"""Tests for the Antigravity CLI (agy) structured driver and provider probe."""

from __future__ import annotations

import base64
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drover.server.harness.structured.agy import (
    AGY_PRINT_TIMEOUT,
    AgyDriver,
    default_command,
    resume_command,
)
from drover.server.providers.agy import (
    AgyUsageProbe,
    clear_observed_exhaustions,
    get_active_observed_exhaustions,
    model_group_from_model,
    parse_agy_quota_exhaustion,
    record_observed_exhaustion,
)

FAKE_AGY = (
    "import json,sys; args=sys.argv[1:]; "
    'idx=args.index("--print"); prompt=args[idx+1]; '
    'print(json.dumps({"event": "init", "conversation_id": "agy-conv-1"})); '
    'print(json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "echo: " + prompt}})); '
    'print(json.dumps({"event": "result", "result": {"conversation_id": "agy-conv-1", "status": "SUCCESS", "usage": {"input_tokens": 100, "output_tokens": 20}}}))'
)

# agy's own shape when its print deadline fires: a result event carrying
# status ERROR, an empty stderr, and exit 1. Taken from the transcript of
# harness session 25205799 on 2026-08-15.
FAILING_AGY_AFTER_RESULT = (
    "import json,sys; "
    'print(json.dumps({"event": "result", "result": {"status": "ERROR", '
    '"conversation_id": "agy-conv-1"}})); '
    "sys.exit(1)"
)

# The other half of the distinction: dead before it produced anything.
FAILING_AGY_NO_RESULT = "import sys; sys.exit(1)"

SLOW_AGY = (
    "import json,sys,time; args=sys.argv[1:]; "
    'idx=args.index("--print"); prompt=args[idx+1]; '
    "time.sleep(1.0); "
    'print(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))'
)


def _driver(sink: list, native_id: str | None = None) -> AgyDriver:
    return AgyDriver(
        [sys.executable, "-c", FAKE_AGY], None, sink.append, native_session_id=native_id
    )


def _wait_for(got: list, predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(got):
            return
        time.sleep(0.05)
    raise AssertionError([m.type for m in got])


# -- default_command & resume_command -----------------------------------------


def test_default_command():
    assert default_command("/usr/bin/agy") == ["/usr/bin/agy"]


def test_resume_command():
    cmd = resume_command(["agy"], "conv-123")
    assert cmd == ["agy", "--conversation", "conv-123"]


# -- lifecycle ---------------------------------------------------------------


def test_start_reports_ready():
    got: list = []
    driver = _driver(got)
    driver.start()
    assert got[0].type == "status"
    assert got[0].payload["awaiting"] == "input"
    driver.close()


def test_is_alive_until_close():
    driver = _driver([])
    assert driver.is_alive() is True
    driver.close()
    assert driver.is_alive() is False


# -- turn roundtrip ----------------------------------------------------------


def test_turn_roundtrip_emits_output_then_complete():
    got: list = []
    driver = _driver(got)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(
        got,
        lambda g: any(m.type == "status" and m.payload.get("turn_complete") for m in g),
    )
    output = next(m for m in got if m.type == "assistant_output")
    assert output.text == "echo: hello"
    assert output.turn_id == "t1"
    assert driver.native_session_id == "agy-conv-1"
    driver.close()


def test_argv_includes_required_flags():
    driver = AgyDriver(
        ["agy"], cwd=None, emit=lambda m: None, native_session_id="conv-456"
    )
    argv = driver._argv_for("hello", model="gemini-3.6-flash-high")
    assert "--dangerously-skip-permissions" in argv
    assert (
        "--output-format" in argv
        and argv[argv.index("--output-format") + 1] == "stream-json"
    )
    assert "--print" in argv and argv[argv.index("--print") + 1] == "hello"
    assert (
        "--model" in argv and argv[argv.index("--model") + 1] == "gemini-3.6-flash-high"
    )
    assert (
        "--conversation" in argv
        and argv[argv.index("--conversation") + 1] == "conv-456"
    )


def test_argv_scopes_workspace_to_cwd():
    # agy does not take its workspace from the process cwd -- without an
    # explicit --add-dir it runs in ~/.gemini/antigravity-cli/scratch no
    # matter what we hand Popen. Every turn is a fresh process, so the flag
    # has to be on every argv, not just the first.
    driver = AgyDriver(["agy"], cwd="/Volumes/M2 1/drover", emit=lambda m: None)
    argv = driver._argv_for("hello")
    assert "--add-dir" in argv
    assert argv[argv.index("--add-dir") + 1] == "/Volumes/M2 1/drover"


def test_argv_omits_add_dir_without_cwd():
    driver = AgyDriver(["agy"], cwd=None, emit=lambda m: None)
    assert "--add-dir" not in driver._argv_for("hello")


def test_argv_does_not_duplicate_caller_supplied_add_dir():
    driver = AgyDriver(
        ["agy", "--add-dir", "/srv/repo"], cwd="/tmp/other", emit=lambda m: None
    )
    argv = driver._argv_for("hello")
    assert argv.count("--add-dir") == 1
    assert argv[argv.index("--add-dir") + 1] == "/srv/repo"


# -- the print-mode deadline (issue #188) ----------------------------------
#
# `agy --print` gives a turn 5m0s by default and then ends it, reporting
# status ERROR and exiting 1 with nothing on stderr. Three turns died that way
# on 2026-08-15, each at the five minute mark measured from turn start, with
# the tool command they were waiting on (`uv run pytest`, a 13 minute suite)
# never returning a result. A coding turn routinely runs longer than five
# minutes, so the default is far too short to be left implicit.


def test_argv_sets_a_print_timeout_longer_than_agys_default():
    driver = AgyDriver(["agy"], cwd=None, emit=lambda m: None)

    argv = driver._argv_for("hello")

    assert "--print-timeout" in argv
    assert argv[argv.index("--print-timeout") + 1] == AGY_PRINT_TIMEOUT


def test_argv_keeps_a_caller_supplied_print_timeout():
    driver = AgyDriver(["agy", "--print-timeout", "30m"], cwd=None, emit=lambda m: None)

    argv = driver._argv_for("hello")

    assert argv.count("--print-timeout") == 1
    assert argv[argv.index("--print-timeout") + 1] == "30m"


# -- reporting a non-zero exit (issue #188) --------------------------------
#
# "the turn finished and then the process failed" and "the turn failed" are
# different things to a user deciding whether the session is worth continuing,
# and they were reported identically: one error bubble carrying the exit code
# and nothing else, because agy writes nothing to stderr on the way out.


def test_exit_after_a_completed_turn_says_so_and_names_the_status():
    got: list = []
    driver = AgyDriver(
        [sys.executable, "-c", FAILING_AGY_AFTER_RESULT], None, got.append
    )
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda g: any(m.type == "error" for m in g))

    error = next(m for m in got if m.type == "error")
    assert "after completing the turn" in error.text
    # agy's own verdict, which it did report and which was being discarded.
    assert "ERROR" in error.text
    assert error.payload["returncode"] == 1
    assert error.payload["after_turn_complete"] is True
    driver.close()


def test_exit_before_any_result_is_still_reported_as_a_failed_turn():
    got: list = []
    driver = AgyDriver([sys.executable, "-c", FAILING_AGY_NO_RESULT], None, got.append)
    driver.start()
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda g: any(m.type == "error" for m in g))

    error = next(m for m in got if m.type == "error")
    assert "after completing the turn" not in error.text
    assert error.payload["after_turn_complete"] is False
    driver.close()


def test_non_zero_exit_is_logged_server_side(caplog):
    got: list = []
    driver = AgyDriver(
        [sys.executable, "-c", FAILING_AGY_AFTER_RESULT], None, got.append
    )
    driver.start()
    with caplog.at_level(logging.WARNING, logger="drover.harnessd"):
        driver.send_turn("hello", turn_id="turn-42")
        _wait_for(got, lambda g: any(m.type == "error" for m in g))

    # Nothing recorded the exit at all, so a failure the user did not
    # screenshot left no trace anywhere on the hub.
    assert any(
        "agy" in r.message and "turn-42" in r.message and "1" in r.message
        for r in caplog.records
    )
    driver.close()


def test_parse_stream_line_tool():
    got: list = []
    driver = _driver(got)
    buf: list[str] = []
    tool_update = {
        "event": "step_update",
        "step_update": {
            "step_index": 3,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "pwd"}},
        },
    }
    messages = driver.parse_stream_line(json.dumps(tool_update), buf, "t1")
    assert len(messages) == 1
    assert messages[0].type == "tool_action"
    assert messages[0].payload["tool"] == "run_command"


@pytest.mark.parametrize(
    "line",
    [
        '{"event": "step_update", "step_update": {"step_type": "tool", '
        '"state": "ACTIVE", "tool_info": null}}',
        '{"event": "init", "init": null}',
        '{"event": "step_update", "step_update": null}',
    ],
)
def test_parse_stream_line_survives_null_sub_objects(line):
    """A null sub-object must not kill the turn thread.

    ``parse_stream_line`` runs on the pump thread, where an escaping
    exception ends the turn with no completion event at all -- the session
    just hangs.
    """
    driver = _driver([])

    driver.parse_stream_line(line, [], "t1")


def test_answer_permission_raises():
    driver = _driver([])
    with pytest.raises(RuntimeError, match="no interactive approvals"):
        driver.answer_permission("req-1", "allow")


def test_interrupt_terminates_an_in_flight_turn():
    got: list = []
    driver = AgyDriver([sys.executable, "-c", SLOW_AGY], None, got.append)
    driver.send_turn("hello", turn_id="t1")
    _wait_for(got, lambda _g: driver._turn_process is not None, timeout=5.0)
    driver.interrupt()
    _wait_for(got, lambda g: any(m.type == "error" for m in g))
    driver.close()
    assert driver.is_alive() is False


# -- provider probe ----------------------------------------------------------
#
# Every credential source is injected. Reading the real ``~/.gemini`` would
# make these pass or fail on whether this machine happens to be signed into
# agy, which is how a "hermetic" suite starts passing for the wrong reason.


def test_provider_probe_reports_the_signed_in_account(tmp_path: Path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com", "old": []}))

    snapshot = AgyUsageProbe(
        accounts_path=accounts, state_dir=tmp_path, keychain_reader=lambda: None
    ).read(host_id="test-host")

    assert snapshot.provider == "google"
    assert snapshot.host_id == "test-host"
    assert snapshot.account_label == "someone@example.com"


def test_provider_probe_says_capacity_is_unavailable_without_a_credential(
    tmp_path: Path,
):
    """No token anywhere means no capacity -- the card must not claim health."""
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))

    snapshot = AgyUsageProbe(
        accounts_path=accounts, state_dir=tmp_path, keychain_reader=lambda: None
    ).read()

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "not_authenticated"
    assert snapshot.windows == ()


# -- quota fetch -------------------------------------------------------------

# Verbatim shape of a real 200 from
# ``v1internal:retrieveUserQuotaSummary`` (Mac mini, 2026-08-10), trimmed to
# the fields the probe reads.
QUOTA_SUMMARY = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "window": "weekly",
                    "resetTime": "2026-08-16T20:02:26Z",
                    "remainingFraction": 0.9080874,
                },
                {
                    "bucketId": "gemini-5h",
                    "window": "5h",
                    "resetTime": "2026-08-11T03:15:27Z",
                    "remainingFraction": 0.9022169,
                },
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {
                    "bucketId": "3p-weekly",
                    "window": "weekly",
                    "resetTime": "2026-08-18T02:15:30Z",
                    "remainingFraction": 1,
                },
            ],
        },
    ],
}


def _cred(access: str = "tok-1", expiry: str = "2099-01-01T00:00:00Z") -> str:
    return json.dumps(
        {
            "auth_method": "consumer",
            "token": {
                "access_token": access,
                "refresh_token": "refresh-1",
                "token_type": "Bearer",
                "expiry": expiry,
            },
        }
    )


def _opener(calls: list, payload=QUOTA_SUMMARY, status: int = 200):
    def open_(url: str, headers: dict, body: bytes, timeout: float):
        calls.append({"url": url, "headers": headers, "body": body})
        return status, json.dumps(payload).encode()

    return open_


def test_probe_maps_the_quota_summary_into_windows(tmp_path: Path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))
    calls: list = []

    snapshot = AgyUsageProbe(
        accounts_path=accounts,
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=_opener(calls),
    ).read()

    assert snapshot.status == "ok"
    assert snapshot.error_category is None
    kinds = {w.kind: w for w in snapshot.windows}
    assert set(kinds) == {"five_hour", "seven_day", "seven_day_claude_gpt"}
    # remaining_fraction is what the API reports; the card wants used.
    assert kinds["seven_day"].used_percent == pytest.approx(9.19126, abs=1e-4)
    assert kinds["five_hour"].used_percent == pytest.approx(9.77831, abs=1e-4)
    assert kinds["seven_day_claude_gpt"].used_percent == pytest.approx(0.0)
    assert kinds["five_hour"].window_minutes == 300
    assert kinds["seven_day"].window_minutes == 10080
    assert kinds["seven_day"].resets_at is not None
    assert kinds["seven_day"].resets_at.year == 2026


def test_probe_sends_an_antigravity_user_agent(tmp_path: Path):
    """The gate that made this look unfetchable.

    Without an "antigravity" User-Agent the endpoint answers 403
    PERMISSION_DENIED even with a valid token, which reads as "not
    permitted" rather than "wrong header". A regression here would silently
    blank the card, so it is asserted rather than left to the live fleet.
    """
    calls: list = []

    AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=_opener(calls),
    ).read()

    assert calls, "probe made no request"
    agent = calls[-1]["headers"]["User-Agent"]
    assert "antigravity" in agent.lower()


def test_probe_reads_the_token_file_when_there_is_no_keychain(tmp_path: Path):
    """Linux hosts (the NAS) have no Keychain; go-keyring falls back to a file."""
    token_file = tmp_path / "antigravity-cli" / "antigravity-oauth-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(_cred(access="from-file"))
    calls: list = []

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: None,
        opener=_opener(calls),
    ).read()

    assert snapshot.status == "ok"
    assert calls[-1]["headers"]["Authorization"] == "Bearer from-file"


def test_probe_decodes_the_go_keyring_base64_wrapper(tmp_path: Path):
    blob = (
        "go-keyring-base64:"
        + base64.b64encode(_cred(access="from-keychain").encode()).decode()
    )
    calls: list = []

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: blob,
        opener=_opener(calls),
    ).read()

    assert snapshot.status == "ok"
    assert calls[-1]["headers"]["Authorization"] == "Bearer from-keychain"


def test_probe_refreshes_an_expired_token_without_writing_it_back(tmp_path: Path):
    """agy only refreshes while it runs, so an idle host's token is stale.

    The refreshed token is used in memory only -- writing back into agy's own
    credential store would risk corrupting the state of a live CLI.
    """
    token_file = tmp_path / "antigravity-cli" / "antigravity-oauth-token"
    token_file.parent.mkdir(parents=True)
    stale = _cred(access="expired", expiry="2020-01-01T00:00:00Z")
    token_file.write_text(stale)
    calls: list = []

    def open_(url: str, headers: dict, body: bytes, timeout: float):
        calls.append({"url": url, "headers": headers, "body": body})
        if "oauth2" in url:
            return (
                200,
                json.dumps({"access_token": "fresh", "expires_in": 3599}).encode(),
            )
        return 200, json.dumps(QUOTA_SUMMARY).encode()

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: None,
        opener=open_,
        oauth_clients=lambda: (("id-1", "secret-1"),),
    ).read()

    assert snapshot.status == "ok"
    assert "oauth2" in calls[0]["url"], "expired token was not refreshed"
    assert calls[-1]["headers"]["Authorization"] == "Bearer fresh"
    assert token_file.read_text() == stale, "probe rewrote agy's credential store"


def test_probe_tries_each_oauth_client_pairing(tmp_path: Path):
    """The binary yields ids and secrets with no clue which pairs with which."""
    calls: list = []

    def open_(url: str, headers: dict, body: bytes, timeout: float):
        calls.append(body)
        if "oauth2" in url:
            if b"secret-good" not in body:
                return 401, b'{"error": "invalid_client"}'
            return 200, json.dumps({"access_token": "fresh"}).encode()
        return 200, json.dumps(QUOTA_SUMMARY).encode()

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(expiry="2020-01-01T00:00:00Z"),
        opener=open_,
        oauth_clients=lambda: (("id-1", "secret-bad"), ("id-1", "secret-good")),
    ).read()

    assert snapshot.status == "ok"


def test_probe_reports_expired_when_no_oauth_client_can_be_found(tmp_path: Path):
    """No agy binary to read the client out of -- say so, do not crash."""
    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(expiry="2020-01-01T00:00:00Z"),
        opener=lambda *a: (200, b"{}"),
        oauth_clients=lambda: (),
    ).read()

    assert snapshot.status == "usage_unavailable"
    assert snapshot.error_category == "token_expired"


def test_probe_reports_unavailable_when_the_quota_call_is_rejected(tmp_path: Path):
    def open_(url: str, headers: dict, body: bytes, timeout: float):
        return 403, b'{"error": {"code": 403}}'

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=open_,
    ).read()

    assert snapshot.status == "usage_unavailable"
    assert snapshot.windows == ()


def test_probe_never_raises_when_the_transport_explodes(tmp_path: Path):
    """read() must never raise: harnessd's do_GET has no try wrapper."""

    def open_(url: str, headers: dict, body: bytes, timeout: float):
        raise OSError("connection reset")

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=open_,
    ).read()

    assert snapshot.status in ("usage_unavailable", "error")
    assert snapshot.windows == ()


def test_probe_ignores_disabled_buckets(tmp_path: Path):
    payload = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-5h",
                        "window": "5h",
                        "remainingFraction": 0.5,
                        "disabled": True,
                    },
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.5,
                    },
                ],
            }
        ]
    }

    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=_opener([], payload=payload),
    ).read()

    assert [w.kind for w in snapshot.windows] == ["seven_day"]


def test_provider_probe_reports_unknown_identity_without_credentials(tmp_path: Path):
    snapshot = AgyUsageProbe(
        accounts_path=tmp_path / "missing.json",
        state_dir=tmp_path,
        keychain_reader=lambda: None,
    ).read()

    assert snapshot.account_label == "Unknown account"
    assert snapshot.account_identity is None
    assert snapshot.status == "usage_unavailable"


def test_provider_probe_never_raises_on_a_broken_accounts_file(tmp_path: Path):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text("{ not json")

    snapshot = AgyUsageProbe(
        accounts_path=accounts, state_dir=tmp_path, keychain_reader=lambda: None
    ).read()

    assert snapshot.account_label == "Unknown account"
    assert snapshot.account_identity is None


def test_provider_probe_does_not_attribute_an_old_account_when_active_is_null(
    tmp_path: Path,
):
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": None, "old": ["someone@example.com"]}))

    snapshot = AgyUsageProbe(
        accounts_path=accounts, state_dir=tmp_path, keychain_reader=lambda: None
    ).read()

    assert snapshot.account_label == "Unknown account"
    assert snapshot.account_identity is None


@pytest.mark.parametrize(
    "fixture,email",
    [
        ("identity-present", "ArnieSaha@gmail.com"),
        ("identity-missing", None),
    ],
)
@pytest.mark.parametrize("store", ["file", "keychain"])
def test_current_agy_credential_identity(tmp_path: Path, fixture: str, email, store):
    credential = (
        Path(__file__).parent / "fixtures" / "agy" / f"{fixture}.json"
    ).read_text()
    if store == "file":
        token_file = tmp_path / "antigravity-cli" / "antigravity-oauth-token"
        token_file.parent.mkdir()
        token_file.write_text(credential)
    snapshot = AgyUsageProbe(
        state_dir=tmp_path,
        keychain_reader=lambda: credential if store == "keychain" else None,
        opener=_opener([]),
    ).read()
    assert snapshot.status == "ok"
    assert snapshot.account_label == (email or "Unknown account")
    assert snapshot.account_identity == (email.lower() if email else None)


def test_current_credential_wins_over_old_accounts_file(tmp_path: Path):
    (tmp_path / "google_accounts.json").write_text(
        json.dumps({"active": "old@example.com"})
    )
    credential = (
        Path(__file__).parent / "fixtures/agy/identity-present.json"
    ).read_text()
    snapshot = AgyUsageProbe(
        state_dir=tmp_path, keychain_reader=lambda: credential, opener=_opener([])
    ).read()
    assert snapshot.account_identity == "arniesaha@gmail.com"


def test_malformed_id_token_does_not_hide_quota(tmp_path: Path):
    credential = json.loads(_cred())
    credential["id_token"] = "header.!invalid!.signature"
    snapshot = AgyUsageProbe(
        state_dir=tmp_path,
        keychain_reader=lambda: json.dumps(credential),
        opener=_opener([]),
    ).read()
    assert snapshot.status == "ok"
    assert snapshot.account_identity is None


def test_id_token_subject_is_hashed_when_email_is_absent(tmp_path: Path):
    import base64
    import hashlib

    credential = json.loads(_cred())
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "google-user-id"}).encode())
        .decode()
        .rstrip("=")
    )
    credential["id_token"] = f"header.{payload}.signature"
    snapshot = AgyUsageProbe(
        state_dir=tmp_path,
        keychain_reader=lambda: json.dumps(credential),
        opener=_opener([]),
    ).read()
    assert (
        snapshot.account_identity
        == "google-sub:" + hashlib.sha256(b"google-user-id").hexdigest()
    )
    assert snapshot.account_label == "Unknown account"


def test_parse_agy_quota_exhaustion_durations():
    base = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)

    # h m s
    res = parse_agy_quota_exhaustion(
        "Individual quota reached for Claude Sonnet 3.5. Resets in 162h52m18s",
        now=base,
    )
    assert res == base + timedelta(hours=162, minutes=52, seconds=18)

    # 5h
    res = parse_agy_quota_exhaustion(
        "RESOURCE_EXHAUSTED: Individual quota reached. Resets in 5h",
        now=base,
    )
    assert res == base + timedelta(hours=5)

    # 30m
    res = parse_agy_quota_exhaustion(
        "Individual quota reached ... Resets in 30m",
        now=base,
    )
    assert res == base + timedelta(minutes=30)

    # 45s
    res = parse_agy_quota_exhaustion("Resets in 45s", now=base)
    assert res == base + timedelta(seconds=45)

    # 2h15m30s
    res = parse_agy_quota_exhaustion("Quota exceeded. Resets in 2h15m30s", now=base)
    assert res == base + timedelta(hours=2, minutes=15, seconds=30)

    # Negative / non-matching
    assert parse_agy_quota_exhaustion("Regular error message", now=base) is None
    assert (
        parse_agy_quota_exhaustion("RESOURCE_EXHAUSTED without time", now=base) is None
    )
    assert parse_agy_quota_exhaustion("", now=base) is None


def test_model_group_from_model():
    assert model_group_from_model("claude-sonnet-4") == "3p"
    assert model_group_from_model("claude-3-5-sonnet") == "3p"
    assert model_group_from_model("gpt-4o") == "3p"
    assert model_group_from_model("gpt-4o-mini") == "3p"
    assert model_group_from_model("gemini-2.5-pro") == "gemini"
    assert model_group_from_model("gemini-1.5-flash") == "gemini"
    assert model_group_from_model(None) == "gemini"
    assert model_group_from_model("custom-model") == "gemini"


def test_sliding_response_omits_untracked_buckets(tmp_path: Path):
    """Buckets whose resetTime slides with fetch_time + window are omitted."""
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))

    fetch_time = datetime(2026, 8, 10, 10, 0, 0, tzinfo=timezone.utc)
    # Sliding weekly bucket: resetTime is fetch_time + 7 days (within 120s tolerance)
    sliding_weekly_reset = fetch_time + timedelta(days=7, seconds=10)
    # Sliding 5h bucket: resetTime is fetch_time + 5h (within 120s tolerance)
    sliding_5h_reset = fetch_time + timedelta(hours=5, seconds=5)
    # Fixed gemini weekly bucket: resetTime is fixed (e.g. 4 days from now)
    fixed_gemini_reset = fetch_time + timedelta(days=4)

    payload = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "resetTime": fixed_gemini_reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "remainingFraction": 0.85,
                    }
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "window": "weekly",
                        "resetTime": sliding_weekly_reset.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "remainingFraction": 1,
                    },
                    {
                        "bucketId": "3p-5h",
                        "window": "5h",
                        "resetTime": sliding_5h_reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "remainingFraction": 1,
                    },
                ],
            },
        ]
    }

    calls: list = []
    snapshot = AgyUsageProbe(
        accounts_path=accounts,
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=_opener(calls, payload=payload),
        now=lambda: fetch_time,
    ).read()

    assert snapshot.status == "ok"
    # Sliding 3p buckets must be omitted, never reported as 100% remaining (0% used)
    kinds = {w.kind: w for w in snapshot.windows}
    assert "seven_day" in kinds
    assert "seven_day_claude_gpt" not in kinds
    assert "five_hour_claude_gpt" not in kinds
    assert kinds["seven_day"].used_percent == pytest.approx(15.0)


def test_sliding_response_with_no_fixed_buckets_is_usage_unavailable(tmp_path: Path):
    """When all buckets in response slide, status is usage_unavailable."""
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))

    fetch_time = datetime(2026, 8, 10, 10, 0, 0, tzinfo=timezone.utc)
    sliding_weekly_reset = fetch_time + timedelta(days=7)

    payload = {
        "groups": [
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "window": "weekly",
                        "resetTime": sliding_weekly_reset.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "remainingFraction": 1,
                    }
                ],
            }
        ]
    }

    calls: list = []
    snapshot = AgyUsageProbe(
        accounts_path=accounts,
        state_dir=tmp_path,
        keychain_reader=lambda: _cred(),
        opener=_opener(calls, payload=payload),
        now=lambda: fetch_time,
    ).read()

    assert snapshot.status == "usage_unavailable"
    assert snapshot.windows == ()
    assert snapshot.error_category == "quota_api_unreachable"


def test_observed_exhaustion_snapshot_and_expiry(tmp_path: Path):
    """Observed exhaustion marks status observed_exhausted, source observed_429 until reset."""
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))

    clear_observed_exhaustions()
    try:
        t0 = datetime(2026, 8, 10, 10, 0, 0, tzinfo=timezone.utc)
        reset_time = t0 + timedelta(hours=2)

        record_observed_exhaustion(
            host_id="test-host",
            model_group="3p",
            resets_at=reset_time,
            observed_at=t0,
        )

        calls: list = []
        probe = AgyUsageProbe(
            accounts_path=accounts,
            state_dir=tmp_path,
            keychain_reader=lambda: _cred(),
            opener=_opener(calls, payload={"groups": []}),
            now=lambda: t0 + timedelta(minutes=15),
        )

        # Before reset passes: status is observed_exhausted, source observed_429
        snap1 = probe.read(host_id="test-host")
        assert snap1.status == "observed_exhausted"
        assert snap1.source == "observed_429"
        assert snap1.error_category is None
        kinds = {w.kind: w for w in snap1.windows}
        assert "five_hour_claude_gpt" in kinds
        assert kinds["five_hour_claude_gpt"].used_percent == 100.0
        assert kinds["five_hour_claude_gpt"].resets_at == reset_time

        # Another host is unaffected
        snap_other = probe.read(host_id="other-host")
        assert snap_other.status == "usage_unavailable"
        assert snap_other.source == "agy-usage"

        # After reset passes: status reverts to normal probe status
        probe.now = lambda: t0 + timedelta(hours=2, minutes=1)
        snap2 = probe.read(host_id="test-host")
        assert snap2.status == "usage_unavailable"
        assert snap2.source == "agy-usage"
    finally:
        clear_observed_exhaustions()


def test_gemini_unaffected_by_3p_exhaustion(tmp_path: Path):
    """Gemini quota windows remain accurate when 3p is observed exhausted."""
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}))

    clear_observed_exhaustions()
    try:
        t0 = datetime(2026, 8, 10, 10, 0, 0, tzinfo=timezone.utc)
        gemini_reset = t0 + timedelta(hours=3)
        claude_reset = t0 + timedelta(hours=162)

        gemini_payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-5h",
                            "window": "5h",
                            "resetTime": gemini_reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "remainingFraction": 0.75,
                        }
                    ],
                }
            ]
        }

        record_observed_exhaustion(
            host_id="test-host",
            model_group="3p",
            resets_at=claude_reset,
            observed_at=t0,
        )

        calls: list = []
        snap = AgyUsageProbe(
            accounts_path=accounts,
            state_dir=tmp_path,
            keychain_reader=lambda: _cred(),
            opener=_opener(calls, payload=gemini_payload),
            now=lambda: t0,
        ).read(host_id="test-host")

        assert snap.status == "observed_exhausted"
        assert snap.source == "observed_429"
        kinds = {w.kind: w for w in snap.windows}
        # Gemini 5h window is preserved with its 25% used
        assert kinds["five_hour"].used_percent == pytest.approx(25.0)
        assert kinds["five_hour"].resets_at == gemini_reset
        # Claude weekly window is injected at 100% used
        assert kinds["seven_day_claude_gpt"].used_percent == 100.0
        assert kinds["seven_day_claude_gpt"].resets_at == claude_reset
    finally:
        clear_observed_exhaustions()


def test_driver_check_and_record_exhaustion():
    """AgyDriver records observed exhaustion for host and model group on 429 error."""
    clear_observed_exhaustions()
    try:
        driver = AgyDriver(["agy"], None, lambda _: None, host_id="host-xyz")
        driver._turn_model = "claude-3-5-sonnet"

        driver._check_and_record_exhaustion(
            stderr_text="",
            reported_status="AGY_ERROR RESOURCE_EXHAUSTED",
            error_text="Individual quota reached for Claude 3.5 Sonnet. Resets in 162h52m18s",
        )

        active = get_active_observed_exhaustions(host_id="host-xyz")
        assert len(active) == 1
        assert active[0].host_id == "host-xyz"
        assert active[0].model_group == "3p"
        assert active[0].resets_at > datetime.now(timezone.utc) + timedelta(hours=160)
    finally:
        clear_observed_exhaustions()
