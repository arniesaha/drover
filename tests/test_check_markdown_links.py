from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_markdown_links.py"


def run_checker(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(path) for path in paths)],
        capture_output=True,
        text=True,
    )


def test_checker_accepts_existing_local_links_and_skips_external_targets(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "[nested](nested/page.md#section)\n"
        "![image](images/diagram.png)\n"
        "[external](https://example.com/docs)\n"
        "[email](mailto:security@example.com)\n"
        "[anchor](#local-section)\n"
        "```md\n[example](missing-example.md)\n```\n"
    )
    (docs / "nested").mkdir()
    (docs / "nested" / "page.md").write_text("# Page\n")
    (docs / "images").mkdir()
    (docs / "images" / "diagram.png").write_bytes(b"png")

    result = run_checker(docs)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout


def test_checker_reports_missing_local_document_and_image(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text("[missing](missing.md)\n![image](images/missing.png)\n")

    result = run_checker(guide)

    assert result.returncode == 1
    assert "guide.md:1 -> missing.md" in result.stdout
    assert "guide.md:2 -> images/missing.png" in result.stdout
