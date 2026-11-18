"""Candidate provider selection with GitHub as the safe default."""

from .github import GitHubProvider
from .local_candidates import LocalCandidateProvider


def provider_for_scope(workspace_id, scope, *, public_text_validator=None):
    if scope.candidate_source == "local_file":
        return LocalCandidateProvider(workspace_id, scope)
    return GitHubProvider(
        workspace_id, scope, public_text_validator=public_text_validator
    )
