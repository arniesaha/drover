import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from drover.server.__main__ import main
from drover.server.control_store import postgres_control_store
from drover.server.profile import ProfileActor, act_on_proposal, read_profile
from drover.server.profile_cli import import_record, import_sources, parse_markdown


def test_sensitive_nested_headings():
    markdown = """# Preferences
Use tables.
## Health
Private health content.
### Details
Nested details.
## Finance
Private finances.
## Job-search
Private search.
## Personal
Private life.
# Rules
Ask before deployment.
"""
    items = parse_markdown(markdown, tier="trusted")
    assert [i["tier"] for i in items] == [
        "trusted",
        "private",
        "private",
        "private",
        "private",
        "private",
        "trusted",
    ]
    assert items[-1]["kind"] == "rule"
    assert (
        parse_markdown("Medical\n=======\nPrivate information")[0]["tier"] == "private"
    )
    assert parse_markdown("# Active work\nThread")[0]["layer"] == "work"
    assert parse_markdown("# Decisions\nDecision")[0]["layer"] == "decision"


@pytest.mark.parametrize("tier", ["private", "general", "trusted"])
def test_categories_never_raise_explicit_tier(tier):
    for text in (
        "# Miscellaneous\nA fact",
        "An unheaded fact",
        "# Preferences\nUse tables",
    ):
        assert parse_markdown(text, tier=tier)[0]["tier"] == tier
    assert parse_markdown("# Rules\nUse tables")[0]["tier"] == "private"


def test_code_fences_are_content():
    items = parse_markdown("# Preferences\n```markdown\n# Health\nExample only\n```\n")
    assert len(items) == 1 and items[0]["tier"] == "private"
    assert "# Health" in items[0]["body"]


def test_cli_dry_run_without_config(tmp_path, monkeypatch):
    source = tmp_path / "USER.md"
    source.write_text("# Preferences\nUse tables.\n# Health\nSensitive.\n")

    def unexpected(*args):
        raise AssertionError("dry-run must not resolve configuration")

    monkeypatch.setattr("drover.server.__main__._resolve_config", unexpected)
    result = CliRunner().invoke(main, ["profile", "import", "--from", str(source)])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["dry_run"] and [i["tier"] for i in value["items"]] == [
        "private",
        "private",
    ]
    assert source.read_text().endswith("Sensitive.\n")


def test_cli_apply_idempotence_and_private_pending(
    pg_control_path, tmp_path, monkeypatch
):
    source = tmp_path / "MEMORY.md"
    original = "# Preferences\nUse tables.\n# Health\nPrivate marker.\n"
    source.write_text(original)
    monkeypatch.setattr(
        "drover.server.__main__._resolve_config",
        lambda _: SimpleNamespace(duckdb_path=pg_control_path),
    )
    args = ["profile", "import", "--from", str(source), "--tier", "trusted", "--apply"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    statuses = json.loads(result.output)["items"]
    assert [i["status"] for i in statuses] == ["accepted", "pending"]
    assert "Private marker" not in read_profile(pg_control_path)["bundle"]
    assert "Use tables" not in read_profile(pg_control_path)["bundle"]  # trusted-only
    repeat = CliRunner().invoke(main, args)
    assert repeat.exit_code == 0, repeat.output
    assert all(i["unchanged"] for i in json.loads(repeat.output)["items"])
    with postgres_control_store(pg_control_path).connection() as con:
        assert con.execute("SELECT count(*) FROM profile_proposals").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM profile_items").fetchone()[0] == 1
    assert source.read_text() == original


def test_changed_import_is_pending_review(pg_control_path, tmp_path):
    source = tmp_path / "USER.md"
    source.write_text("# Preferences\nFirst preference.\n")
    first = import_record(pg_control_path, import_sources(source, tier="general")[0])
    source.write_text("# Preferences\nChanged preference.\n")
    second = import_record(pg_control_path, import_sources(source, tier="general")[0])
    assert first["item_id"] == second["item_id"]
    assert second["status"] == "pending"
    assert "First preference" in read_profile(pg_control_path)["bundle"]
    act_on_proposal(
        pg_control_path,
        second["proposal_id"],
        "accept",
        actor=ProfileActor("operator", "private", True),
    )
    assert "Changed preference" in read_profile(pg_control_path)["bundle"]
    act_on_proposal(
        pg_control_path,
        second["proposal_id"],
        "revert",
        actor=ProfileActor("operator", "private", True),
    )
    assert "First preference" in read_profile(pg_control_path)["bundle"]


def test_parallel_import_has_no_duplicates(pg_control_path, tmp_path):
    source = tmp_path / "USER.md"
    source.write_text("# Rules\nAsk before deployment.\n")
    record = import_sources(source, tier="general")[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: import_record(pg_control_path, record), range(4))
        )
    assert sum(not result["unchanged"] for result in results) == 1
    with postgres_control_store(pg_control_path).connection() as con:
        assert con.execute("SELECT count(*) FROM profile_items").fetchone()[0] == 1


