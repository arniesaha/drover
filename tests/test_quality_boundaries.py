"""Structural quality gates (#1024 Slice B): dependency boundary + work contract.

Boundary seeds use throwaway source trees and a seeded copy of the real tree.
Contract tests drive the real HTTP handler -> runtime.dispatch -> WorkService
path. The PG-free tier uses a store tripwire, so every case also proves that
rejection happens before any durable effect. The PG tier needs an explicit,
owned ``DROVER_TEST_POSTGRES_DSN`` and never the /tmp auto-cluster.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from test_work_queue_execution import Harness, Provider, launch
from test_work_queue_execution import service as service  # noqa: F401  (PG fixture)
from test_work_queue_github_acceptance import ready

from drover.server.web.app import start_metrics_server
from drover.server.web.auth import AuthSettings
from drover.server.web.credentials import CredentialStore
from drover.server.work_queue.config import WorkQueueConfig
from drover.server.work_queue.github import ProviderUnavailable
from drover.server.work_queue.runtime import dispatch
from drover.server.work_queue.service import WorkService

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"quality_{name}", ROOT / "scripts" / "quality" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their own module
    spec.loader.exec_module(module)
    return module


boundaries = _load("boundaries")
contract = _load("work_contract")

# --- dependency boundary -------------------------------------------------------

SEED = boundaries.Boundary(
    name="seed",
    doc="test",
    roots=("drover.server.work_queue",),
    forbidden=("drover.server.lake", "anthropic"),
)
BASE = {
    "drover/__init__.py": "",
    "drover/server/__init__.py": "",
    "drover/server/work_queue/__init__.py": "",
    "drover/server/lake/__init__.py": "",
    "drover/server/lake/serving.py": "",
    "drover/server/lakehouse.py": "",
    "drover/server/shared.py": "",
    "drover/server/helper/__init__.py": "",
    "drover/server/helper/sub.py": "",
}


def _tree(tmp_path, **files):
    src = tmp_path / "src"
    for rel, text in {**BASE, **files}.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    return src


def _guard(capsys, src, *flags, boundary=SEED):
    code = boundaries.main(list(flags), src=src, boundaries=(boundary,))
    return code, json.loads(capsys.readouterr().out)


ROOT_FILE = "drover/server/work_queue/x.py"


@pytest.mark.parametrize(
    "files,forbidden",
    [
        ({ROOT_FILE: "import drover.server.lake.serving\n"}, "drover.server.lake"),
        ({ROOT_FILE: "from drover.server.lake import serving\n"}, "drover.server.lake"),
        ({ROOT_FILE: "from ..lake import serving\n"}, "drover.server.lake"),
        ({ROOT_FILE: "from .. import lake\n"}, "drover.server.lake"),
        (
            {ROOT_FILE: "def f():\n    from drover.server.lake import serving\n"},
            "drover.server.lake",
        ),
        (
            {
                ROOT_FILE: "import importlib\nimportlib.import_module('drover.server.lake')\n"
            },
            "drover.server.lake",
        ),
        (
            {
                ROOT_FILE: "from drover.server import shared\n",
                "drover/server/shared.py": "import drover.server.lake\n",
            },
            "drover.server.lake",
        ),
        (
            {
                ROOT_FILE: "import drover.server.helper.sub\n",
                "drover/server/helper/__init__.py": "from ..lake import serving\n",
            },
            "drover.server.lake",
        ),
        ({ROOT_FILE: "def f():\n    import anthropic\n"}, "anthropic"),
    ],
    ids=[
        "absolute",
        "from_absolute",
        "relative",
        "relative_package",
        "root_lazy",
        "importlib_literal",
        "transitive_eager",
        "parent_package_init",
        "third_party_lazy",
    ],
)
def test_seeded_forbidden_dependency_fails_with_its_path(
    capsys, tmp_path, files, forbidden
):
    code, report = _guard(capsys, _tree(tmp_path, **files))
    assert code == 1 and report["classification"] == "violation"
    (hit,) = report["boundaries"]["seed"]
    assert hit["forbidden"] == forbidden
    assert hit["path"][0].startswith("drover.server.work_queue.x:")


@pytest.mark.parametrize(
    "files",
    [
        {ROOT_FILE: "import drover.server.lakehouse\n"},  # prefix is not a match
        {
            ROOT_FILE: "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    import drover.server.lake\n"
        },
        # Documented limitation: a lazy import inside a *shared* module needs a
        # call graph to attribute to the root; it is out of scope, not proven.
        {
            ROOT_FILE: "from drover.server import shared\n",
            "drover/server/shared.py": "def f():\n    import drover.server.lake\n",
        },
    ],
    ids=["similar_name", "type_checking_only", "shared_module_lazy_out_of_scope"],
)
def test_healthy_or_out_of_scope_sources_pass(capsys, tmp_path, files):
    assert _guard(capsys, _tree(tmp_path, **files))[0] == 0


def test_eager_only_mode_ignores_root_lazy_imports(capsys, tmp_path):
    src = _tree(tmp_path, **{ROOT_FILE: "def f():\n    import drover.server.lake\n"})
    assert _guard(capsys, src)[0] == 1
    assert _guard(capsys, src, "--eager-only")[0] == 0


def test_ratchet_fails_when_a_known_edge_is_gone(capsys, tmp_path):
    stale = boundaries.Boundary(
        "seed",
        "test",
        SEED.roots,
        SEED.forbidden,
        known=(("drover.server.work_queue.x", "drover.server.lake"),),
    )
    code, report = _guard(capsys, _tree(tmp_path), "--ratchet", boundary=stale)
    assert code == 1
    assert "stale_known_edge" in report["boundaries"]["seed"][0]


def test_unmatched_root_or_bad_source_is_harness_failure(capsys, tmp_path):
    lost = boundaries.Boundary("seed", "test", ("drover.nowhere",), SEED.forbidden)
    assert _guard(capsys, _tree(tmp_path), boundary=lost)[0] == 4
    assert _guard(capsys, _tree(tmp_path, **{ROOT_FILE: "def (:\n"}))[0] == 4


def test_real_source_characterized_violation_and_ratchet(capsys):
    code, report = _guard(capsys, ROOT / "src", boundary=boundaries.BOUNDARIES[0])
    hits = report["boundaries"]["work-coordinator-control"]
    assert code == 1 and {h["forbidden"] for h in hits} == {"drover.server.mcp.tools"}
    assert hits[0]["path"][0].startswith("drover.server.work_queue.")
    assert "(lazy)" in hits[0]["path"][0]
    for flags in (["--ratchet"], ["--eager-only"]):
        assert (
            _guard(capsys, ROOT / "src", *flags, boundary=boundaries.BOUNDARIES[0])[0]
            == 0
        ), flags


def test_seeded_real_tree_import_of_lake_fails_the_ratchet(capsys, tmp_path):
    src = tmp_path / "src"
    shutil.copytree(ROOT / "src" / "drover", src / "drover")
    target = src / "drover" / "server" / "work_queue" / "provider.py"
    target.write_text(target.read_text() + "\nimport drover.server.lake.serving\n")
    code, report = _guard(capsys, src, "--ratchet", boundary=boundaries.BOUNDARIES[0])
    hits = report["boundaries"]["work-coordinator-control"]
    assert code == 1 and [h["forbidden"] for h in hits] == ["drover.server.lake"]
    assert hits[0]["path"][0].startswith("drover.server.work_queue.provider:")


# --- typed response contract (unit negatives) ----------------------------------


@pytest.mark.parametrize(
    "status,body,action",
    [
        (200, {"outcome": 1}, "verified"),  # wrong type, no coercion
        (200, {"outcome": "verified", "debug": "x"}, "verified"),  # unknown field
        (200, {"outcome": "verified"}, "failed"),  # wrong semantic outcome
        (True, {"outcome": "verified"}, "verified"),  # bool is not a status
        (400, {"error": "invalid work request", "reason": "leak"}, "verified"),
        (409, {"error": "work operation denied"}, "verified"),  # reason required
        (403, {"error": "invalid work request", "reason": "x"}, "verified"),
        (418, {"error": "teapot"}, "verified"),
        (200, {"outcome": "verified"}, "merge"),
    ],
)
def test_contract_rejects_bad_shapes_and_semantics(status, body, action):
    with pytest.raises(contract.ContractViolation):
        contract.validate_update_response(status, body, action=action)


# --- real HTTP boundary, PG-free tier ------------------------------------------


class Tripwire:
    """Any store access means a rejection reached durable state."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"store reached: {name}")


