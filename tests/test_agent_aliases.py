import pytest

from drover.agent_aliases import AGENT_ALIASES, canonicalize


def test_canonicalize_none_returns_none():
    assert canonicalize(None) is None


def test_canonicalize_unknown_passes_through():
    assert canonicalize("mux-router") == "mux-router"
    assert canonicalize("totally-novel-agent") == "totally-novel-agent"


def test_canonicalize_already_canonical_passes_through():
    for canonical in {"nas-claude", "nas-openclaw", "macmini-hermes"}:
        assert canonicalize(canonical) == canonical


@pytest.mark.parametrize(
    "raw,expected",
    [
        # AgentWeave proxy <tool>-<host> style
        ("claude-nas", "nas-claude"),
        ("claude-code-nas", "nas-claude"),
        ("hermes-macmini", "macmini-hermes"),
        ("pimono-macmini", "macmini-pimono"),
        ("claude-macmini", "macmini-claude"),
        # Historical AgentWeave emitted mac rather than macmini in Claude Code ids.
        ("claude-code-mac", "macmini-claude"),
        ("claude-code-mac-subagent", "macmini-claude"),
        ("claude-code-mac-session-hook", "macmini-claude"),
        ("openclaw-nas", "nas-openclaw"),
        ("max-macmini", "macmini-pimono"),
        # v1 single-token instrumentation (from 374a6f7)
        ("max-v1", "macmini-pimono"),
        ("nix-v1", "nas-openclaw"),
        ("nix-v1-subagent-v1", "nas-openclaw"),
        # Friendly shortcuts the user types from the CLI
        ("nix", "nas-openclaw"),
        ("openclaw", "nas-openclaw"),
        ("claude", "nas-claude"),
        ("hermes", "macmini-hermes"),
        ("jenny", "macmini-hermes"),
        ("max", "macmini-pimono"),
        ("pimono", "macmini-pimono"),
    ],
)
def test_known_aliases(raw, expected):
    assert canonicalize(raw) == expected


def test_canonicalize_is_idempotent():
    """Applying canonicalize twice equals applying once. Required because the
    parser canonicalizes at write time and the CLI canonicalizes at read time;
    if both ran on the same string it must be a no-op the second time."""
    for raw in AGENT_ALIASES:
        once = canonicalize(raw)
        twice = canonicalize(once)
        assert once == twice
