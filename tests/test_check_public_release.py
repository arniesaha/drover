import importlib.util
from pathlib import Path
import sys

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "check_public_release.py"
SPEC = importlib.util.spec_from_file_location("check_public_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
check_paths = MODULE.check_paths
main = MODULE.main


def write_file(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_check_paths_finds_private_environment_and_stale_names(tmp_path: Path) -> None:
    paths = [
        write_file(tmp_path, "docs/setup.md", "cd /Users/alice/projects/drover\n"),
        write_file(tmp_path, "docs/network.md", "server: http://192.168.1.70:7080\n"),
        write_file(tmp_path, "docs/tailnet.md", "host.private-name.ts.net\n"),
        write_file(tmp_path, "docs/sdk.md", "Import NexusKit in your application.\n"),
        write_file(tmp_path, "docs/harness.md", "Start the Meta Harness daemon.\n"),
    ]

    findings = check_paths(paths, root=tmp_path)

    assert {finding.rule for finding in findings} == {
        "personal-home-path",
        "private-ip-address",
        "private-tailnet-hostname",
        "legacy-public-name",
        "legacy-harness-name",
    }


def test_check_paths_scans_release_runtime_source(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "src/drover/defaults.py",
        'DEFAULT_ROOT = "/Users/example/projects/drover"\n',
    )

    findings = check_paths([path], root=tmp_path)

    assert [finding.rule for finding in findings] == ["personal-home-path"]


def test_check_paths_redacts_credential_values(tmp_path: Path) -> None:
    path = write_file(tmp_path, "config.env", 'token = "secret-value"\n')

    findings = check_paths([path], root=tmp_path)

    assert len(findings) == 1
    assert findings[0].rule == "credential-value"
    assert "secret-value" not in findings[0].excerpt
    assert "[REDACTED]" in findings[0].excerpt


def test_check_paths_does_not_treat_type_annotations_as_credentials(
    tmp_path: Path,
) -> None:
    path = write_file(
        tmp_path,
        "Client.swift",
        "func configure(token: String) async {}\n",
    )

    assert check_paths([path], root=tmp_path) == []


def test_check_paths_allows_test_only_credential_fixtures(tmp_path: Path) -> None:
    path = write_file(tmp_path, "tests/test_client.py", 'token = "fake-token"\n')

    assert check_paths([path], root=tmp_path) == []


def test_check_paths_skips_binary_files(tmp_path: Path) -> None:
    path = tmp_path / "fixture.bin"
    path.write_bytes(b"token=secret-value\x00binary")

    assert check_paths([path], root=tmp_path) == []


def test_check_paths_allows_documented_legacy_compatibility(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "docs/compatibility.md",
        "NexusKit is retained only as a historical compatibility name.\n",
    )

    assert check_paths([path], root=tmp_path) == []


def test_check_paths_rejects_legacy_nexus_skill_entrypoint(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "skills/nexus/SKILL.md",
        "---\nname: nexus\ndescription: Use when recalling prior work.\n---\n",
    )

    findings = check_paths([path], root=tmp_path)

    assert len(findings) == 1
    assert findings[0].rule == "legacy-skill-entrypoint"


def test_check_paths_rejects_private_roadmap_link(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "docs/direction.md",
        "See https://github.com/arniesaha/drover-roadmap for private plans.\n",
    )

    findings = check_paths([path], root=tmp_path)

    assert len(findings) == 1
    assert findings[0].rule == "private-roadmap-link"


def test_check_paths_rejects_private_planning_paths(tmp_path: Path) -> None:
    paths = [
        write_file(tmp_path, "docs/roadmap.md", "# Roadmap\n"),
        write_file(tmp_path, "docs/superpowers/specs/design.md", "# Design\n"),
    ]

    findings = check_paths(paths, root=tmp_path)

    assert [finding.rule for finding in findings] == [
        "private-planning-path",
        "private-planning-path",
    ]


def test_check_paths_rejects_em_dash_in_public_prose(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "docs/overview.md",
        "Drover is local-first — sessions stay under your control.\n",
    )

    findings = check_paths([path], root=tmp_path)

    assert [finding.rule for finding in findings] == ["public-em-dash"]


def test_check_paths_rejects_legacy_product_positioning(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "README.md",
        "Drover, formerly Nexus, manages coding-agent sessions.\n",
    )

    findings = check_paths([path], root=tmp_path)

    assert [finding.rule for finding in findings] == ["legacy-positioning-copy"]


def test_check_paths_limits_copy_rules_to_public_prose(tmp_path: Path) -> None:
    paths = [
        write_file(tmp_path, "src/drover/client.py", "# retry — then fail\n"),
        write_file(tmp_path, "src/drover/prompts/system.md", "Think — then act.\n"),
        write_file(tmp_path, "tests/fixtures/session.md", "Captured — unchanged.\n"),
    ]

    assert check_paths(paths, root=tmp_path) == []


def test_check_paths_allows_nexus_compatibility_contract(tmp_path: Path) -> None:
    path = write_file(
        tmp_path,
        "docs/compatibility.md",
        "Historical spans formerly used `nexus.*` telemetry keys.\n",
    )

    assert check_paths([path], root=tmp_path) == []


def test_check_paths_scopes_public_prose_relative_to_repository_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tests" / "drover"
    path = write_file(root, "docs/overview.md", "Direct copy — under docs.\n")

    findings = check_paths([path], root=root)

    assert [finding.rule for finding in findings] == ["public-em-dash"]


def test_check_paths_ignores_public_directory_names_above_repository_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "docs" / "drover"
    path = write_file(root, "src/notes.md", "Internal note — not public prose.\n")

    assert check_paths([path], root=root) == []


# The pre-commit hook audits the staged set, which is neither the tracked set
# nor the working tree, so it hands the script an explicit list of paths.


def test_main_audits_only_the_paths_it_is_given(tmp_path: Path) -> None:
    write_file(tmp_path, "docs/roadmap.md", "# Roadmap\n")
    clean = write_file(tmp_path, "docs/overview.md", "Drover keeps work local.\n")

    assert main([str(clean), "--root", str(tmp_path)]) == 0


def test_main_reports_findings_relative_to_the_given_root(
    tmp_path: Path, capsys
) -> None:
    path = write_file(tmp_path, "docs/superpowers/design.md", "# Design\n")

    assert main([str(path), "--root", str(tmp_path)]) == 1

    output = capsys.readouterr().out
    assert "docs/superpowers/design.md:1: private-planning-path" in output
    assert "1 finding(s)" in output


def test_main_accepts_paths_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    write_file(tmp_path, "docs/superpowers/design.md", "# Design\n")
    monkeypatch.chdir(tmp_path)

    assert main(["docs/superpowers/design.md", "--root", str(tmp_path)]) == 1


def test_main_refuses_paths_outside_the_audit_root(tmp_path: Path, capsys) -> None:
    outside = write_file(tmp_path, "elsewhere/notes.md", "A note.\n")
    root = tmp_path / "repo"
    root.mkdir()

    assert main([str(outside), "--root", str(root)]) == 2
    assert "outside" in capsys.readouterr().out
