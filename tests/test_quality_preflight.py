"""Focused contract tests for scripts/quality/preflight.{sh,py} (issue #1024).

Every environment failure is induced with stubs or a throwaway git repo; host
PATH is never mutated: the helper takes an explicit ``path`` and subprocesses
get their own ``env``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QUALITY = ROOT / "scripts" / "quality"
_spec = importlib.util.spec_from_file_location(
    "quality_preflight", QUALITY / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

GIT_DIR = str(Path(shutil.which("git")).parent)
DEFAULT_SHARED = preflight.SHARED_TEMP_ROOTS


@pytest.fixture(autouse=True)
def shared_temp(tmp_path, monkeypatch) -> Path:
    """Hermetic shared-temp policy: tmp_path itself may live under /tmp when
    TMPDIR is unset (clean ``env -i`` shell), so tests substitute a fake root."""
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setattr(preflight, "SHARED_TEMP_ROOTS", (str(shared),))
    return shared


def test_default_policy_refuses_system_temp_roots():
    assert {"/tmp", "/private/tmp", "/var/tmp"} <= set(DEFAULT_SHARED)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "drover").mkdir(parents=True)
    (root / "src" / "drover" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.11"\n'
    )
    (root / "uv.lock").write_text("version = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def out(tmp_path: Path) -> Path:
    target = tmp_path / "out"
    target.mkdir()
    return target


def stub_python(
    where: Path,
    *,
    drover_file: str,
    version=(3, 14, 7),
    missing=(),
    pg: dict | None = None,
) -> Path:
    """A fake interpreter answering the module probe and the PG probe."""
    mods = {
        name: (
            {"ok": False, "error": f"ModuleNotFoundError: No module named '{name}'"}
            if name in missing
            else {
                "ok": True,
                "file": drover_file if name == "drover" else f"/x/{name}.py",
                "version": "1",
            }
        )
        for name in ("pytest", "drover", "psycopg")
    }
    probe = {
        "executable": str(where / "python-stub"),
        "version": list(version),
        "modules": mods,
    }
    script = where / "python-stub"
    script.write_text(
        '#!/bin/sh\ncase "$3" in\n'
        f"  *pg_namespace*) printf '%s\\n' '{json.dumps(pg or {})}' ;;\n"
        f"  *) printf '%s\\n' '{json.dumps(probe)}' ;;\nesac\n"
    )
    script.chmod(0o755)
    return script


def run(capsys, argv, path=GIT_DIR):
    code = preflight.main([str(a) for a in argv], path=path)
    lines = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(lines[-1])


def base_args(repo: Path, out: Path, python: Path) -> list:
    return ["--root", repo, "--out", out, "--python", python]


# --- POSIX bootstrap: the missing-interpreter assumption ---------------------


def _bootstrap_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree" / "scripts" / "quality"
    tree.mkdir(parents=True)
    shutil.copy(QUALITY / "preflight.sh", tree / "preflight.sh")
    shutil.copy(QUALITY / "preflight.py", tree / "preflight.py")
    return tree


def test_bootstrap_classifies_absent_supported_interpreter(tmp_path):
    tree = _bootstrap_tree(tmp_path)
    empty = tmp_path / "bin"
    empty.mkdir()
    env = {"PATH": str(empty), "HOME": str(tmp_path)}
    # The old assumption: an unversioned interpreter on PATH. It dies 127 with
    # an unclassified "not found" and no diagnosis.
    naive = subprocess.run(
        ["/bin/sh", "-c", "python3 scripts/quality/preflight.py"],
        cwd=tree.parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    assert naive.returncode == 127
    result = subprocess.run(
        ["/bin/sh", str(tree / "preflight.sh"), "--out", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["classification"] == "environment_unavailable"
    assert "no Python >= 3.11" in result.stderr


def test_bootstrap_skips_too_old_interpreter(tmp_path):
    tree = _bootstrap_tree(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    old = bindir / "python3"
    old.write_text("#!/bin/sh\nexit 1\n")  # answers the >=3.11 check with "too old"
    old.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", str(tree / "preflight.sh"), "--out", str(tmp_path)],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3


# --- interpreter, tools, dependencies -----------------------------------------


def test_missing_project_venv_is_environment_unavailable(capsys, repo, out):
    code, result = run(capsys, ["--root", repo, "--out", out])
    assert (code, result["check"]) == (3, "interpreter")
    assert "uv sync --locked --extra dev" in result["action"]


def test_missing_git_tool(capsys, repo, out, tmp_path):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    code, result = run(capsys, base_args(repo, out, py), path=str(tmp_path / "nothing"))
    assert (code, result["check"]) == (3, "tool:git")


def test_missing_dependency_blocks_expensive_work(capsys, repo, out, tmp_path):
    py = stub_python(
        tmp_path, drover_file=str(repo / "src/drover/__init__.py"), missing=("pytest",)
    )
    marker = tmp_path / "expensive-ran"
    code, result = run(
        capsys,
        [
            *base_args(repo, out, py),
            "--then",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w')",
        ],
    )
    assert (code, result["check"]) == (3, "import:pytest")
    assert not marker.exists()
    assert not list(out.iterdir()), "no receipt and no leftover probe after failure"


def test_runtime_shadowing_drover_is_rejected(capsys, repo, out, tmp_path):
    py = stub_python(
        tmp_path, drover_file="/Users/x/.drover/runtime/lib/drover/__init__.py"
    )
    code, result = run(capsys, base_args(repo, out, py))
    assert (code, result["check"]) == (3, "import:drover")


def test_unsupported_project_python_version(capsys, repo, out, tmp_path):
    py = stub_python(
        tmp_path, drover_file=str(repo / "src/drover/__init__.py"), version=(3, 10, 9)
    )
    code, result = run(capsys, base_args(repo, out, py))
    assert (code, result["check"]) == (3, "interpreter")


# --- output directory ---------------------------------------------------------


@pytest.mark.parametrize("kind", ["shared_tmp", "missing", "read_only"])
def test_invalid_or_denied_output(capsys, repo, tmp_path, shared_temp, kind):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    target = {
        "shared_tmp": shared_temp,
        "missing": tmp_path / "absent",
        "read_only": tmp_path / "ro",
    }[kind]
    if kind == "read_only":
        target.mkdir()
        target.chmod(0o500)
    try:
        code, result = run(capsys, base_args(repo, target, py))
    finally:
        if kind == "read_only":
            target.chmod(0o700)
    assert (code, result["check"]) == (3, "output_dir")


# --- worktree/base context and receipts --------------------------------------


def test_wrong_worktree_and_base(capsys, repo, out, tmp_path):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    code, result = run(
        capsys, [*base_args(repo, out, py), "--expect-worktree", tmp_path]
    )
    assert (code, result["check"]) == (3, "worktree")
    code, result = run(capsys, [*base_args(repo, out, py), "--expect-base", "0" * 40])
    assert (code, result["check"]) == (3, "base")


def test_valid_clean_context_writes_receipt_and_cleans_probe(
    capsys, repo, out, tmp_path
):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    head = _git(repo, "rev-parse", "HEAD")
    code, result = run(
        capsys,
        [
            *base_args(repo, out, py),
            "--expect-worktree",
            repo,
            "--expect-base",
            head,
            "--require-clean",
        ],
    )
    assert (code, result["classification"]) == (0, "pass")
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["head"] == head and receipt["python_version"] == "3.14.7"
    assert (
        receipt["worktree"] == str(repo.resolve()) and len(receipt["lock_sha256"]) == 64
    )
    assert [p.name for p in out.iterdir()] == [Path(result["receipt"]).name]


def test_stale_receipt_cannot_certify_new_source(capsys, repo, out, tmp_path):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    _, first = run(capsys, base_args(repo, out, py))
    (repo / "uv.lock").write_text("version = 2\n")
    _git(repo, "commit", "-qam", "lock change")
    code, result = run(
        capsys, [*base_args(repo, out, py), "--verify-receipt", first["receipt"]]
    )
    assert (code, result["check"]) == (3, "receipt")
    assert "head" in result["message"] and "lock_sha256" in result["message"]


@pytest.mark.parametrize(
    "rc,code,classification",
    [
        (0, 0, "pass"),
        (1, 1, "product_test_failure"),
        (5, 4, "harness_failure"),
    ],
)
def test_then_classifies_focused_command(
    capsys, repo, out, tmp_path, rc, code, classification
):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    got, result = run(
        capsys,
        [
            *base_args(repo, out, py),
            "--then",
            sys.executable,
            "-c",
            f"raise SystemExit({rc})",
        ],
    )
    assert (got, result["classification"]) == (code, classification)


def test_then_refuses_unversioned_python(capsys, repo, out, tmp_path):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    code, result = run(capsys, [*base_args(repo, out, py), "--then", "python3", "-V"])
    assert (code, result["check"]) == (4, "then")


def test_cleanup_failure_is_harness_failure_and_retains_proof(
    capsys, repo, out, tmp_path, monkeypatch
):
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))

    def deny(path):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(preflight, "_remove", deny)
    code, result = run(capsys, base_args(repo, out, py))
    assert (code, result["check"]) == (4, "cleanup")
    leftovers = list(out.iterdir())
    assert [p.name.startswith(".preflight-probe-") for p in leftovers] == [True]
    assert str(leftovers[0]) in result["message"]


# --- optional PostgreSQL fixture profile -------------------------------------

GOOD_PG = {
    "user": "drover_test",
    "server_version_num": 170004,
    "read_only": "on",
    "namespaces": 4,
    "host": "/Users/x/scratch/pg",
    "port": 55432,
    "dbname": "postgres",
    "data_directory": "/Users/x/scratch/pg/data",
}


def _pg_path(tmp_path: Path, client) -> str:
    """client: int major, literal --version text, or None for no psql at all."""
    bindir = tmp_path / "pgbin"
    bindir.mkdir(exist_ok=True)
    psql = bindir / "psql"
    if client is None:
        psql.unlink(missing_ok=True)
    else:
        text = (
            f"psql (PostgreSQL) {client}.4 (Homebrew)"
            if isinstance(client, int)
            else client
        )
        psql.write_text(f"#!/bin/sh\necho '{text}'\n")
        psql.chmod(0o755)
    return os.pathsep.join([str(bindir), GIT_DIR])


def _pg_args(repo, out, py):
    return [
        *base_args(repo, out, py),
        "--profile",
        "focused-tests-pg",
        "--pg-dsn-env",
        "PREFLIGHT_TEST_DSN",
        "--pg-expect-role",
        "drover_test",
        "--pg-expect-major",
        "17",
        "--pg-forbid-endpoint",
        "5432",
        "--pg-expect-data-prefix",
        "/Users/x/scratch/",
    ]


@pytest.mark.parametrize(
    "override,client,check",
    [
        ({}, 17, None),
        ({"user": "postgres"}, 17, "pg:role"),
        ({"port": 5432, "host": "/tmp"}, 17, "pg:endpoint"),
        (
            {"data_directory": "/opt/homebrew/var/postgresql@17"},
            17,
            "pg:data_directory",
        ),
        ({"server_version_num": 160004}, 17, "pg:version"),
        ({}, 16, "pg:binary"),
    ],
)
def test_pg_fixture_checks(
    capsys, repo, out, tmp_path, monkeypatch, override, client, check
):
    monkeypatch.setenv(
        "PREFLIGHT_TEST_DSN", "host=/x port=1 user=drover_test password=hunter2"
    )
    py = stub_python(
        tmp_path,
        drover_file=str(repo / "src/drover/__init__.py"),
        pg={**GOOD_PG, **override},
    )
    code, result = run(capsys, _pg_args(repo, out, py), path=_pg_path(tmp_path, client))
    assert result.get("check") == check, result
    assert code == (0 if check is None else 3)
    blob = json.dumps(result) + "".join(p.read_text() for p in out.iterdir())
    assert "hunter2" not in blob


@pytest.mark.parametrize(
    "client", [None, "psql: not a postgres client", "psql (PostgreSQL)"]
)
def test_pg_missing_or_malformed_client_blocks_expensive_work(
    capsys, repo, out, tmp_path, monkeypatch, client
):
    monkeypatch.setenv("PREFLIGHT_TEST_DSN", "host=/x port=1 user=drover_test")
    py = stub_python(
        tmp_path, drover_file=str(repo / "src/drover/__init__.py"), pg=GOOD_PG
    )
    marker = tmp_path / "expensive-ran"
    code, result = run(
        capsys,
        [
            *_pg_args(repo, out, py),
            "--then",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w')",
        ],
        path=_pg_path(tmp_path, client),
    )
    assert (code, result["check"]) == (3, "pg:binary"), result
    assert not marker.exists()
    assert not list(out.iterdir()), "no receipt after a PG binary failure"


@pytest.mark.parametrize(
    "override,client",
    [
        ({"port": 55433}, 17),
        ({"user": "other_role"}, 17),
        ({"server_version_num": 180001}, 18),
        ({"data_directory": "/Users/x/scratch/pg2/data"}, 17),
    ],
)
def test_receipt_cannot_certify_a_different_pg_fixture(
    capsys, repo, out, tmp_path, monkeypatch, override, client
):
    # No expectation flags: each second fixture is individually acceptable, so
    # only receipt binding can tell that it is not the fixture first certified.
    monkeypatch.setenv("PREFLIGHT_TEST_DSN", "host=/x port=1 user=drover_test")
    drover = str(repo / "src/drover/__init__.py")
    py = stub_python(tmp_path, drover_file=drover, pg=GOOD_PG)
    args = [
        *base_args(repo, out, py),
        "--profile",
        "focused-tests-pg",
        "--pg-dsn-env",
        "PREFLIGHT_TEST_DSN",
    ]
    _, first = run(capsys, args, path=_pg_path(tmp_path, 17))
    assert first["classification"] == "pass", first
    stub_python(tmp_path, drover_file=drover, pg={**GOOD_PG, **override})
    code, result = run(
        capsys,
        [*args, "--verify-receipt", first["receipt"]],
        path=_pg_path(tmp_path, client),
    )
    assert (code, result["check"]) == (3, "receipt"), result
    assert "'pg'" in result["message"]


def test_pg_profile_requires_dsn_and_plain_profile_never_probes_pg(
    capsys, repo, out, tmp_path, monkeypatch
):
    monkeypatch.delenv("PREFLIGHT_TEST_DSN", raising=False)
    py = stub_python(tmp_path, drover_file=str(repo / "src/drover/__init__.py"))
    code, result = run(capsys, _pg_args(repo, out, py), path=_pg_path(tmp_path, 17))
    assert (code, result["check"]) == (3, "pg:dsn")
    code, result = run(capsys, base_args(repo, out, py))
    assert code == 0 and "pg" not in json.loads(Path(result["receipt"]).read_text())


# --- real interpreter (not a stub) -------------------------------------------


def test_real_locked_interpreter_against_this_checkout(capsys, out):
    if (
        not (ROOT / ".venv").exists()
        or ROOT.resolve() not in Path(sys.prefix).resolve().parents
    ):
        pytest.skip("needs this checkout's own uv venv")
    code, result = run(
        capsys, ["--root", ROOT, "--out", out, "--python", sys.executable]
    )
    assert (code, result["classification"]) == (0, "pass"), result
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["python_version"] == ".".join(map(str, sys.version_info[:3]))
