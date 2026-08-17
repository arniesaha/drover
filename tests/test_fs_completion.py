"""Tests for harnessd's filesystem path-completion endpoints.

The "New Session" screen completes a working directory against the *selected
host's* real filesystem, one request per keystroke. Two properties matter more
than anything else here:

- Half-typed paths are the normal case, not an error, so a parent that does not
  exist (or cannot be read) still answers 200 with an empty list.
- The endpoints touch the filesystem and nothing else. A per-keystroke call that
  reached the registry, DuckDB, or the control-plane lock would starve session
  creation the way the ``_list_sessions`` N+1 did.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from drover.schema import bootstrap
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    create_harness_server,
    register_daemon_host,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry

TOKEN = "fs-completion-token"


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("fs-completion-daemon")
    duckdb_path = root / "drover.duckdb"
    bootstrap(parquet_dir=root / "parquet", duckdb_path=duckdb_path)
    state = HarnessDaemonState(
        host_id="test-host",
        display_name="Test Host",
        kind="linux",
        registry=HarnessRegistry(duckdb_path),
        pty=PtySessionManager(),
        presets=DEFAULT_PRESETS,
        local_url="http://127.0.0.1:0",
        api_token=TOKEN,
        worktrees_dir=root / "worktrees",
    )
    register_daemon_host(state)
    server = create_harness_server(listen_host="127.0.0.1", listen_port=0, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _complete(base_url: str, typed: str | None = None, *, token: str | None = TOKEN):
    url = f"{base_url}/fs/complete"
    if typed is not None:
        url = f"{url}?{urlencode({'path': typed})}"
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _exists(base_url: str, paths, *, token: str | None = TOKEN):
    data = json.dumps({"paths": paths}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url}/fs/exists", data=data, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _names(payload) -> list[str]:
    return [entry["name"] for entry in payload["entries"]]


def test_completion_returns_directories_and_never_plain_files(base_url, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "notes.txt").write_text("not a cwd")

    status, payload = _complete(base_url, f"{tmp_path}/")

    assert status == 200
    assert _names(payload) == ["alpha", "beta"]
    assert payload["truncated"] is False
    assert "error" not in payload
    assert payload["parent"] == str(tmp_path)
    assert payload["entries"][0]["path"] == str(tmp_path / "alpha")


def test_completion_filters_by_partial_case_insensitively(base_url, tmp_path):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "protocols").mkdir()
    (tmp_path / "scratch").mkdir()

    _, payload = _complete(base_url, f"{tmp_path}/pro")

    assert _names(payload) == ["Projects", "protocols"]


def test_trailing_slash_lists_the_directory_itself(base_url, tmp_path):
    nested = tmp_path / "workspace"
    nested.mkdir()
    (nested / "drover").mkdir()

    _, with_slash = _complete(base_url, f"{nested}/")
    _, without_slash = _complete(base_url, str(nested))

    assert with_slash["parent"] == str(nested)
    assert _names(with_slash) == ["drover"]
    # Without the slash the last segment is a *partial*, so the parent moves up.
    assert without_slash["parent"] == str(tmp_path)
    assert _names(without_slash) == ["workspace"]


def test_sorting_is_case_insensitive_by_name(base_url, tmp_path):
    for name in ("Zebra", "apple", "Banana", "cherry"):
        (tmp_path / name).mkdir()

    _, payload = _complete(base_url, f"{tmp_path}/")

    assert _names(payload) == ["apple", "Banana", "cherry", "Zebra"]


def test_results_are_capped_at_fifty_and_flag_truncation(base_url, tmp_path):
    for index in range(60):
        (tmp_path / f"dir{index:03d}").mkdir()

    _, payload = _complete(base_url, f"{tmp_path}/")

    assert len(payload["entries"]) == 50
    assert payload["truncated"] is True
    # The cap applies after sorting, so it is the alphabetical head.
    assert _names(payload)[0] == "dir000"
    assert _names(payload)[-1] == "dir049"


def test_exactly_fifty_entries_is_not_truncated(base_url, tmp_path):
    for index in range(50):
        (tmp_path / f"dir{index:03d}").mkdir()

    _, payload = _complete(base_url, f"{tmp_path}/")

    assert len(payload["entries"]) == 50
    assert payload["truncated"] is False


def test_hidden_entries_appear_only_when_the_partial_starts_with_a_dot(
    base_url, tmp_path
):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".cache").mkdir()
    (tmp_path / "code").mkdir()

    _, plain = _complete(base_url, f"{tmp_path}/")
    _, dotted = _complete(base_url, f"{tmp_path}/.")
    _, dotted_prefix = _complete(base_url, f"{tmp_path}/.co")

    assert _names(plain) == ["code"]
    assert _names(dotted) == [".cache", ".config"]
    assert _names(dotted_prefix) == [".config"]


def test_leading_tilde_expands_to_the_host_users_home(base_url, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "Developer").mkdir()
    monkeypatch.setenv("HOME", str(home))

    _, listed = _complete(base_url, "~/")
    _, partial = _complete(base_url, "~/Dev")

    assert listed["parent"] == str(home)
    assert _names(listed) == ["Developer"]
    assert _names(partial) == ["Developer"]
    assert partial["entries"][0]["path"] == str(home / "Developer")


def test_a_missing_parent_is_an_empty_list_not_an_error(base_url, tmp_path):
    status, payload = _complete(base_url, f"{tmp_path}/nope/deeper/th")

    assert status == 200
    assert payload["entries"] == []
    assert payload["truncated"] is False
    assert payload["error"] == "not_found"


def test_a_parent_that_is_a_file_is_an_empty_list_not_an_error(base_url, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x")

    status, payload = _complete(base_url, f"{target}/any")

    assert status == 200
    assert payload["entries"] == []
    assert payload["error"] == "not_found"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a mode-000 directory",
)
def test_an_unreadable_parent_reports_permission_denied(base_url, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "inside").mkdir()
    locked.chmod(0o000)
    try:
        status, payload = _complete(base_url, f"{locked}/")
    finally:
        locked.chmod(0o755)

    assert status == 200
    assert payload["entries"] == []
    assert payload["error"] == "permission_denied"


def test_a_missing_path_parameter_lists_the_filesystem_root(base_url):
    status, payload = _complete(base_url)

    assert status == 200
    assert payload["parent"] == "/"
    assert "error" not in payload
    assert isinstance(payload["entries"], list)


def test_an_empty_path_parameter_lists_the_filesystem_root(base_url):
    status, payload = _complete(base_url, "")

    assert status == 200
    assert payload["parent"] == "/"
    assert isinstance(payload["entries"], list)


def test_completion_requires_the_bearer_token(base_url):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _complete(base_url, "/", token=None)
    assert excinfo.value.code == 401

    with pytest.raises(urllib.error.HTTPError) as wrong:
        _complete(base_url, "/", token="not-the-token")
    assert wrong.value.code == 401


def test_exists_requires_the_bearer_token(base_url):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _exists(base_url, ["/"], token=None)
    assert excinfo.value.code == 401


def test_exists_is_true_only_for_directories(base_url, tmp_path):
    directory = tmp_path / "adir"
    directory.mkdir()
    plain = tmp_path / "afile"
    plain.write_text("x")
    missing = tmp_path / "gone"

    status, payload = _exists(base_url, [str(directory), str(plain), str(missing)])

    assert status == 200
    assert payload["exists"] == {
        str(directory): True,
        str(plain): False,
        str(missing): False,
    }


def test_exists_caps_the_input_at_sixty_four_paths(base_url, tmp_path):
    paths = []
    for index in range(70):
        entry = tmp_path / f"d{index:03d}"
        entry.mkdir()
        paths.append(str(entry))

    _, payload = _exists(base_url, paths)

    assert len(payload["exists"]) == 64
    assert paths[63] in payload["exists"]
    assert paths[64] not in payload["exists"]


def test_exists_echoes_the_callers_path_strings_verbatim(
    base_url, tmp_path, monkeypatch
):
    """The client matches keys by exact string, so normalizing them silently
    breaks stale-path filtering: an unmatched key reads as "not answered", the
    path stays visible, and nothing anywhere reports a problem."""

    home = tmp_path / "home"
    home.mkdir()
    (home / "Developer").mkdir()
    monkeypatch.setenv("HOME", str(home))
    directory = tmp_path / "plain"
    directory.mkdir()

    asked = [
        f"{directory}/",  # trailing slash
        f"{tmp_path}//plain",  # doubled separator
        "~/Developer",  # needs expansion to answer
        str(directory),
    ]
    _, payload = _exists(base_url, asked)

    # Byte-identical keys, not the normalized forms used to answer.
    assert set(payload["exists"]) == set(asked)
    assert all(payload["exists"][path] is True for path in asked)


def test_exists_never_raises_on_junk_input(base_url):
    status, payload = _exists(base_url, ["", "\x00bad", 17, None, {"a": 1}])

    assert status == 200
    assert payload["exists"].get("") is False
    assert payload["exists"].get("\x00bad") is False


def test_exists_tolerates_a_missing_paths_key(base_url):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    request = urllib.request.Request(
        f"{base_url}/fs/exists",
        data=json.dumps({}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert payload == {"exists": {}}


def test_completion_stays_out_of_the_control_plane(base_url, tmp_path, monkeypatch):
    """A per-keystroke endpoint anywhere near the control plane starves creates.

    ``_list_sessions`` once opened 115 DuckDB instances per request and pushed
    ``GET /sessions`` to 42-55s, which starved session creation. Completion runs
    on every keystroke, so it must be filesystem-only: no registry, no DuckDB,
    no control-plane lock.
    """

    import inspect

    import drover.server.harness.daemon as daemon_module

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("fs completion must not construct a HarnessRegistry")

    monkeypatch.setattr(daemon_module, "HarnessRegistry", _boom)
    (tmp_path / "somewhere").mkdir()

    status, payload = _complete(base_url, f"{tmp_path}/some")

    assert status == 200
    assert _names(payload) == ["somewhere"]

    for name in ("_fs_complete", "_fs_exists"):
        source = inspect.getsource(
            getattr(daemon_module.HarnessRequestHandler, name)
        ).lower()
        for forbidden in ("registry", "duckdb", "lock", "recovery"):
            assert forbidden not in source, f"{name} must not reach {forbidden}"
