"""Release notes lead with the curated CHANGELOG section for the tag."""

import pytest
from changelog_section import changelog_section, main

CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-11-20

### Fixed
- The thing.

## [1.1.0] - 2026-11-01

### Added
- Older thing.

[1.2.0]: https://example.com/compare
"""


def test_extracts_only_the_requested_version():
    assert changelog_section(CHANGELOG, "1.2.0") == "### Fixed\n- The thing.\n"


def test_last_section_drops_file_level_link_references():
    assert changelog_section(CHANGELOG, "1.1.0") == "### Added\n- Older thing.\n"


@pytest.mark.parametrize("version", ["9.9.9", "Unreleased", "1.2"])
def test_missing_or_empty_sections_fail(version):
    with pytest.raises(ValueError):
        changelog_section(CHANGELOG, version)


def test_cli_accepts_a_tag(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG)
    assert main(["changelog_section.py", "v1.2.0", str(path)]) == 0
    assert capsys.readouterr().out == "### Fixed\n- The thing.\n"
    assert main(["changelog_section.py", "v3.0.0", str(path)]) == 1
