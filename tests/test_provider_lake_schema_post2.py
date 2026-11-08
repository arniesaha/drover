import pytest

from drover.server.lake.rebuild import PROVIDER_SCHEMA
from drover.server.lake.runtime import create_table

class Capture:
    def __init__(self): self.sql = []
    def execute(self, sql): self.sql.append(sql)

def test_provider_harnesses_is_explicit_list_schema():
    assert PROVIDER_SCHEMA["harnesses"] == "VARCHAR[]"
    con = Capture()
    create_table(con, "provider_usage_snapshots", {"harnesses": ["VARCHAR[]"][0]})
    assert "harnesses" in con.sql[0]

def test_declared_type_allowlist_remains_narrow():
    for forbidden in ("INTEGER[]", "VARCHAR[][]", "JSON", "VARCHAR; DROP TABLE x"):
        with pytest.raises(ValueError, match="invalid declared schema"):
            create_table(Capture(), "provider_usage_snapshots", {"field": forbidden})

def test_unknown_provider_input_column_is_not_declared():
    observed = {"snapshot_id", "harnesses", "unexpected_field"}
    assert observed - PROVIDER_SCHEMA.keys() == {"unexpected_field"}

def test_provider_harnesses_round_trip_and_extra_parquet_column_fails_closed(tmp_path):
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from drover.server.lake.rebuild import stage_relation
    from drover.server.lake.runtime import LakeError

    extracted = tmp_path / "input"
    path = extracted / "parquet/provider_usage_snapshots/usage.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"snapshot_id": "round-trip", "harnesses": ["claude", "codex"]}],
            schema=pa.schema([
                pa.field("snapshot_id", pa.string()),
                pa.field("harnesses", pa.list_(pa.string())),
            ]),
        ),
        path,
    )
    inventory = [{"path": str(path.relative_to(extracted))}]
    with duckdb.connect() as con:
        stage_relation(con, "provider_usage_snapshots", inventory, extracted)
        assert con.execute("SELECT harnesses FROM raw_provider_usage_snapshots").fetchone()[0] == ["claude", "codex"]

    bad = extracted / "parquet/provider_usage_snapshots/extra.parquet"
    pq.write_table(
        pa.Table.from_pylist([{"snapshot_id": "bad", "unexpected_field": "nope"}]),
        bad,
    )
    with duckdb.connect() as con:
        with pytest.raises(LakeError, match="rebuild_unknown_schema_columns"):
            stage_relation(con, "provider_usage_snapshots", [{"path": str(bad.relative_to(extracted))}], extracted)
