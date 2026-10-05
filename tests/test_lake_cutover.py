"""S5 cutover gate, preflight and switch/rollback scripts (no PostgreSQL, no lake).

Every external effect is injected: connections, subprocesses, launchd and HTTP.
The end-to-end gate runs against scratch clusters via
``scripts/lake_gate_rehearsal.py``.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from drover.config import load_config
from drover.server.__main__ import main
from drover.server.lake import cutover
from drover.server.lake.cutover import (
    Check,
    CutoverError,
    CutoverPlan,
    Launchd,
    ServiceView,
    execute,
    exit_code,
    flip_config,
    memory_limit_bytes,
    replace_table,
    rollback_actions,
    run_check,
    run_preflight,
    switch_actions,
    take_backup,
    verdict,
    verify_hub,
)
from drover.server.lake.gate import GateRun, PeakRss, gate_config_text

SHA = "a" * 64
GIB = 1024**3


# --- fakes -------------------------------------------------------------------


class Rows:
    def __init__(self, rows):
        self.rows = list(rows)
        self.rowcount = len(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    """Answers by the first matching substring, records every statement."""

    def __init__(self, answers):
        self.answers = answers
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else query.as_string(None)
        self.statements.append(text)
        for needle, rows in self.answers.items():
            if needle in text:
                if isinstance(rows, Exception):
                    raise rows
                return Rows(rows)
        return Rows([])


def role_answers(*, user, insert, database="drover_lake_blue", create=False):
    return {
        "has_table_privilege": [(True, insert, create)],
        "SELECT current_user, current_database()": [(user, database)],
        "tablename = 'ducklake_snapshot'": [("public",)],
        '"ducklake_snapshot"': [(3,)],
    }


def control_answers(*, sessions=12, pending=0, catalogs=()):
    return {
        "TRANSACTION READ ONLY": [],
        "max(version)": [(12,)],
        "command <> 'collector'": [(sessions,)],
        "to_regclass": [(True,)],
        "acknowledged_at IS NULL": [(pending, list(catalogs))],
    }


@pytest.fixture
def plan(tmp_path):
    (tmp_path / "lakes").mkdir()
    return CutoverPlan.from_name("blue", lake_root=tmp_path / "lakes")


def write_service(tmp_path, *, env=None, config_extra="", analytics=""):
    """A plist + config shaped like the production hub, all under tmp_path."""
    config = tmp_path / "home" / ".drover" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "[paths]\n"
        f'parquet_dir = "{tmp_path / "parquet"}"\n'
        f'duckdb_path = "{tmp_path / "drover.duckdb"}"\n'
        "[control_store]\n"
        'backend = "postgres"\n'
        'dsn_env = "DROVER_CONTROL_DSN"\n'
        "[server]\n"
        "metrics_http_port = 7080\n"
        "[update]\n"
        'pinned_version = "0.6.0"\n' + config_extra + analytics
    )
    plist = tmp_path / "com.drover.server.plist"
    environment = {
        "HOME": str(tmp_path / "home"),
        "DROVER_CONTROL_DSN": "host=/tmp port=5 dbname=prod user=hub",
        "DROVER_LAKE_BLUE_READER_DSN": "host=/tmp dbname=drover_lake_blue user=r",
        "DROVER_LAKE_BLUE_EXPORTER_DSN": "host=/tmp dbname=drover_lake_blue user=e",
        "DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT": "4GB",
    }
    environment.update(env or {})
    with plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "com.drover.server",
                "ProgramArguments": [
                    "/runtime/current/bin/drover-server",
                    "--config",
                    str(config),
                    "run",
                ],
                "EnvironmentVariables": {
                    k: v for k, v in environment.items() if v is not None
                },
            },
            stream,
        )
    return ServiceView.load(plist)


def version_run(stdout="drover-server, version 0.6.0", code=0):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    return run


def connections(service, **overrides):
    cons = {
        service.env.get("DROVER_LAKE_BLUE_READER_DSN"): FakeConnection(
            role_answers(user="drover_lake_blue_reader", insert=False)
        ),
        service.env.get("DROVER_LAKE_BLUE_EXPORTER_DSN"): FakeConnection(
            role_answers(user="drover_lake_blue_exporter", insert=True)
        ),
        service.env.get("DROVER_CONTROL_DSN"): FakeConnection(control_answers()),
    }
    cons.update(overrides)
    return cons


def preflight(plan, service, cons, *, stage="gate", run=None, monkeypatch=None, **kw):
    environ = kw.pop(
        "environ",
        {"DROVER_LAKE_EXTENSION_DIR": "/ext", "DROVER_LAKE_ENGINE_SHA256": SHA},
    )
    return {
        c.id: c
        for c in run_preflight(
            plan,
            service,
            stage=stage,
            release="v0.6.0",
            min_free_bytes=kw.pop("min_free_bytes", 1),
            min_ram_bytes=kw.pop("min_ram_bytes", 1),
            connect=lambda dsn: cons[dsn],
            run=run or version_run(),
            ram=kw.pop("ram", lambda: 64 * GIB),
            environ=environ,
            **kw,
        )
    }


@pytest.fixture(autouse=True)
def pinned_runtime(monkeypatch):
    """Extension/engine verification is covered by the lake runtime tests."""
    from drover.server.lake import runtime

    monkeypatch.setattr(runtime, "verify_runtime", lambda spec: {})


# --- names, budgets, verdicts ------------------------------------------------


def test_every_name_derives_from_one_lake_variable(tmp_path):
    plan = CutoverPlan.from_name("green", lake_root=tmp_path)
    assert plan.data_root == tmp_path.resolve() / "green"
    assert plan.catalog_database == "drover_lake_green"
    assert plan.reader_dsn_env == "DROVER_LAKE_GREEN_READER_DSN"
    assert plan.exporter_dsn_env == "DROVER_LAKE_GREEN_EXPORTER_DSN"
    assert plan.admin_dsn_env == "DROVER_LAKE_GREEN_ADMIN_DSN"
    assert plan.gate_control_database == "drover_gate_green_control"
    assert plan.gate_catalog_database == "drover_gate_green_lake"
    assert plan.verdict_path == tmp_path.resolve() / ".gate" / "green.verdict.json"
    assert all("green" in value.lower() for value in plan.describe().values())


@pytest.mark.parametrize("name", ["", "Blue", "1x", "a-b", "x" * 25, "drop;"])
def test_lake_name_is_validated(tmp_path, name):
    with pytest.raises(CutoverError, match="cutover_lake_name_invalid"):
        CutoverPlan.from_name(name, lake_root=tmp_path)


def test_memory_limit_uses_duckdb_units():
    assert memory_limit_bytes("4GB") == 4_000_000_000
    assert memory_limit_bytes("4GiB") == 4 * GIB
    assert memory_limit_bytes(" 512 MiB ") == 512 * 1024**2
    for bad in ("", "4", "4XB", "0GB", "-1GB", "iB"):
        with pytest.raises(CutoverError):
            memory_limit_bytes(bad)


def test_verdict_and_exit_codes(plan):
    passing = verdict("gate", plan, [Check("a", True), Check("b", True)])
    assert passing["passed"] and passing["failed"] == [] and exit_code(passing) == 0
    failing = verdict("gate", plan, [Check("a", True), Check("b", False)])
    assert not failing["passed"] and failing["failed"] == ["b"]
    assert exit_code(failing) == 1
    errored = verdict("gate", plan, [Check("a", True)], error="gate_hub_exited")
    assert not errored["passed"] and exit_code(errored) == 2
    empty = verdict("gate", plan, [])
    assert not empty["passed"] and exit_code(empty) == 1


def test_run_check_turns_exceptions_into_sanitized_failures():
    dsn = "host=db password=hunter2 dbname=x"

    def boom():
        raise RuntimeError(f"connection failed: {dsn}")

    check = run_check("probe", boom, dsn)
    assert not check.passed
    assert "hunter2" not in json.dumps(check.as_dict())


def test_libpq_env_keeps_dsn_off_argv_and_can_force_read_only():
    env = cutover.libpq_env(
        "host=/tmp port=6 dbname=prod user=u password=pw",
        read_only=True,
        base={"PATH": "/bin", "PGHOST": "elsewhere"},
    )
    assert env["PGHOST"] == "/tmp" and env["PGDATABASE"] == "prod"
    assert env["PGPASSWORD"] == "pw" and env["PATH"] == "/bin"
    assert "default_transaction_read_only=on" in env["PGOPTIONS"]


# --- preflight -----------------------------------------------------------------


def test_service_view_reads_launchd_argv_env_and_config(tmp_path):
    service = write_service(tmp_path)
    assert service.label == "com.drover.server"
    assert service.config_path == tmp_path / "home" / ".drover" / "config.toml"
    assert service.version_argv() == ["/runtime/current/bin/drover-server", "--version"]
    assert service.hub_url(service.config()) == "http://127.0.0.1:7080"


def test_preflight_all_green_in_the_service_view(tmp_path, plan):
    service = write_service(tmp_path)
    cons = connections(service)
    checks = preflight(plan, service, cons)
    assert all(c.passed for c in checks.values()), {
        k: c.evidence for k, c in checks.items() if not c.passed
    }
    assert set(checks) == {
        "service_config",
        "service_env",
        "catalog_role_reader",
        "catalog_role_exporter",
        "control_store",
        "lake_root_disk_free",
        "ram",
        "installed_version",
        "updater_state",
        "lake_runtime_pins",
        "target_paths",
    }
    # The control store was only read, inside a read-only session.
    statements = cons[service.env["DROVER_CONTROL_DSN"]].statements
    assert statements[0].endswith("TRANSACTION READ ONLY")
    assert not any(s.split()[0].upper() in {"INSERT", "UPDATE"} for s in statements)
    assert checks["service_env"].evidence["analytical_budget_bytes"] == 4_000_000_000


def test_preflight_fails_a_reader_that_can_write(tmp_path, plan):
    service = write_service(tmp_path)
    reader = service.env["DROVER_LAKE_BLUE_READER_DSN"]
    cons = connections(
        service,
        **{reader: FakeConnection(role_answers(user="hub", insert=True))},
    )
    check = preflight(plan, service, cons)["catalog_role_reader"]
    assert not check.passed and check.evidence["insert"] is True


def test_preflight_fails_an_exporter_on_the_wrong_catalog(tmp_path, plan):
    service = write_service(
        tmp_path,
        env={"DROVER_LAKE_BLUE_EXPORTER_DSN": "host=/tmp dbname=other user=e"},
    )
    exporter = service.env["DROVER_LAKE_BLUE_EXPORTER_DSN"]
    cons = connections(
        service,
        **{
            exporter: FakeConnection(
                role_answers(user="e", insert=True, database="other")
            )
        },
    )
    checks = preflight(plan, service, cons)
    assert not checks["catalog_role_exporter"].passed
    assert checks["service_env"].evidence["wrong_catalog_database"] == [
        "DROVER_LAKE_BLUE_EXPORTER_DSN"
    ]


def test_preflight_fails_when_a_dsn_is_missing_from_the_plist(tmp_path, plan):
    service = write_service(tmp_path, env={"DROVER_LAKE_BLUE_READER_DSN": None})
    checks = preflight(plan, service, connections(service))
    assert not checks["service_env"].passed
    assert checks["service_env"].evidence["missing"] == ["DROVER_LAKE_BLUE_READER_DSN"]
    assert not checks["catalog_role_reader"].passed


def test_preflight_fails_a_role_that_cannot_connect(tmp_path, plan):
    service = write_service(tmp_path)
    reader = service.env["DROVER_LAKE_BLUE_READER_DSN"]
    refused = FakeConnection(
        {"current_user": OSError("password authentication failed for r")}
    )
    check = preflight(plan, service, connections(service, **{reader: refused}))[
        "catalog_role_reader"
    ]
    assert not check.passed and "authentication failed" in check.evidence["error"]


@pytest.mark.parametrize(
    "answers, field",
    [
        (control_answers(sessions=10_001), "identity_sessions"),
        (
            control_answers(pending=2, catalogs=["old-catalog"]),
            "unacknowledged_export_batches",
        ),
    ],
)
def test_preflight_blocks_identity_limit_and_stale_export_batches(
    tmp_path, plan, answers, field
):
    service = write_service(tmp_path)
    control = service.env["DROVER_CONTROL_DSN"]
    check = preflight(
        plan, service, connections(service, **{control: FakeConnection(answers)})
    )["control_store"]
    assert not check.passed and check.evidence[field]


def test_preflight_disk_and_ram_minimums(tmp_path, plan):
    service = write_service(tmp_path)
    checks = preflight(
        plan,
        service,
        connections(service),
        min_free_bytes=10**18,
        min_ram_bytes=128 * GIB,
        ram=lambda: 32 * GIB,
    )
    assert not checks["lake_root_disk_free"].passed
    assert not checks["ram"].passed


def test_preflight_version_and_runtime_current(tmp_path, plan):
    service = write_service(tmp_path)
    checks = preflight(
        plan,
        service,
        connections(service),
        run=version_run("drover-server, version 0.5.7"),
    )
    assert not checks["installed_version"].passed
    runtime = tmp_path / "home" / ".drover" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "current").symlink_to("0.5.7")
    checks = preflight(plan, service, connections(service))
    assert not checks["installed_version"].passed
    assert checks["installed_version"].evidence["runtime_current"] == "0.5.7"


def test_preflight_updater_must_be_pinned_and_marker_free(tmp_path, plan):
    service = write_service(tmp_path, config_extra='\n[update]\npinned_version = ""\n')
    assert not preflight(plan, service, connections(service))["updater_state"].passed
    service = write_service(tmp_path)
    runtime = tmp_path / "home" / ".drover" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "pending_verification.json").write_text("{}")
    check = preflight(plan, service, connections(service))["updater_state"]
    assert not check.passed and check.evidence["pending_verification_marker"]


def test_preflight_requires_lake_runtime_pins(tmp_path, plan):
    service = write_service(tmp_path)
    check = preflight(plan, service, connections(service), environ={})[
        "lake_runtime_pins"
    ]
    assert not check.passed


def test_preflight_gate_stage_requires_an_empty_gate_dir(tmp_path, plan):
    service = write_service(tmp_path)
    plan.gate_dir.mkdir(parents=True)
    (plan.gate_dir / "left-over").write_text("x")
    assert not preflight(plan, service, connections(service))["target_paths"].passed


def _passing_gate_verdict(plan, *, age=timedelta(0)):
    document = verdict("gate", plan, [Check("A1", True)])
    document["generated_at"] = (datetime.now(timezone.utc) - age).isoformat()
    plan.verdict_path.parent.mkdir(parents=True, exist_ok=True)
    plan.verdict_path.write_text(json.dumps(document))


def test_preflight_switch_stage_requires_proof_and_fresh_gate(tmp_path, plan):
    service = write_service(tmp_path)
    check = preflight(plan, service, connections(service), stage="switch")[
        "target_paths"
    ]
    assert not check.passed
    assert check.evidence["serving_proof"] is False
    assert check.evidence["gate_verdict_passed_and_fresh"] is False
    plan.proof_path.parent.mkdir(parents=True)
    plan.proof_path.write_text("{}")
    _passing_gate_verdict(plan, age=timedelta(hours=30))
    assert not preflight(plan, service, connections(service), stage="switch")[
        "target_paths"
    ].passed
    _passing_gate_verdict(plan)
    assert preflight(plan, service, connections(service), stage="switch")[
        "target_paths"
    ].passed


def test_preflight_cli_reports_an_unreadable_plist_as_exit_2(tmp_path):
    result = CliRunner().invoke(
        main,
        [
            "gate",
            "--lake",
            "blue",
            "--lake-root",
            str(tmp_path),
            "--preflight",
            "--release",
            "v0.6.0",
            "--plist",
            str(tmp_path / "missing.plist"),
        ],
    )
    assert result.exit_code == 2, result.output
    document = json.loads(result.output)
    assert document["kind"] == "preflight" and "plist unreadable" in document["error"]


# --- backup, flip, procedures ----------------------------------------------


def fake_pg_tools(calls, *, listing="1; TABLE drover_control harness_sessions"):
    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("env")))
        if Path(argv[0]).name == "pg_dump":
            target = next(a.split("=", 1)[1] for a in argv if a.startswith("--file="))
            Path(target).write_bytes(b"PGDMP-custom")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, listing, "")

    return run


def test_backup_dumps_read_only_copies_and_verifies(tmp_path, plan, monkeypatch):
    monkeypatch.setattr(cutover, "pg_tool", lambda name, pg_bin=None: f"/pg/{name}")
    service = write_service(tmp_path)
    calls = []
    receipt = take_backup(
        plan, service, service.config(), run=fake_pg_tools(calls), stamp="S1"
    )
    target = plan.backup_dir / "S1"
    assert receipt["verified"] and set(receipt["files"]) == {
        "control.dump",
        "config.toml",
        "com.drover.server.plist",
    }
    dump_argv, dump_env = calls[0]
    assert "--schema=drover_control" in dump_argv
    assert not any("prod" in a or "dbname" in a for a in dump_argv)
    assert dump_env["PGDATABASE"] == "prod"
    assert "default_transaction_read_only=on" in dump_env["PGOPTIONS"]
    assert (target / "config.toml").read_text() == service.config_path.read_text()
    (target / "config.toml").write_text("tampered")
    with pytest.raises(CutoverError, match="backup_file_changed"):
        cutover.verify_backup(target, run=fake_pg_tools([]))
    with pytest.raises(CutoverError, match="backup_dump_unreadable"):
        (target / "config.toml").write_text(service.config_path.read_text())
        cutover.verify_backup(target, run=fake_pg_tools([], listing=""))


def test_replace_table_rewrites_only_the_named_table():
    text = (
        "[paths]\nparquet_dir = '/p'\n\n[analytics]\nbackend = 'legacy'\n"
        "[[hosts]]\nname = 'a'\n[analytics.extra]\nx = 1\n[server]\nport = 1\n"
    )
    out = replace_table(text, "analytics", ['backend = "ducklake"'])
    assert out.count("[analytics]") == 1 and "backend = 'legacy'" not in out
    assert "[[hosts]]" in out and "[analytics.extra]" in out and "[server]" in out
    assert out.rstrip().endswith('[analytics]\nbackend = "ducklake"')


def test_flip_config_is_atomic_validated_and_idempotent(tmp_path, plan):
    service = write_service(tmp_path)
    plan.proof_path.parent.mkdir(parents=True)
    plan.proof_path.write_text('{"proof": 1}')
    selection = cutover.ducklake_selection(
        plan, extension_dir="/ext", engine_sha256=SHA, epoch="blue-1"
    )
    assert flip_config(service.config_path, selection) is True
    cfg = load_config(service.config_path)
    assert cfg.analytics.backend == "ducklake"
    assert cfg.analytics.catalog_dsn_env == "DROVER_LAKE_BLUE_READER_DSN"
    assert cfg.analytics.exporter_dsn_env == "DROVER_LAKE_BLUE_EXPORTER_DSN"
    assert cfg.metrics_http_port == 7080  # other tables untouched
    assert flip_config(service.config_path, selection) is False
    assert flip_config(service.config_path, cutover.LEGACY_SELECTION) is True
    assert load_config(service.config_path).analytics.backend == "legacy"
    with pytest.raises(ValueError):
        flip_config(service.config_path, {"backend": "ducklake"})
    assert load_config(service.config_path).analytics.backend == "legacy"
    assert not list(service.config_path.parent.glob("*.cutover-new"))


class RecordingLaunchd(Launchd):
    def __init__(self, service, loaded=True):
        self.calls = []
        self._loaded = loaded

        def run(argv, **kwargs):
            self.calls.append(argv)
            if argv[1] == "bootout":
                self._loaded = False
            if argv[1] == "bootstrap":
                self._loaded = True
            code = 0 if argv[1] != "print" or self._loaded else 113
            return subprocess.CompletedProcess(argv, code, "", "")

        super().__init__(service, run=run)


def test_switch_dry_run_prints_ordered_actions_and_changes_nothing(tmp_path, plan):
    service = write_service(tmp_path)
    before = service.config_path.read_text()
    launchd = RecordingLaunchd(service)
    lines = []
    actions = switch_actions(
        plan, service, legacy_root=tmp_path / "parquet", launchd=launchd, environ={}
    )
    execute(actions, dry_run=True, echo=lines.append)
    assert [a.step for a in actions] == [
        "require_gate",
        "backup",
        "stop_service",
        "fenced_delta_import",
        "flip_backend",
        "start_service",
        "verify",
    ]
    text = "\n".join(lines)
    assert all("(dry-run)" in line for line in lines)
    assert "launchctl bootout gui/" in text and "launchctl bootstrap gui/" in text
    assert "--catalog-dsn-env DROVER_LAKE_BLUE_ADMIN_DSN" in text
    assert "catalog_dsn_env=DROVER_LAKE_BLUE_READER_DSN" in text
    assert "http://127.0.0.1:7080/readyz" in text
    assert launchd.calls == []
    assert service.config_path.read_text() == before
    assert not plan.backup_dir.exists()


def test_switch_refuses_without_a_passing_gate_verdict(tmp_path, plan):
    service = write_service(tmp_path)
    launchd = RecordingLaunchd(service)
    actions = switch_actions(
        plan, service, legacy_root=tmp_path, launchd=launchd, environ={}
    )
    with pytest.raises(CutoverError, match="gate_verdict_missing_or_stale"):
        execute(actions, dry_run=False, echo=lambda _: None)
    assert launchd.calls == [] and not plan.backup_dir.exists()


def test_switch_runs_backup_first_and_stops_at_the_failing_step(
    tmp_path, plan, monkeypatch
):
    monkeypatch.setattr(cutover, "pg_tool", lambda name, pg_bin=None: f"/pg/{name}")
    calls = []
    real_backup = cutover.take_backup
    monkeypatch.setattr(
        cutover,
        "take_backup",
        lambda *a, **kw: real_backup(*a, **{**kw, "run": fake_pg_tools(calls)}),
    )
    service = write_service(tmp_path)
    _passing_gate_verdict(plan)
    launchd = RecordingLaunchd(service)
    actions = switch_actions(
        plan, service, legacy_root=tmp_path, launchd=launchd, environ={}
    )
    with pytest.raises(CutoverError, match="fenced_delta_import_failed"):
        execute(actions, dry_run=False, echo=lambda _: None)
    assert calls and Path(calls[0][0][0]).name == "pg_dump"
    assert any(argv[1] == "bootout" for argv in launchd.calls)
    assert load_config(service.config_path).analytics.backend == "legacy"


def test_rollback_dry_run_uses_the_recorded_switch_time(tmp_path, plan):
    service = write_service(tmp_path)
    with pytest.raises(CutoverError, match="rollback_since_unknown"):
        rollback_actions(plan, service, launchd=RecordingLaunchd(service))
    cutover._record_state(plan, switch_epoch=1759600000.5, epoch="blue-1")
    cutover._record_state(plan, switch_epoch=9e9)  # re-runs keep the first time
    launchd = RecordingLaunchd(service)
    lines = []
    actions = rollback_actions(plan, service, launchd=launchd)
    execute(actions, dry_run=True, echo=lines.append)
    assert [a.step for a in actions] == [
        "backup",
        "stop_service",
        "flip_backend",
        "outbox_replay",
        "start_service",
        "verify",
    ]
    text = "\n".join(lines)
    assert "outbox replay --sink legacy --since 1759600000.5" in text
    assert "backend=legacy" in text
    assert launchd.calls == []


def test_rollback_flips_back_and_replays_with_the_service_env(
    tmp_path, plan, monkeypatch
):
    monkeypatch.setattr(cutover, "take_backup", lambda *a, **kw: {"verified": True})
    monkeypatch.setattr(cutover, "verify_hub", lambda url, timeout: {"ok": True})
    service = write_service(tmp_path)
    plan.proof_path.parent.mkdir(parents=True)
    plan.proof_path.write_text("{}")
    flip_config(
        service.config_path,
        cutover.ducklake_selection(
            plan, extension_dir="/ext", engine_sha256=SHA, epoch="e"
        ),
    )
    replays = []

    def run(argv, **kwargs):
        replays.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "Replayed 3 events", "")

    launchd = RecordingLaunchd(service)
    actions = rollback_actions(plan, service, since=5.0, launchd=launchd, run=run)
    for _ in range(2):  # idempotent: a second run is safe
        execute(actions, dry_run=False, echo=lambda _: None)
    assert load_config(service.config_path).analytics.backend == "legacy"
    argv, env = replays[0]
    assert argv[-6:] == ["outbox", "replay", "--sink", "legacy", "--since", "5.0"]
    assert env["DROVER_CONTROL_DSN"] == service.env["DROVER_CONTROL_DSN"]
    # Each run stops the loaded job, flips (already legacy: no-op), restarts.
    assert [c[1] for c in launchd.calls if c[1] != "print"] == [
        "bootout",
        "bootstrap",
    ] * 2


def test_verify_hub_needs_analytical_ok_and_ready():
    responses = iter(
        [
            (200, "ok\nanalytical=recovering\n"),
            (503, "{}"),
            (200, "ok\nanalytical=ok\n"),
            (200, json.dumps({"memory": {"state": "ok"}})),
        ]
    )
    result = verify_hub(
        "http://hub", timeout=5, fetch=lambda url: next(responses), interval=0
    )
    assert result["readyz_memory"] == {"state": "ok"}
    with pytest.raises(CutoverError, match="hub_not_ready"):
        verify_hub("http://hub", timeout=0, fetch=lambda url: (503, "down"), interval=0)


# --- gate ------------------------------------------------------------------------


def test_gate_risk_checks():
    restore = {
        "identity_sessions": 10_000,
        "identity_limit": 10_000,
        "unacknowledged_export_batches": 0,
        "unacknowledged_catalog_ids": [],
    }
    assert GateRun.identity_check(restore)[0]
    assert GateRun.export_batches_check(restore)[0]
    assert not GateRun.identity_check({**restore, "identity_sessions": 10_001})[0]
    stale = {**restore, "unacknowledged_export_batches": 1}
    passed, evidence = GateRun.export_batches_check(
        {**stale, "unacknowledged_catalog_ids": ["c1"]}
    )
    assert not passed and evidence["catalog_ids"] == ["c1"]


def test_gate_config_is_isolated_and_loadable(tmp_path):
    text = gate_config_text(
        duckdb_path=tmp_path / "data" / "drover.duckdb",
        incoming=tmp_path / "incoming",
        parquet=tmp_path / "parquet",
        worktrees=tmp_path / "worktrees",
        control_dsn_env="DROVER_LAKE_BLUE_GATE_CONTROL_DSN",
        schema="drover_control",
        ports={"metrics": 41000, "mcp": 41001, "otlp": 41002},
        budget_bytes=4_000_000_000,
        analytics={
            "backend": "ducklake",
            "catalog_dsn_env": "DROVER_LAKE_BLUE_GATE_CATALOG_DSN",
            "data_root": str(tmp_path / "lake"),
            "extension_dir": str(tmp_path / "ext"),
            "engine_sha256": SHA,
            "verification_sha256": SHA,
            "epoch": "gate",
        },
    )
    path = tmp_path / "gate.toml"
    path.write_text(text)
    cfg = load_config(path)
    assert cfg.incoming_dir == tmp_path / "incoming"
    assert cfg.metrics_http_port == 41000 and cfg.server_metrics_host == "127.0.0.1"
    assert cfg.update_enabled is False and cfg.analytics.backend == "ducklake"
    assert cfg.memory.rss_budget_bytes == 4_000_000_000
    assert cfg.summarizer_backend_policy == "cloud"
    assert cfg.control_store.dsn_env == "DROVER_LAKE_BLUE_GATE_CONTROL_DSN"


def test_peak_rss_sampler_keeps_peak_and_counts_errors():
    values = iter([10, 30, OSError("gone"), 20])

    def reader(pid):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    sampler = PeakRss(1, reader=reader)
    for _ in range(4):
        sampler.sample()
    assert sampler.as_dict()["peak_bytes"] == 30
    assert sampler.samples == 3 and sampler.errors == 1


def test_gate_child_env_strips_production_secrets(tmp_path, monkeypatch):
    from drover.server.lake.gate import GateOptions

    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    monkeypatch.setenv("PROD_CONTROL_DSN", "dbname=prod")
    monkeypatch.setenv("SCRATCH_DSN", "dbname=postgres")
    monkeypatch.setenv("DROVER_LAKE_BLUE_READER_DSN", "dbname=drover_lake_blue")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.drover.server")
    monkeypatch.setenv("PGPASSWORD", "pw")
    gate = GateRun(
        plan,
        GateOptions(
            legacy_root=tmp_path,
            source_dsn_env="PROD_CONTROL_DSN",
            scratch_admin_dsn_env="SCRATCH_DSN",
        ),
        echo=lambda _: None,
    )
    env = gate.child_env()
    for name in (
        "PROD_CONTROL_DSN",
        "SCRATCH_DSN",
        "DROVER_LAKE_BLUE_READER_DSN",
        "ANTHROPIC_API_KEY",
        "XPC_SERVICE_NAME",
        "PGPASSWORD",
    ):
        assert name not in env
    assert env["HOME"] == str(plan.gate_dir / "home")
    assert env["DROVER_DUCKDB_ANALYTICAL_MEMORY_LIMIT"] == "4GB"


def test_gate_refuses_a_scratch_cluster_shared_with_the_source(tmp_path, monkeypatch):
    from drover.server.lake.gate import GateOptions

    plan = CutoverPlan.from_name("blue", lake_root=tmp_path)
    monkeypatch.setenv("SRC", "host=/tmp port=5432 dbname=prod")
    monkeypatch.setenv("DST", "host=/tmp port=5432 dbname=postgres")
    options = GateOptions(
        legacy_root=tmp_path, source_dsn_env="SRC", scratch_admin_dsn_env="DST"
    )
    with pytest.raises(CutoverError, match="gate_scratch_cluster_is_source"):
        GateRun(plan, options).check_isolation()
    from dataclasses import replace

    GateRun(plan, replace(options, allow_shared_cluster=True)).check_isolation()


def test_gate_dir_is_only_replaced_when_it_is_a_previous_gate(tmp_path):
    from drover.server.lake.gate import GATE_MARKER, GateOptions

    plan = CutoverPlan.from_name("blue", lake_root=tmp_path / "lakes")
    plan.gate_dir.mkdir(parents=True)
    (plan.gate_dir / "precious").write_text("x")
    options = GateOptions(
        legacy_root=tmp_path / "legacy",
        source_dsn_env="S",
        scratch_admin_dsn_env="D",
        replace=True,
    )
    with pytest.raises(CutoverError, match="gate_dir_not_empty"):
        GateRun(plan, options).prepare_dir()
    (plan.gate_dir / GATE_MARKER).write_text("old")
    GateRun(plan, options).prepare_dir()
    assert not (plan.gate_dir / "precious").exists()


@pytest.mark.parametrize(
    "document, code",
    [
        ({"passed": True}, 0),
        ({"passed": False, "error": None}, 1),
        ({"passed": False, "error": "gate_hub_exited"}, 2),
    ],
)
def test_gate_cli_prints_one_verdict_and_exits_by_outcome(
    tmp_path, monkeypatch, document, code
):
    from drover.server.lake import gate

    seen = {}

    def fake_run_gate(plan, options, echo):
        seen["plan"], seen["options"] = plan, options
        return {"kind": "gate", "lake": plan.name, **document}

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    result = CliRunner().invoke(
        main,
        [
            "gate",
            "--lake",
            "blue",
            "--lake-root",
            str(tmp_path),
            "--legacy-root",
            str(tmp_path),
            "--source-dsn-env",
            "SRC",
            "--scratch-admin-dsn-env",
            "DST",
            "--memory-limit",
            "4GB",
        ],
    )
    assert result.exit_code == code, result.output
    assert json.loads(result.output)["lake"] == "blue"
    assert seen["options"].since_days == 60
    assert seen["options"].memory_limit == "4GB"


def test_seed_tar_holds_only_the_three_empty_relations(tmp_path):
    import tarfile

    output = tmp_path / "seed.tar"
    result = CliRunner().invoke(main, ["lake", "seed-tar", "--output", str(output)])
    assert result.exit_code == 0, result.output
    with tarfile.open(output) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == [
        "parquet/agent_events/date=_seed/agent_id=_seed/empty.parquet",
        "parquet/control_outbox_batches/empty.parquet",
        "parquet/provider_usage_snapshots/empty.parquet",
    ]
    again = CliRunner().invoke(main, ["lake", "seed-tar", "--output", str(output)])
    assert again.exit_code != 0


def test_cutover_cli_dry_runs_print_and_change_nothing(tmp_path, monkeypatch):
    service = write_service(tmp_path)
    before = service.config_path.read_text()
    calls = []
    monkeypatch.setattr(
        cutover.subprocess, "run", lambda *a, **kw: calls.append(a) or None
    )
    lakes = tmp_path / "lakes"
    lakes.mkdir()
    common = ["--lake", "blue", "--lake-root", str(lakes)]
    common += ["--plist", str(service.plist_path), "--dry-run"]
    switch = CliRunner().invoke(
        main, ["cutover", "switch", *common, "--legacy-root", str(tmp_path)]
    )
    assert switch.exit_code == 0, switch.output
    assert switch.output.count("(dry-run)") == 7
    rollback = CliRunner().invoke(
        main, ["cutover", "rollback", *common, "--since", "1759600000"]
    )
    assert rollback.exit_code == 0, rollback.output
    assert "outbox replay --sink legacy --since 1759600000.0" in rollback.output
    assert calls == [] and service.config_path.read_text() == before
    assert not (lakes / ".cutover-backups").exists()


def test_gate_cli_requires_its_inputs(tmp_path):
    result = CliRunner().invoke(
        main, ["gate", "--lake", "blue", "--lake-root", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "--legacy-root" in result.output and "--source-dsn-env" in result.output
