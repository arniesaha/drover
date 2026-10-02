"""Public capability v1, including hostile and mixed-version host declarations."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from drover.schema import bootstrap
from drover.server.harness import daemon
from drover.server.harness.adapters import HarnessAdapterRegistry
from drover.server.harness.auth import AuthFlowManager
from drover.server.harness.capabilities import (
    MAX_ENVELOPE_BYTES,
    MAX_MATRIX_BYTES,
    InvalidCapabilities,
    validate_capabilities,
)
from drover.server.harness.daemon import (
    DEFAULT_PRESETS,
    HarnessDaemonState,
    HarnessPreset,
)
from drover.server.harness.pty import PtySessionManager
from drover.server.harness.registry import HarnessRegistry
from drover.server.metrics import MetricsCollector


@pytest.fixture
def collector(tmp_path):
    path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=path)
    return MetricsCollector(
        duckdb_path=path, incoming_dir=tmp_path / "incoming", summarizer_report={}
    )


def state(tmp_path):
    return HarnessDaemonState(
        host_id="test-host",
        display_name="Test Host",
        kind="linux",
        registry=HarnessRegistry(tmp_path / "host.duckdb"),
        pty=PtySessionManager(),
        presets=dict(DEFAULT_PRESETS),
        auth=AuthFlowManager({}),
    )


def envelope(matrix=None):
    return {
        "host_id": "test-host",
        "harnesses": [
            {
                "name": "codex",
                "enabled": True,
                "description": "Codex CLI",
                "command": [],
                "capabilities": (
                    matrix
                    if matrix is not None
                    else {
                        "schema_version": 1,
                        "harness_id": "codex",
                        "launch_modes": ["structured"],
                    }
                ),
            }
        ],
    }


def test_every_offered_harness_emits_registry_contract_without_probes(
    tmp_path, monkeypatch
):
    host = state(tmp_path)
    for harness_id in host.adapters.ids():
        adapter = host.adapters.resolve(harness_id)

        def forbidden(*args, **kwargs):
            pytest.fail("capability publication must not invoke runtime hooks")

        for method in (
            "health",
            "default_command",
            "auth_adapter",
            "model_catalog_adapter",
        ):
            monkeypatch.setattr(adapter, method, forbidden)
    public = host.capabilities()
    assert {row["name"] for row in public["harnesses"]} == set(DEFAULT_PRESETS)
    for row in public["harnesses"]:
        assert {"name", "enabled", "description", "command", "capabilities"} == set(row)
        matrix = row["capabilities"]
        assert matrix["schema_version"] == 1
        assert matrix["harness_id"] == row["name"]
        if row["name"] == "shell":
            assert matrix["launch_modes"] == ["pty"]
            assert not matrix["interrupt"]
        else:
            declared = host.adapters.resolve(row["name"]).capabilities
            assert matrix["launch_modes"] == sorted(declared.launch_modes)
            for key, value in vars(declared).items():
                assert matrix[key] == (
                    sorted(value) if isinstance(value, frozenset) else value
                )
    assert len(json.dumps(public)) < 4096


def test_unregistered_preset_cannot_be_presented_as_launchable(tmp_path):
    host = state(tmp_path)
    host.adapters = HarnessAdapterRegistry()
    host.presets = {"fixture": HarnessPreset("fixture", ("secret",), True, "Fixture")}
    row = host.capabilities()["harnesses"][0]
    assert row["enabled"] is False  # old clients also fail closed
    assert row["capabilities"]["launch_modes"] == []


def test_empty_launch_modes_disable_even_a_known_enabled_harness():
    result = validate_capabilities(
        envelope({"schema_version": 1, "launch_modes": []}), "test-host"
    )
    assert result["harnesses"][0]["enabled"] is False


def test_unknown_fields_are_ignored_and_missing_flags_are_false():
    payload = envelope()
    payload["credentials"] = {"secret": "not-public"}
    row = payload["harnesses"][0]
    row["prompt"] = "not-public"
    row["capabilities"]["native_auth"] = {"token": "not-public"}
    result = validate_capabilities(payload, "test-host")
    assert "not-public" not in json.dumps(result)
    matrix = result["harnesses"][0]["capabilities"]
    assert not matrix["interactive_auth"]
    assert not matrix["native_resume"]
    assert matrix["attachments"] == []
    assert matrix["turn_preferences"] is False


def test_turn_preferences_follow_the_adapter_turn_dispatch_contract(tmp_path):
    host = state(tmp_path)
    rows = {row["name"]: row["capabilities"] for row in host.capabilities()["harnesses"]}
    for harness_id in host.adapters.ids():
        adapter = host.adapters.resolve(harness_id)
        assert rows[harness_id]["turn_preferences"] is (
            adapter.turn_preferences_mutable and adapter.capabilities.model_catalog
        )
    # Claude's persistent process drops later overrides at turn dispatch.
    assert rows["claude-code"]["turn_preferences"] is False
    assert rows["codex"]["turn_preferences"] is True
    assert rows["shell"]["turn_preferences"] is False


def test_turn_preferences_must_be_a_strict_boolean():
    with pytest.raises(InvalidCapabilities):
        validate_capabilities(
            envelope(
                {"schema_version": 1, "launch_modes": ["structured"], "turn_preferences": 1}
            ),
            "test-host",
        )


def test_legacy_fallback_never_synthesizes_a_matrix():
    legacy = {"harnesses": ["shell", {"name": "codex beta", "enabled": True}]}
    assert validate_capabilities(legacy, "old-host") == legacy
    assert validate_capabilities(None, "old-host") == {}
    assert validate_capabilities({}, "old-host") == {}


INVALID_MATRICES = [
    None,
    [],
    {},
    {"schema_version": True, "launch_modes": []},
    {"schema_version": "1", "launch_modes": []},
    {"schema_version": 0, "launch_modes": []},
    {"schema_version": 1, "launch_modes": "structured"},
    {"schema_version": 1, "launch_modes": ["telepathy"]},
    {"schema_version": 1, "launch_modes": ["pty", "pty"]},
    {"schema_version": 1, "launch_modes": [{}]},
    {"schema_version": 1, "launch_modes": [], "interrupt": 1},
    {"schema_version": 1, "launch_modes": [], "attachments": True},
    {
        "schema_version": 1,
        "launch_modes": [],
        "attachments": ["image/png", "image/png"],
    },
    {"schema_version": 1, "launch_modes": [], "attachments": ["not-a-mime"]},
    {
        "schema_version": 1,
        "launch_modes": [],
        "attachments": [f"image/type{i}" for i in range(17)],
    },
    {"schema_version": 1, "launch_modes": [], "harness_id": "other"},
    {"schema_version": 1, "launch_modes": [], "future": "x" * MAX_MATRIX_BYTES},
]


@pytest.mark.parametrize("matrix", INVALID_MATRICES)
def test_reject_invalid_matrix_before_persistence(tmp_path, matrix):
    payload = envelope()
    payload["harnesses"][0]["capabilities"] = matrix
    registry = HarnessRegistry(tmp_path / "never-created.duckdb")
    with pytest.raises(InvalidCapabilities):
        registry.register_host(
            host_id="test-host", display_name="Test", kind="linux", capabilities=payload
        )
    assert not registry.control_plane_path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"host_id": "other"},
        {"harnesses": {}},
        {"harnesses": ["shell", "shell"]},
        {"harnesses": ["shell", {"name": "shell"}]},
        {"harnesses": [{"name": "codex", "enabled": "true"}]},
        {"harnesses": [{"name": "codex", "command": "secret"}]},
        {
            "harnesses": [
                {
                    "name": "../codex",
                    "capabilities": {"schema_version": 1, "launch_modes": []},
                }
            ]
        },
        {"harnesses": [{"name": " "}]},
        {"harnesses": ["x" * 257]},
        {"harnesses": [f"harness-{i}" for i in range(33)]},
        {"harnesses": [{"name": "codex", "description": "x" * 1025}]},
        {"unknown": "x" * MAX_ENVELOPE_BYTES},
    ],
)
def test_reject_invalid_envelopes(payload):
    with pytest.raises(InvalidCapabilities):
        validate_capabilities(payload, "test-host")


def test_mixed_versions_persist_and_proxy_without_inventing_capabilities(collector):
    payload = envelope()
    payload["harnesses"].extend(
        [
            {"name": "shell", "enabled": True},
            {
                "name": "future",
                "enabled": True,
                "capabilities": {
                    "schema_version": 2,
                    "launch_modes": ["structured"],
                    "interrupt": True,
                },
            },
        ]
    )
    status, body = collector.register_harness_host(
        {"host_id": "test-host", "capabilities": payload}
    )
    assert status == 200
    public = json.loads(body)["host"]["capabilities"]
    persisted = (
        HarnessRegistry(collector.duckdb_path).get_host("test-host").capabilities
    )
    assert public == persisted
    for include_sessions in (False, True):
        proxied = json.loads(
            collector.render_harness_json(include_sessions=include_sessions)
        )
        assert proxied["hosts"][0]["capabilities"] == public
    current, legacy, future = public["harnesses"]
    assert current["capabilities"]["launch_modes"] == ["structured"]
    assert legacy == {"name": "shell", "enabled": True}
    assert future["enabled"] is False
    assert future["capabilities"] == {
        "schema_version": 2,
        "harness_id": "future",
        "launch_modes": [],
    }


def test_bad_registration_returns_400_without_overwriting_good_data_or_logging_secrets(
    collector, caplog
):
    good = envelope()
    assert (
        collector.register_harness_host({"host_id": "test-host", "capabilities": good})[
            0
        ]
        == 200
    )
    registry = HarnessRegistry(collector.duckdb_path)
    before = registry.get_host("test-host")
    bad = envelope(
        {"schema_version": 1, "launch_modes": ["credential-prompt-native-auth-secret"]}
    )
    status, body = collector.register_harness_host(
        {"host_id": "test-host", "capabilities": bad}
    )
    assert status == 400
    assert "credential-prompt-native-auth-secret" not in body + caplog.text
    assert registry.get_host("test-host") == before


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"host_id":"wrong"}',
        '{"harnesses":[{"name":"codex","capabilities":null}]}',
        '{"harnesses":[],"harnesses":["shell"]}',
        json.dumps({"unknown": "x" * MAX_ENVELOPE_BYTES}),
    ],
)
def test_pre_upgrade_invalid_storage_fails_closed_in_listings(collector, raw):
    registry = HarnessRegistry(collector.duckdb_path)
    registry.register_host(host_id="bad", display_name="Bad", kind="linux")
    registry.register_host(
        host_id="good",
        display_name="Good",
        kind="linux",
        capabilities={"harnesses": ["shell"]},
    )
    with registry._connect() as con:
        con.execute(
            "UPDATE harness_hosts SET capabilities_json = ? WHERE host_id = 'bad'",
            [raw],
        )
    result = json.loads(collector.render_harness_json(include_sessions=False))
    hosts = {host["host_id"]: host for host in result["hosts"]}
    assert hosts["bad"]["capabilities"] == {}
    assert hosts["good"]["capabilities"] == {"harnesses": ["shell"]}


def test_no_credentials_prompts_or_native_auth_on_wire_or_disk(
    tmp_path, collector, monkeypatch
):
    host = state(tmp_path)
    host.api_token = "credential-secret"
    host.host_token = "host-token-secret"
    host.pending_initial_input = {"session": "prompt-secret"}
    host.presets["codex"] = replace(
        host.presets["codex"], command=("codex", "--token", "native-auth-secret")
    )
    public = host.capabilities()
    # Old hosts can send these too: only the allowlisted declaration survives.
    public["credentials"] = host.api_token
    public["harnesses"][0]["command"] = ["env", "TOKEN=credential-secret"]
    public["harnesses"][0]["prompt"] = "prompt-secret"
    public["harnesses"][0]["capabilities"]["native_auth"] = {
        "access_token": "native-auth-secret"
    }
    status, body = collector.register_harness_host(
        {"host_id": host.host_id, "capabilities": public}
    )
    assert status == 200
    registry = HarnessRegistry(collector.duckdb_path)
    with registry._connect() as con:
        stored = con.execute("SELECT capabilities_json FROM harness_hosts").fetchone()[
            0
        ]
    for wire in (
        json.dumps(host.capabilities()),
        body,
        stored,
        collector.render_harness_json(include_sessions=False),
    ):
        for secret in (
            "credential-secret",
            "host-token-secret",
            "prompt-secret",
            "native-auth-secret",
        ):
            assert secret not in wire
    assert all(row["command"] == [] for row in json.loads(stored)["harnesses"])


def test_current_host_envelope_matches_ios_compatibility_fixture(tmp_path):
    fixture = (
        Path(__file__).parents[1]
        / "apps/drover/DroverKit/Tests/DroverKitTests/Fixtures/harness-capabilities-v1.json"
    )
    snapshot = json.loads(fixture.read_text())
    assert snapshot["hosts"][0]["capabilities"] == state(tmp_path).capabilities()


def test_defaults_cannot_grow_persisted_envelope_past_limit():
    # Valid MIME strings and descriptions fill the input without unknown keys
    # to discard; adding default booleans would push the projection over 64KiB.
    payload = {
        "harnesses": [
            {
                "name": f"harness-{i}",
                "description": "x" * 1024,
                "capabilities": {
                    "schema_version": 1,
                    "launch_modes": ["structured"],
                    "attachments": [
                        "application/" + "x" * 100 + str(j) for j in range(7)
                    ],
                },
            }
            for i in range(32)
        ]
    }
    assert len(json.dumps(payload)) <= MAX_ENVELOPE_BYTES
    with pytest.raises(InvalidCapabilities):
        validate_capabilities(payload, "test-host")