def test_directory_and_validation(tmp_path):
    (tmp_path / "USER.md").write_text("# Rules\nA rule")
    (tmp_path / "MEMORY.md").write_text("# Preferences\nA preference")
    assert len(import_sources(tmp_path)) == 2
    invalid = tmp_path / "invalid.txt"
    invalid.write_text("content")
    result = CliRunner().invoke(main, ["profile", "import", "--from", str(invalid)])
    assert result.exit_code != 0 and "markdown" in result.output
    result = CliRunner().invoke(
        main, ["profile", "import", "--from", str(tmp_path), "--tier", "invalid"]
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("tier", ["general", "trusted", "private"])
def test_sensitive_heading_fixture(tier):
    text = (
        Path(__file__).parent / "fixtures/profile/sensitive-headings.md"
    ).read_text()
    items = parse_markdown(text, tier=tier)
    assert len(items) == 25
    for item in items:
        assert item["tier"] == "private"
        assert any(r.startswith("sensitive_heading:") for r in item["tier_reasons"])


@pytest.mark.parametrize("tier", ["general", "trusted", "private"])
def test_body_pii_fixture(tier):
    text = (Path(__file__).parent / "fixtures/profile/body-pii.md").read_text()
    items = parse_markdown(text, tier=tier)
    expected = [
        "street_address",
        "ip_address",
        "ip_address",
        "currency_amount",
        "currency_amount",
        "phone_number",
        "phone_number",
        "email",
    ]
    assert len(items) == len(expected)
    for item, reason in zip(items, expected):
        assert item["tier"] == "private"
        assert f"body_pii:{reason}" in item["tier_reasons"]


def test_heading_keys_and_sibling_reset():
    items = parse_markdown(
        "# Rules\n## Tax\n### Detail\nSynthetic.\n## Style\nUse tables.", tier="general"
    )
    assert [i["key"] for i in items] == ["Rules / Tax / Detail", "Rules / Style"]
    assert [i["tier"] for i in items] == ["private", "general"]
    assert items[0]["tier_reasons"] == [
        "explicit_tier:general",
        "sensitive_heading:Tax",
    ]


def test_default_import_and_classification_provenance(pg_control_path, tmp_path):
    source = tmp_path / "USER.md"
    source.write_text("# Preferences\nContact synthetic@example.invalid")
    record = import_sources(source)[0]
    proposal = import_record(pg_control_path, record)
    assert proposal["status"] == "pending"
    assert "Contact" not in read_profile(pg_control_path)["bundle"]
    act_on_proposal(
        pg_control_path,
        proposal["proposal_id"],
        "accept",
        actor=ProfileActor("operator", "private", True),
    )
    with postgres_control_store(pg_control_path).connection() as con:
        provenance = con.execute(
            "SELECT provenance FROM profile_items WHERE item_id = ?",
            [proposal["item_id"]],
        ).fetchone()[0]
    assert provenance["import_classification"] == {
        "key": "Preferences",
        "tier_reasons": ["default_private", "body_pii:email"],
    }


@pytest.mark.parametrize(
    "text,reason",
    [
        ("+44 20 7946 0958", "phone_number"),
        ("+49 (30) 12345678", "phone_number"),
        ("INR 123.45", "currency_amount"),
        ("₹123", "currency_amount"),
        ("123 Example Avenue", "street_address"),
    ],
)
def test_additional_synthetic_pii_formats(text, reason):
    item = parse_markdown(f"# Preferences\nSynthetic: {text}", tier="trusted")[0]
    assert item["tier"] == "private"
    assert f"body_pii:{reason}" in item["tier_reasons"]


def test_explicit_private_reason():
    assert parse_markdown("# Rules\nUse tables", tier="private")[0]["tier_reasons"] == [
        "explicit_tier:private"
    ]