@pytest.fixture
def http(tmp_path):
    credentials = CredentialStore(tmp_path / "credentials.json")
    device, token = credentials.issue(scope="device", label="synthetic device")
    _, restricted = credentials.issue(scope="preflight", label="synthetic restricted")
    cfg = WorkQueueConfig(
        enabled=True,
        shadow_mode=False,
        workspace_id="test-workspace",
        principal_roles={"credential:" + device.id: "nix"},
        goals=[
            {
                "goal_id": "goal",
                "repo": "example/project",
                "goal_label": "goal:test",
                "credential": {
                    "token_env": "TEST_TOKEN",
                    "capabilities": ["issues_read", "issues_write"],
                },
            }
        ],
    )
    store, provider = Tripwire(), Provider()
    holder = SimpleNamespace(
        work_control=WorkService(cfg, store, {"goal": provider}, Harness())
    )
    auth = AuthSettings(
        enabled=True, api_token="synthetic-legacy", credentials=credentials
    )
    server = start_metrics_server(host="127.0.0.1", port=0, collector=holder, auth=auth)
    url = f"http://127.0.0.1:{server.server_port}/work/update"
    yield SimpleNamespace(
        url=url,
        token=token,
        restricted=restricted,
        store=store,
        provider=provider,
        holder=holder,
    )
    server.shutdown()
    server.server_close()


