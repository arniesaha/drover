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
    """AgentWeave can emit cwd as ``prov.cwd`` instead of a top-level cwd."""
    attr = derive_repo_attribution(
        {"prov.cwd": "/home/Arnab/clawd/projects/healthos/backend"}
    )

    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "healthos"


def test_agentweave_project_label_does_not_invent_repo_identity() -> None:
    """Fallback labels are useful project labels, not reliable repo identity."""
    attr = derive_repo_attribution({"prov.project": "nexus"})

    assert attr.repo_owner is None
    assert attr.repo_name is None


# ---------------------------------------------------------------------------
# Known-roots fallback tests  (bug #57 – NAS paths on Mac Mini)
# ---------------------------------------------------------------------------


def test_known_roots_exact_match_nexus() -> None:
    """Exact NAS root path should map to the nexus repo."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/dev/nexus"})
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "nexus"


def test_known_roots_subdir_match_nexus() -> None:
    """A subdirectory of a known root should also resolve via prefix match."""
    attr = derive_repo_attribution(
        {"workspaceDir": "/home/Arnab/dev/nexus/src/some/subdir"}
    )
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "nexus"


def test_known_roots_unknown_path_returns_none() -> None:
    """Paths not in the mapping should leave owner/name as None."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/dev/unknown-repo"})
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


def test_known_roots_agentweave() -> None:
    """agentweave NAS path should resolve correctly."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/dev/agentweave"})
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "agentweave"


def test_known_roots_openclaw() -> None:
    """openclaw NAS path is the primary driver of bug #57."""
    attr = derive_repo_attribution(
        {"workspaceDir": "/home/Arnab/dev/openclaw/plugins/cursor"}
    )
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "openclaw"


def test_known_roots_openclaw_runtime_root() -> None:
    """Current OpenClaw runtime workspace should map to openclaw."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/clawd"})
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "openclaw"


def test_known_roots_openclaw_runtime_maps_when_path_exists_without_git(
    monkeypatch,
) -> None:
    """Runtime workspace may exist on the collector without being a git repo."""
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/home/Arnab/clawd")
    monkeypatch.setattr("drover.attribution._git", lambda *args: None)

    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/clawd"})

    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "openclaw"


def test_known_roots_openclaw_runtime_subdir() -> None:
    """Subdirectories of the OpenClaw runtime workspace should map too."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/clawd/subdir"})
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "openclaw"


def test_known_roots_healthos_project_under_openclaw_runtime() -> None:
    """Project-scoped clawd workspaces should use the project repo, not clawd."""
    attr = derive_repo_attribution(
        {"workspaceDir": "/home/Arnab/clawd/projects/healthos/backend"}
    )
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "healthos"


def test_known_roots_ai_ops_studio_project_under_openclaw_runtime() -> None:
    """Recent NAS Claude project cwd should resolve via project root mapping."""
    attr = derive_repo_attribution(
        {"workspaceDir": "/home/Arnab/clawd/projects/ai-ops-studio/site"}
    )
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "ai-ops-studio"


def test_known_roots_macmini_jenny_project_roots() -> None:
    """Mac Mini project roots under Jenny are safe to attribute by exact prefix."""
    attr = derive_repo_attribution({"cwd": "/Users/arnabmac/jenny/nexus/src"})
    assert attr.repo_owner == "arniesaha"
    assert attr.repo_name == "nexus"


def test_known_roots_paperclip_nexus_workspace_paths() -> None:
    """Paperclip pod workspaces are remote paths but still stable repo roots."""
    for cwd in (
        "/paperclip/home/instances/default/workspaces/e46aa686-4fa6-414c-94a7-946538fb308f/nexus",
        "/paperclip/home/instances/default/workspaces/e46aa686-4fa6-414c-94a7-946538fb308f/nexus/src",
        "/paperclip/home/instances/default/workspaces/e46aa686/4fa6/414c/94a7/946538fb308f/nexus",
        "/paperclip/home/instances/default/workspaces/e46aa686/4fa6/414c/94a7/946538fb308f/nexus/tests",
    ):
        attr = derive_repo_attribution({"cwd": cwd})
        assert attr.repo_owner == "arniesaha"
        assert attr.repo_name == "nexus"


def test_known_roots_hermes_agent_checkout() -> None:
    """Hermes Agent lives under ~/.hermes but is still a real git checkout."""
    attr = derive_repo_attribution({"cwd": "/Users/arnabmac/.hermes/hermes-agent/src"})
    assert attr.repo_owner == "NousResearch"
    assert attr.repo_name == "hermes-agent"


def test_working_directory_xml_in_content_can_drive_attribution() -> None:
    """Claude-Mem queue events carry the observed cwd inside XML content."""
    attr = derive_repo_attribution(
        {
            "content": "<observed_from_primary_session>\n"
            "  <working_directory>/Users/arnabmac/.hermes/hermes-agent</working_directory>\n"
            "</observed_from_primary_session>"
        }
    )
    assert attr.repo_owner == "NousResearch"
    assert attr.repo_name == "hermes-agent"


def test_known_roots_does_not_map_macmini_home_or_claude_memory() -> None:
    """Generic home and Claude memory folders are intentionally unattributed."""
    for cwd in (
        "/Users/arnabmac",
        "/Users/arnabmac/.claude-mem/observer-sessions",
    ):
        attr = derive_repo_attribution({"cwd": cwd})
        assert attr.repo_owner is None
        assert attr.repo_name is None
        assert attr.activity_type == GENERAL_WORKSPACE_ACTIVITY_TYPE


def test_known_roots_does_not_map_arnab_home() -> None:
    """Do not broadly attribute all /home/Arnab paths to OpenClaw."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab"})
    assert attr.repo_owner is None
    assert attr.repo_name is None
    assert attr.activity_type == GENERAL_WORKSPACE_ACTIVITY_TYPE


def test_enrich_marks_arnab_home_as_general_workspace() -> None:
    """Generic NAS home cwd is expected non-project activity, not a repo."""
    enriched = enrich_raw_repo_attribution({"workspaceDir": "/home/Arnab"})

    assert "_repo_owner" not in enriched
    assert "_repo_name" not in enriched
    assert enriched["_nexus_activity_type"] == GENERAL_WORKSPACE_ACTIVITY_TYPE


def test_general_workspace_classification_is_exact_not_broad_home_mapping() -> None:
    """Unknown project-like descendants should remain real attribution misses."""
    assert classify_cwd_activity("/home/Arnab") == GENERAL_WORKSPACE_ACTIVITY_TYPE
    assert classify_cwd_activity("/home/Arnab/dev/unknown-repo") is None


def test_known_roots_openclaw_runtime_prefix_boundary() -> None:
    """Prefix matching must not map sibling paths that merely share text."""
    attr = derive_repo_attribution({"workspaceDir": "/home/Arnab/clawd-backup"})
    assert attr.repo_owner is None
    assert attr.repo_name is None
