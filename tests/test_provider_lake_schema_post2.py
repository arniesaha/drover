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
