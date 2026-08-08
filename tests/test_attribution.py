from __future__ import annotations

import subprocess
from pathlib import Path

from drover.attribution import (
    _cwd_from_raw,
    GENERAL_WORKSPACE_ACTIVITY_TYPE,
    classify_cwd_activity,
    derive_repo_attribution,
    enrich_raw_repo_attribution,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_derive_repo_attribution_preserves_explicit_fields() -> None:
    attr = derive_repo_attribution(
        {
            "_repo_owner": "arniesaha",
            "_repo_name": "nexus",
            "gitBranch": "main",
            "cwd": "/does/not/need/to/exist",
        }
    )

    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "nexus"
    assert attr.branch == "main"


def test_enrich_raw_repo_attribution_from_existing_git_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "feature/context")
    _git(repo, "remote", "add", "origin", "git@github.com:arniesaha/nexus.git")

    enriched = enrich_raw_repo_attribution({"cwd": str(repo)})

    assert enriched["_repo_owner"] == "arniesaha"
    assert enriched["_repo_name"] == "nexus"
    assert enriched["gitBranch"] == "feature/context"


def test_enrich_raw_repo_attribution_does_not_guess_missing_cwd() -> None:
    enriched = enrich_raw_repo_attribution({"cwd": "/missing/remote/path"})

    assert "_repo_owner" not in enriched
    assert "_repo_name" not in enriched


def test_cwd_from_raw_accepts_workspacedir() -> None:
    """OpenClaw raw events expose the working dir as ``workspaceDir``."""
    assert _cwd_from_raw({"workspaceDir": "/tmp"}) == "/tmp"


def test_derive_repo_attribution_accepts_agentweave_repository_attr() -> None:
    """AgentWeave may emit a compact owner/repo repository attribute."""
    attr = derive_repo_attribution({"prov.repository": "arniesaha/healthos"})

    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "healthos"


def test_derive_repo_attribution_accepts_agentweave_repository_url() -> None:
    """Repository URLs should normalize through the same parser as git remotes."""
    attr = derive_repo_attribution(
        {"prov.repository": "https://github.com/arniesaha/nexus.git"}
    )

    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "nexus"


def test_derive_repo_attribution_accepts_agentweave_prov_cwd() -> None:
    """A cwd remains useful even when it cannot establish repository identity."""
    attr = derive_repo_attribution({"prov.cwd": "/srv/projects/example/backend"})

    assert attr.cwd == "/srv/projects/example/backend"
    assert attr.repo_owner is None
    assert attr.repo_name is None


def test_agentweave_project_label_does_not_invent_repo_identity() -> None:
    """Fallback labels are useful project labels, not reliable repo identity."""
    attr = derive_repo_attribution({"prov.project": "nexus"})

    assert attr.repo_owner is None
    assert attr.repo_name is None


# ---------------------------------------------------------------------------
# Optional remote known-roots configuration
# ---------------------------------------------------------------------------


def test_configured_known_roots_match_exact_and_descendants(monkeypatch) -> None:
    monkeypatch.setenv(
        "DROVER_REPO_ROOTS_JSON",
        '{"/srv/projects/example": "acme/example"}',
    )
    for cwd in ("/srv/projects/example", "/srv/projects/example/src/module"):
        attr = derive_repo_attribution({"workspaceDir": cwd})
        assert attr.repo_owner == "acme"
        assert attr.repo_name == "example"


def test_known_roots_unknown_path_returns_none() -> None:
    """Paths not in the mapping should leave owner/name as None."""
    attr = derive_repo_attribution({"workspaceDir": "/srv/projects/unknown-repo"})
    assert attr.repo_owner is None
    assert attr.repo_name is None


def test_known_roots_explicit_fields_short_circuit() -> None:
    """Explicit _repo_owner/_repo_name/gitBranch must win without a lookup."""
    attr = derive_repo_attribution(
        {"_repo_owner": "foo", "_repo_name": "bar", "gitBranch": "main"}
    )
    assert attr.repo_owner == "foo"
    assert attr.repo_name == "bar"
    assert attr.branch == "main"


def test_enrich_marks_configured_general_workspace(monkeypatch) -> None:
    monkeypatch.setenv("DROVER_GENERAL_WORKSPACE_ROOTS", "/srv/operator")
    enriched = enrich_raw_repo_attribution({"workspaceDir": "/srv/operator"})

    assert "_repo_owner" not in enriched
    assert "_repo_name" not in enriched
    assert enriched["_nexus_activity_type"] == GENERAL_WORKSPACE_ACTIVITY_TYPE


def test_general_workspace_classification_is_exact_not_broad_home_mapping(
    monkeypatch,
) -> None:
    """Unknown project-like descendants should remain real attribution misses."""
    monkeypatch.setenv("DROVER_GENERAL_WORKSPACE_ROOTS", "/srv/operator")
    assert classify_cwd_activity("/srv/operator") == GENERAL_WORKSPACE_ACTIVITY_TYPE
    assert classify_cwd_activity("/srv/operator/project") is None


def test_known_roots_openclaw_runtime_prefix_boundary() -> None:
    """Prefix matching must not map sibling paths that merely share text."""
    attr = derive_repo_attribution({"workspaceDir": "/srv/projects/example-backup"})
    assert attr.repo_owner is None
    assert attr.repo_name is None
