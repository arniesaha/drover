from pathlib import Path
import textwrap

import duckdb
from click.testing import CliRunner

from drover.server.__main__ import main


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(f"""\
            [paths]
            incoming_dir = "{tmp_path / 'incoming'}"
            parquet_dir  = "{tmp_path / 'parquet'}"
            duckdb_path  = "{tmp_path / 'drover.duckdb'}"
            processed_retention_days = 7

            [server]
            otlp_grpc_port = 14317
            mcp_http_port  = 17077

            [agent]
            agent_id     = "test"
            principal_id = "test"
            """))
    return cfg


def _write_bundle(bundle_dir: Path, *, brief_text: str | None = None) -> None:
    (bundle_dir / "briefs").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "decisions").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "briefs" / "project-alpha.md").write_text(
        brief_text or textwrap.dedent("""\
            ---
            id: project.alpha
            kind: project_brief
            title: Project Alpha Brief
            refs:
              - decision.alpha
            provenance:
              stage: generated
            owner: arnie
            ---
            Alpha needs a local-first import path.
            """)
    )
    (bundle_dir / "decisions" / "decision-alpha.yaml").write_text(textwrap.dedent("""\
            id: decision.alpha
            kind: decision
            title: Use curated context imports
            provenance:
              stage: edited
            status: approved
            body: |
              Store curated edits separately from raw events.
            """))


def test_context_validate_accepts_markdown_and_yaml_bundle(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir)

    res = runner.invoke(
        main, ["--config", str(cfg), "context", "validate", str(bundle_dir)]
    )

    assert res.exit_code == 0, res.output
    assert "context validate ok" in res.output
    assert "records=2" in res.output


def test_context_validate_rejects_missing_refs_and_secret_like_values(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    bundle_dir = tmp_path / "bundle"
    _write_bundle(
        bundle_dir,
        brief_text=textwrap.dedent("""\
            ---
            id: project.alpha
            kind: project_brief
            title: Project Alpha Brief
            refs:
              - decision.missing
            provenance:
              stage: generated
            api_key: gho_123456789012345678901234567890123456
            ---
            Alpha needs a local-first import path.
            """),
    )

    res = runner.invoke(
        main, ["--config", str(cfg), "context", "validate", str(bundle_dir)]
    )

    assert res.exit_code != 0
    assert "missing referenced id 'decision.missing'" in res.output
    assert "unsafe secret-like field root.metadata.api_key" in res.output


def test_context_diff_and_import_round_trip(tmp_path):
    runner = CliRunner()
    cfg = _make_config(tmp_path)
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir)

    diff_before = runner.invoke(
        main, ["--config", str(cfg), "context", "diff", str(bundle_dir)]
    )
    assert diff_before.exit_code == 0, diff_before.output
    assert "created=2" in diff_before.output
    assert "updated=0" in diff_before.output

    dry_run = runner.invoke(
        main, ["--config", str(cfg), "context", "import", str(bundle_dir)]
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "context import (dry-run)" in dry_run.output
    assert "applied=0 provenance_rows=0" in dry_run.output

    applied = runner.invoke(
        main, ["--config", str(cfg), "context", "import", str(bundle_dir), "--apply"]
    )
    assert applied.exit_code == 0, applied.output
    assert "applied=2 provenance_rows=4" in applied.output

    (bundle_dir / "briefs" / "project-alpha.md").write_text(textwrap.dedent("""\
            ---
            id: project.alpha
            kind: project_brief
            title: Project Alpha Brief v2
            refs:
              - decision.alpha
            provenance:
              stage: edited
            owner: arnie
            ---
            Alpha now includes a reviewable diff flow.
            """))

    diff_after = runner.invoke(
        main, ["--config", str(cfg), "context", "diff", str(bundle_dir)]
    )
    assert diff_after.exit_code == 0, diff_after.output
    assert "created=0" in diff_after.output
    assert "updated=1" in diff_after.output
    assert "unchanged=1" in diff_after.output
    assert "changed=content_md,metadata,source_stage,title" in diff_after.output

    reapplied = runner.invoke(
        main, ["--config", str(cfg), "context", "import", str(bundle_dir), "--apply"]
    )
    assert reapplied.exit_code == 0, reapplied.output
    assert "applied=1 provenance_rows=2" in reapplied.output

    con = duckdb.connect(str(tmp_path / "drover.duckdb"))
    try:
        assert (
            con.execute("SELECT count(*) FROM curated_context_records").fetchone()[0]
            == 2
        )
        assert (
            con.execute("SELECT count(*) FROM curated_context_provenance").fetchone()[0]
            == 6
        )
        kinds = {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT event_kind FROM curated_context_provenance"
            ).fetchall()
        }
        assert kinds == {"generated", "edited", "imported"}
    finally:
        con.close()