VALID = {"goal_id": "goal", "number": 1, "run_key": "a" * 64, "action": "verified"}


def _post(h, body, token="device"):
    token = {"device": h.token, "restricted": h.restricted, None: None}[token]
    headers = {"Authorization": "Bearer " + token} if token else {}
    response = requests.post(h.url, json=body, headers=headers, timeout=5)
    return response.status_code, response.json(), response.headers


@pytest.mark.parametrize(
    "case,body,token,status,needle",
    [
        ("no_credential", VALID, None, 401, None),
        ("restricted_scope", VALID, "restricted", 401, None),
        ("non_object", [VALID], "device", 400, None),
        ("unknown_field", {**VALID, "actor_principal": "nix"}, "device", 400, None),
        (
            "missing_run_key",
            {k: v for k, v in VALID.items() if k != "run_key"},
            "device",
            400,
            None,
        ),
        ("number_wrong_type", {**VALID, "number": "1"}, "device", 400, None),
        ("number_bool", {**VALID, "number": True}, "device", 400, None),
        ("goal_outside", {**VALID, "goal_id": "other"}, "device", 403, "goal outside"),
    ],
)
def test_rejections_before_durable_state(http, case, body, token, status, needle):
    got, payload, _ = _post(http, body, token)
    parsed = contract.validate_update_response(got, payload, action="verified")
    assert got == status, (case, payload)
    assert needle is None or needle in parsed.reason
    assert http.store.touched == [] and http.provider.closed == 0


def test_item_outside_goal_shadow_and_unavailable_paths(http, monkeypatch):
    http.provider.data["labels"] = [{"name": "status:queued"}]
    got, payload, headers = _post(http, VALID)
    error = contract.validate_update_response(got, payload, action="verified")
    assert (got, error.reason) == (403, "item outside goal")
    assert headers["Cache-Control"] == "no-store"

    def unavailable(ref):
        raise ProviderUnavailable("synthetic outage")

    monkeypatch.setattr(http.provider, "get_item", unavailable)
    got, payload, _ = _post(http, VALID)
    error = contract.validate_update_response(got, payload, action="verified")
    assert (got, error.reason) == (409, "item provider unavailable")
    service = http.holder.work_control
    service.config = service.config.model_copy(update={"shadow_mode": True})
    got, payload, _ = _post(http, VALID)
    assert contract.validate_update_response(
        got, payload, action="verified"
    ).reason == ("shadow mode is read only")
    http.holder.work_control = None
    got, payload, _ = _post(http, VALID)
    contract.validate_update_response(got, payload, action="verified")
    assert got == 503
    assert http.store.touched == [] and http.provider.closed == 0


@pytest.mark.parametrize(
    "returned,action,needle",
    [
        ({"outcome": 1}, "verified", "shape"),
        ({"outcome": "verified"}, "failed", "valid"),
    ],
)
def test_seeded_bad_service_response_is_rejected_at_http_boundary(
    http, returned, action, needle
):
    http.holder.work_control.update = lambda **kwargs: returned
    got, payload, _ = _post(http, {**VALID, "action": action})
    assert got == 200  # the server emits it; only the contract catches drift
    with pytest.raises(contract.ContractViolation, match=needle):
        contract.validate_update_response(got, payload, action=action)


# --- real service with control PostgreSQL --------------------------------------

needs_pg = pytest.mark.skipif(
    not os.environ.get("DROVER_TEST_POSTGRES_DSN"),
    reason="needs an owned DROVER_TEST_POSTGRES_DSN (never the /tmp auto-cluster)",
)


@pytest.fixture
def control(service, tmp_path):  # noqa: F811
    credentials = CredentialStore(tmp_path / "credentials.json")
    device, token = credentials.issue(scope="device", label="synthetic device")
    service.config = service.config.model_copy(
        update={"principal_roles": {"credential:" + device.id: "nix"}}
    )
    auth = AuthSettings(enabled=True, api_token="", credentials=credentials)
    headers = {"Authorization": "Bearer " + token}

    def call(action, run_key, **fields):
        args = dict(goal_id="goal", number=1, run_key=run_key, action=action, **fields)
        status, body = dispatch(service, auth, headers, "update", args)
        return status, contract.validate_update_response(status, body, action=action)

    return service, call


@needs_pg
def test_pg_github_pilot_success_and_evidence_negatives(control, monkeypatch):
    service, call = control
    client, verify = ready(service, monkeypatch)
    run_key = verify["run_key"]
    # Wrong-type, missing and caller-boolean-only evidence: 400, nothing closes.
    for fields in (
        {"evidence_key": 7},
        {},
        {"behavioral_passed": True, "checks_rerun": True, "evidence_key": "check:ok"},
    ):
        assert call("verified", run_key, **fields)[0] == 400, fields
    client.overrides["/repos/example/project/pulls/9"] = {
        "head": {"sha": "a" * 40},
        "base": {"ref": "main", "repo": {"full_name": "other/project"}},
    }
    assert call("verified", run_key, evidence_key="check-run:7:review:8")[0] == 400
    client.overrides.clear()
    evidence, client.evidence = client.evidence, None
    status, error = call("verified", run_key, evidence_key="check-run:7:review:8")
    # A nonexistent evidence run is a provider 404: ProviderUnavailable -> 409.
    assert (status, error.reason) == (409, "item provider unavailable")
    assert client.closed == 0
    client.evidence = evidence
    status, result = call("verified", run_key, evidence_key="check-run:7:review:8")
    assert (status, result.outcome, client.closed) == (200, "verified", 1)


@needs_pg
def test_pg_non_pilot_caller_flag_path_stays_distinct(control):
    service, call = control
    work = launch(service)
    service.harness.sessions[work["run_key"]].status = "completed"
    status, link = call("complete", work["run_key"], estimated_cost=5)
    assert status == 200 and isinstance(link, contract.VerificationLink)
    service.harness.sessions[link.run_key].status = "completed"
    flags = {"checks_rerun": True, "evidence_key": "check:pass"}
    assert call("verified", link.run_key, behavioral_passed="true", **flags)[0] == 400
    assert service.providers["goal"].closed == 0
    status, result = call("verified", link.run_key, behavioral_passed=True, **flags)
    assert (status, result.outcome, service.providers["goal"].closed) == (
        200,
        "verified",
        1,
    )
