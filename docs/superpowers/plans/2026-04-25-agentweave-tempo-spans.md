# AgentWeave Tempo Spans Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull AgentWeave OTel spans from on-NAS Tempo into a new `lakehouse.spans` BigQuery table and expose them via `nexus trace search` / `nexus trace get`.

**Architecture:** A systemd user timer on ARNABSNAS runs a Python puller every 15 min against the Tempo HTTP API, writes raw trace JSON to local disk, then a shipper rsyncs to GCS. The existing `nexus-etl` Cloud Function dispatches on the `agentweave/tempo/traces/` prefix to a new parser that MERGEs into `lakehouse.spans`. The CLI gains a `trace` Click group reading BigQuery directly.

**Tech Stack:** Python 3.11, Click, google-cloud-bigquery, Terraform, systemd, gsutil, pytest. Spec: `docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md`.

---

## File Structure

**New files:**
- `iac/main.tf` (modify) — add `google_bigquery_table.spans` resource
- `tests/fixtures/agentweave_trace_sample.json` — synthetic OTel trace fixture
- `tests/test_agentweave_parser.py` — parser unit tests
- `tests/test_export_agentweave_tempo.py` — puller unit tests
- `scripts/export_agentweave_tempo.py` — Tempo puller
- `scripts/sync_agentweave_logs.sh` — GCS shipper
- `scripts/systemd/nexus-agentweave.service` — oneshot service unit
- `scripts/systemd/nexus-agentweave.timer` — 15-min timer unit
- `scripts/install_agentweave_shipper.sh` — idempotent installer

**Modified files:**
- `src/nexus/parsers.py` — add `parse_agentweave_trace`
- `src/nexus/cloud_function/nexus/parsers.py` — same content (kept in sync)
- `src/nexus/cloud_function/main.py` — dispatch by GCS prefix; AgentWeave branch writes to `lakehouse.spans` via MERGE
- `src/nexus/cloud_function/requirements.txt` — no new deps expected
- `src/nexus/cli.py` — add `trace` group with `search` and `get`
- `tests/test_cli.py` — tests for `trace search` and `trace get`
- `README.md` — Stage 5a status update

---

## Task 1: Define `lakehouse.spans` table in Terraform

**Files:**
- Modify: `iac/main.tf` (insert after the `agent_events` table block, around line 174)

- [ ] **Step 1: Add the Terraform resource**

Open `iac/main.tf`. After the `google_bigquery_table.agent_events` resource (ends around line 174), insert:

```hcl
resource "google_bigquery_table" "spans" {
  project    = var.gcp_project_id
  dataset_id = google_bigquery_dataset.lakehouse.dataset_id
  table_id   = "spans"

  deletion_protection = false

  time_partitioning {
    type  = "DAY"
    field = "start_time"
  }

  clustering = ["agent_id", "session_id", "trace_id"]

  schema = <<EOF
[
  {"name": "trace_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "span_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "parent_span_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "name", "type": "STRING", "mode": "NULLABLE"},
  {"name": "service_name", "type": "STRING", "mode": "NULLABLE"},
  {"name": "start_time", "type": "TIMESTAMP", "mode": "REQUIRED"},
  {"name": "end_time", "type": "TIMESTAMP", "mode": "NULLABLE"},
  {"name": "duration_ms", "type": "FLOAT64", "mode": "NULLABLE"},
  {"name": "activity_type", "type": "STRING", "mode": "NULLABLE"},
  {"name": "agent_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "agent_type", "type": "STRING", "mode": "NULLABLE"},
  {"name": "session_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "parent_session_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "project", "type": "STRING", "mode": "NULLABLE"},
  {"name": "task_label", "type": "STRING", "mode": "NULLABLE"},
  {"name": "llm_provider", "type": "STRING", "mode": "NULLABLE"},
  {"name": "llm_model", "type": "STRING", "mode": "NULLABLE"},
  {"name": "prompt_tokens", "type": "INT64", "mode": "NULLABLE"},
  {"name": "completion_tokens", "type": "INT64", "mode": "NULLABLE"},
  {"name": "total_tokens", "type": "INT64", "mode": "NULLABLE"},
  {"name": "cache_read_tokens", "type": "INT64", "mode": "NULLABLE"},
  {"name": "cache_write_tokens", "type": "INT64", "mode": "NULLABLE"},
  {"name": "cost_usd", "type": "FLOAT64", "mode": "NULLABLE"},
  {"name": "prompt_preview", "type": "STRING", "mode": "NULLABLE"},
  {"name": "response_preview", "type": "STRING", "mode": "NULLABLE"},
  {"name": "attributes_json", "type": "JSON", "mode": "NULLABLE"},
  {"name": "raw_object_uri", "type": "STRING", "mode": "NULLABLE"},
  {"name": "ingested_at", "type": "TIMESTAMP", "mode": "REQUIRED"}
]
EOF
}
```

- [ ] **Step 2: Validate Terraform syntax**

Run: `cd iac && terraform fmt -check && terraform validate`
Expected: no errors. Do NOT `terraform apply` from this plan — that is a manual step the user runs after review (see `iac/README.md` for the impersonation workflow).

- [ ] **Step 3: Commit**

```bash
git add iac/main.tf
git commit -m "iac: add lakehouse.spans BigQuery table for AgentWeave traces"
```

---

## Task 2: Add the trace fixture

**Files:**
- Create: `tests/fixtures/agentweave_trace_sample.json`

This fixture is a small, hand-crafted OTel trace shaped exactly like the response from `GET /api/traces/<id>?format=json`. Two spans (root `agent_turn` + child `llm_call`) so the parser exercises parent/child handling, resource-attribute inheritance, and LLM token/cost fields.

- [ ] **Step 1: Create the fixture file**

Create `tests/fixtures/agentweave_trace_sample.json` with exactly:

```json
{
  "batches": [
    {
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "agentweave-proxy"}},
          {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}}
        ]
      },
      "scopeSpans": [
        {
          "scope": {"name": "agentweave"},
          "spans": [
            {
              "traceId": "7b15218059664734d870ec48b999e97f",
              "spanId": "aaaaaaaaaaaaaaaa",
              "parentSpanId": "",
              "name": "agent.turn",
              "startTimeUnixNano": "1777141319000000000",
              "endTimeUnixNano": "1777141323988000000",
              "attributes": [
                {"key": "prov.activity.type", "value": {"stringValue": "agent_turn"}},
                {"key": "prov.agent.id", "value": {"stringValue": "claude-code-nas"}},
                {"key": "prov.agent.type", "value": {"stringValue": "main"}},
                {"key": "session.id", "value": {"stringValue": "claude-code-nas-main"}},
                {"key": "prov.project", "value": {"stringValue": "claude-code"}},
                {"key": "prov.task.label", "value": {"stringValue": "review repo"}}
              ]
            },
            {
              "traceId": "7b15218059664734d870ec48b999e97f",
              "spanId": "bbbbbbbbbbbbbbbb",
              "parentSpanId": "aaaaaaaaaaaaaaaa",
              "name": "llm.claude-opus-4-7",
              "startTimeUnixNano": "1777141319843328862",
              "endTimeUnixNano": "1777141323831328862",
              "attributes": [
                {"key": "prov.activity.type", "value": {"stringValue": "llm_call"}},
                {"key": "prov.agent.id", "value": {"stringValue": "claude-code-nas"}},
                {"key": "session.id", "value": {"stringValue": "claude-code-nas-main"}},
                {"key": "prov.llm.provider", "value": {"stringValue": "anthropic"}},
                {"key": "prov.llm.model", "value": {"stringValue": "claude-opus-4-7"}},
                {"key": "prov.llm.prompt_tokens", "value": {"intValue": "1234"}},
                {"key": "prov.llm.completion_tokens", "value": {"intValue": "567"}},
                {"key": "prov.llm.total_tokens", "value": {"intValue": "1801"}},
                {"key": "tokens.cache_read", "value": {"intValue": "800"}},
                {"key": "tokens.cache_write", "value": {"intValue": "100"}},
                {"key": "cost.usd", "value": {"doubleValue": 0.0234}},
                {"key": "prov.llm.prompt_preview", "value": {"stringValue": "Review this repo and propose..."}},
                {"key": "prov.llm.response_preview", "value": {"stringValue": "Here is a plan..."}}
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/agentweave_trace_sample.json
git commit -m "test(fixtures): add AgentWeave Tempo trace sample"
```

---

## Task 3: TDD `parse_agentweave_trace`

**Files:**
- Create: `tests/test_agentweave_parser.py`
- Modify: `src/nexus/parsers.py`
- Modify: `src/nexus/cloud_function/nexus/parsers.py` (mirror — must stay identical to the top-level copy, per existing convention verified by `diff`)

The parser converts a full Tempo trace JSON into a list of dicts ready for BigQuery insert. Resource attributes (e.g. `service.name`) are propagated onto every span. Timestamps come in as nanosecond-since-epoch strings.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agentweave_parser.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nexus.parsers import parse_agentweave_trace

FIXTURE = Path(__file__).parent / "fixtures" / "agentweave_trace_sample.json"


@pytest.fixture
def trace_dict():
    return json.loads(FIXTURE.read_text())


def test_returns_one_dict_per_span(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://b/agentweave/tempo/dt=2026-04-25/traces/x.json")
    assert len(spans) == 2


def test_root_span_has_null_parent(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    root = next(s for s in spans if s["name"] == "agent.turn")
    assert root["parent_span_id"] is None


def test_child_span_parent_set(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    child = next(s for s in spans if s["name"] == "llm.claude-opus-4-7")
    assert child["parent_span_id"] == "aaaaaaaaaaaaaaaa"


def test_resource_attribute_propagated(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    for s in spans:
        assert s["service_name"] == "agentweave-proxy"


def test_provenance_attributes_extracted(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    child = next(s for s in spans if s["name"] == "llm.claude-opus-4-7")
    assert child["agent_id"] == "claude-code-nas"
    assert child["session_id"] == "claude-code-nas-main"
    assert child["activity_type"] == "llm_call"
    assert child["llm_provider"] == "anthropic"
    assert child["llm_model"] == "claude-opus-4-7"


def test_token_and_cost_fields_extracted(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    child = next(s for s in spans if s["name"] == "llm.claude-opus-4-7")
    assert child["prompt_tokens"] == 1234
    assert child["completion_tokens"] == 567
    assert child["total_tokens"] == 1801
    assert child["cache_read_tokens"] == 800
    assert child["cache_write_tokens"] == 100
    assert child["cost_usd"] == pytest.approx(0.0234)


def test_timestamp_conversion(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    child = next(s for s in spans if s["name"] == "llm.claude-opus-4-7")
    assert isinstance(child["start_time"], datetime)
    assert child["start_time"].tzinfo == timezone.utc
    # 1777141319843328862 ns -> 2026-... in UTC
    assert child["start_time"].timestamp() == pytest.approx(1777141319.843328, rel=1e-6)
    assert child["duration_ms"] == pytest.approx(3988.0, abs=1.0)


def test_attributes_json_preserves_full_attribute_set(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://x")
    child = next(s for s in spans if s["name"] == "llm.claude-opus-4-7")
    assert "prov.llm.prompt_preview" in child["attributes_json"]
    assert child["attributes_json"]["prov.llm.prompt_preview"] == "Review this repo and propose..."


def test_previews_truncated_at_2000_chars():
    trace = {
        "batches": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "t",
                                "spanId": "s",
                                "name": "x",
                                "startTimeUnixNano": "0",
                                "endTimeUnixNano": "0",
                                "attributes": [
                                    {"key": "prov.llm.prompt_preview", "value": {"stringValue": "A" * 5000}}
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert len(spans[0]["prompt_preview"]) == 2000


def test_raw_object_uri_stamped(trace_dict):
    spans = parse_agentweave_trace(trace_dict, raw_object_uri="gs://b/p/x.json")
    assert all(s["raw_object_uri"] == "gs://b/p/x.json" for s in spans)


def test_missing_optional_fields_become_none():
    trace = {
        "batches": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "t",
                                "spanId": "s",
                                "name": "x",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    s = spans[0]
    assert s["agent_id"] is None
    assert s["session_id"] is None
    assert s["cost_usd"] is None
    assert s["prompt_preview"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agentweave_parser.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_agentweave_trace' from 'nexus.parsers'`.

- [ ] **Step 3: Implement the parser**

Append to `src/nexus/parsers.py`:

```python
from datetime import datetime, timezone
from typing import Any, Optional


_PROV_ATTR_MAP = {
    "prov.activity.type": "activity_type",
    "prov.agent.id": "agent_id",
    "prov.agent.type": "agent_type",
    "prov.parent.session.id": "parent_session_id",
    "prov.project": "project",
    "prov.task.label": "task_label",
    "prov.llm.provider": "llm_provider",
    "prov.llm.model": "llm_model",
}

_INT_ATTR_MAP = {
    "prov.llm.prompt_tokens": "prompt_tokens",
    "prov.llm.completion_tokens": "completion_tokens",
    "prov.llm.total_tokens": "total_tokens",
    "tokens.cache_read": "cache_read_tokens",
    "tokens.cache_write": "cache_write_tokens",
}


def _otel_attr_value(v: dict) -> Any:
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        # OTel JSON encodes ints as strings; coerce.
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    return None


def _flatten_attrs(attrs: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in attrs or []:
        k = a.get("key")
        if not k:
            continue
        out[k] = _otel_attr_value(a.get("value", {}))
    return out


def _ns_to_dt(ns: Optional[str]) -> Optional[datetime]:
    if ns in (None, "", "0"):
        return None
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _truncate(s: Optional[str], n: int = 2000) -> Optional[str]:
    if s is None:
        return None
    return s[:n]


def parse_agentweave_trace(
    trace: dict, raw_object_uri: str
) -> list[dict]:
    """Convert a Tempo trace JSON into rows for the lakehouse.spans table.

    `trace` is the parsed body of GET /api/traces/<id>?format=json:
        {"batches": [{"resource": {...}, "scopeSpans": [{"spans": [...]}]}]}
    """
    rows: list[dict] = []
    for batch in trace.get("batches", []) or []:
        resource_attrs = _flatten_attrs(
            batch.get("resource", {}).get("attributes", []) or []
        )
        service_name = resource_attrs.get("service.name")
        for ss in batch.get("scopeSpans", []) or batch.get(
            "instrumentationLibrarySpans", []
        ) or []:
            for span in ss.get("spans", []) or []:
                attrs = _flatten_attrs(span.get("attributes", []) or [])
                # Resource attrs are visible to consumers via attributes_json
                # for completeness; keep span-scoped only as the explicit map.
                merged_for_storage = {**resource_attrs, **attrs}

                row: dict = {
                    "trace_id": span.get("traceId"),
                    "span_id": span.get("spanId"),
                    "parent_span_id": span.get("parentSpanId") or None,
                    "name": span.get("name"),
                    "service_name": service_name,
                    "start_time": _ns_to_dt(span.get("startTimeUnixNano")),
                    "end_time": _ns_to_dt(span.get("endTimeUnixNano")),
                    "session_id": attrs.get("session.id")
                    or attrs.get("prov.session.id"),
                    "prompt_preview": _truncate(attrs.get("prov.llm.prompt_preview")),
                    "response_preview": _truncate(
                        attrs.get("prov.llm.response_preview")
                    ),
                    "cost_usd": attrs.get("cost.usd"),
                    "attributes_json": merged_for_storage,
                    "raw_object_uri": raw_object_uri,
                }
                for src, dst in _PROV_ATTR_MAP.items():
                    row[dst] = attrs.get(src)
                for src, dst in _INT_ATTR_MAP.items():
                    v = attrs.get(src)
                    row[dst] = int(v) if isinstance(v, (int, float, str)) and str(v).strip() else None
                if row["start_time"] and row["end_time"]:
                    row["duration_ms"] = (
                        row["end_time"] - row["start_time"]
                    ).total_seconds() * 1000.0
                else:
                    row["duration_ms"] = None
                rows.append(row)
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agentweave_parser.py -v`
Expected: PASS, all 10 tests green.

- [ ] **Step 5: Mirror to the cloud_function copy**

The convention in this repo is that `src/nexus/cloud_function/nexus/parsers.py` is byte-identical to `src/nexus/parsers.py` (verified by `diff`).

Run: `cp src/nexus/parsers.py src/nexus/cloud_function/nexus/parsers.py && diff src/nexus/parsers.py src/nexus/cloud_function/nexus/parsers.py`
Expected: no diff output.

- [ ] **Step 6: Run black**

Run: `black src/nexus/parsers.py src/nexus/cloud_function/nexus/parsers.py tests/test_agentweave_parser.py`
Expected: "All done!" or "1 file reformatted".

- [ ] **Step 7: Commit**

```bash
git add src/nexus/parsers.py src/nexus/cloud_function/nexus/parsers.py tests/test_agentweave_parser.py
git commit -m "feat(parsers): add parse_agentweave_trace for OTel Tempo traces"
```

---

## Task 4: Cloud Function ETL — dispatch and `lakehouse.spans` MERGE

**Files:**
- Modify: `src/nexus/cloud_function/main.py`

The existing `nexus_etl` handler reads a GCS event, calls the right parser by file path, and writes `AgentEvent` rows into BigQuery `agent_events` and pgvector. We add a new branch for files under `agentweave/tempo/traces/` that uses `parse_agentweave_trace`, MERGEs into `lakehouse.spans`, and skips pgvector.

- [ ] **Step 1: Read the current dispatch logic**

Run: `grep -n "file_name" src/nexus/cloud_function/main.py | head -30`
Identify the section that selects a parser based on the GCS object path. The new branch must be checked BEFORE any session parser so trace files don't get fed into the wrong parser.

- [ ] **Step 2: Add helpers and the AgentWeave branch**

In `src/nexus/cloud_function/main.py`, add `import io` near the top alongside the existing imports if it is not already imported (`grep -n '^import io' src/nexus/cloud_function/main.py` — add the line if missing). Then add at module scope (alongside the existing globals — the file already imports `json`, do not re-import):

```python
from nexus.parsers import parse_agentweave_trace

SPANS_TABLE_ID = f"{PROJECT_ID}.{DATASET_ID}.spans"


def _load_spans_via_merge(rows: list[dict], raw_object_uri: str) -> int:
    """MERGE rows into lakehouse.spans on (trace_id, span_id). Returns row count."""
    if not rows:
        return 0
    # Use a temporary load + MERGE so dedupe is atomic on (trace_id, span_id).
    staging_table = f"{PROJECT_ID}.{DATASET_ID}._spans_staging_{int(time.time()*1000)}"
    # Coerce attributes_json -> JSON string for BQ JSON column input.
    bq_rows = []
    for r in rows:
        rr = dict(r)
        rr["start_time"] = rr["start_time"].isoformat() if rr["start_time"] else None
        rr["end_time"] = rr["end_time"].isoformat() if rr["end_time"] else None
        rr["attributes_json"] = (
            json.dumps(rr["attributes_json"])
            if rr.get("attributes_json") is not None
            else None
        )
        rr["ingested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        bq_rows.append(rr)

    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        autodetect=False,
        schema=[
            bigquery.SchemaField("trace_id", "STRING", "REQUIRED"),
            bigquery.SchemaField("span_id", "STRING", "REQUIRED"),
            bigquery.SchemaField("parent_span_id", "STRING"),
            bigquery.SchemaField("name", "STRING"),
            bigquery.SchemaField("service_name", "STRING"),
            bigquery.SchemaField("start_time", "TIMESTAMP", "REQUIRED"),
            bigquery.SchemaField("end_time", "TIMESTAMP"),
            bigquery.SchemaField("duration_ms", "FLOAT64"),
            bigquery.SchemaField("activity_type", "STRING"),
            bigquery.SchemaField("agent_id", "STRING"),
            bigquery.SchemaField("agent_type", "STRING"),
            bigquery.SchemaField("session_id", "STRING"),
            bigquery.SchemaField("parent_session_id", "STRING"),
            bigquery.SchemaField("project", "STRING"),
            bigquery.SchemaField("task_label", "STRING"),
            bigquery.SchemaField("llm_provider", "STRING"),
            bigquery.SchemaField("llm_model", "STRING"),
            bigquery.SchemaField("prompt_tokens", "INT64"),
            bigquery.SchemaField("completion_tokens", "INT64"),
            bigquery.SchemaField("total_tokens", "INT64"),
            bigquery.SchemaField("cache_read_tokens", "INT64"),
            bigquery.SchemaField("cache_write_tokens", "INT64"),
            bigquery.SchemaField("cost_usd", "FLOAT64"),
            bigquery.SchemaField("prompt_preview", "STRING"),
            bigquery.SchemaField("response_preview", "STRING"),
            bigquery.SchemaField("attributes_json", "JSON"),
            bigquery.SchemaField("raw_object_uri", "STRING"),
            bigquery.SchemaField("ingested_at", "TIMESTAMP", "REQUIRED"),
        ],
    )
    nl_json = "\n".join(json.dumps(r) for r in bq_rows).encode("utf-8")
    load_job = bq_client.load_table_from_file(
        file_obj=io.BytesIO(nl_json),
        destination=staging_table,
        job_config=job_config,
    )
    load_job.result()

    merge_sql = f"""
    MERGE `{SPANS_TABLE_ID}` T
    USING (
      SELECT * FROM `{staging_table}`
      WHERE start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    ) S
    ON T.trace_id = S.trace_id AND T.span_id = S.span_id
    WHEN NOT MATCHED THEN INSERT ROW
    WHEN MATCHED THEN UPDATE SET
      parent_span_id = S.parent_span_id,
      name = S.name,
      service_name = S.service_name,
      start_time = S.start_time,
      end_time = S.end_time,
      duration_ms = S.duration_ms,
      activity_type = S.activity_type,
      agent_id = S.agent_id,
      agent_type = S.agent_type,
      session_id = S.session_id,
      parent_session_id = S.parent_session_id,
      project = S.project,
      task_label = S.task_label,
      llm_provider = S.llm_provider,
      llm_model = S.llm_model,
      prompt_tokens = S.prompt_tokens,
      completion_tokens = S.completion_tokens,
      total_tokens = S.total_tokens,
      cache_read_tokens = S.cache_read_tokens,
      cache_write_tokens = S.cache_write_tokens,
      cost_usd = S.cost_usd,
      prompt_preview = S.prompt_preview,
      response_preview = S.response_preview,
      attributes_json = S.attributes_json,
      raw_object_uri = S.raw_object_uri,
      ingested_at = S.ingested_at
    """
    bq_client.query(merge_sql).result()
    bq_client.delete_table(staging_table, not_found_ok=True)
    return len(bq_rows)
```

In the same file, find the file-prefix dispatch (just after the `if file_name.endswith(".processed") ...` early-return). Add this branch BEFORE the existing session-parser dispatch:

```python
    if file_name.startswith("agentweave/tempo/traces/") and file_name.endswith(".json"):
        # Download the trace JSON, parse, MERGE into lakehouse.spans.
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(file_name)
        with tempfile.NamedTemporaryFile(suffix=".json") as tf:
            blob.download_to_filename(tf.name)
            with open(tf.name, "r") as fh:
                trace_dict = json.load(fh)
        raw_uri = f"gs://{bucket_name}/{file_name}"
        rows = parse_agentweave_trace(trace_dict, raw_object_uri=raw_uri)
        n = _load_spans_via_merge(rows, raw_uri)
        print(f"AgentWeave trace {file_name}: merged {n} spans into spans table")
        return "OK"

    if file_name.startswith("agentweave/tempo/search-window-"):
        # Archive only — no parse. Audit value is the GCS object itself.
        print(f"AgentWeave search window archived: {file_name}")
        return "OK"
```

- [ ] **Step 3: Sanity-check syntax**

Run: `python -c "import ast; ast.parse(open('src/nexus/cloud_function/main.py').read())"`
Expected: no output (no syntax errors).

- [ ] **Step 4: Black**

Run: `black src/nexus/cloud_function/main.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cloud_function/main.py
git commit -m "feat(etl): route agentweave/tempo/* into lakehouse.spans MERGE"
```

---

## Task 5: TDD `nexus trace search`

**Files:**
- Modify: `src/nexus/cli.py`
- Modify: `tests/test_cli.py`

Match the style of the existing `search`/`replay` commands: BigQuery client + parameterized query + JSONL output by default.

- [ ] **Step 1: Write the failing test**

Open `tests/test_cli.py`. Read the file to see how the existing tests stub the BigQuery client (look for any `bigquery.Client` mock patterns; if none, the new tests will define one).

Append:

```python
from unittest.mock import MagicMock, patch
from click.testing import CliRunner
from nexus.cli import main as cli_main


def _row(d):
    r = MagicMock()
    for k, v in d.items():
        setattr(r, k, v)
    return r


@patch("nexus.cli.bigquery.Client")
def test_trace_search_default_jsonl(mock_client_cls):
    mock_client = mock_client_cls.return_value
    mock_client.query.return_value.result.return_value = [
        _row({
            "trace_id": "T1",
            "service_name": "agentweave-proxy",
            "name": "llm.claude-opus-4-7",
            "agent_id": "claude-code-nas",
            "session_id": "claude-code-nas-main",
            "start_time": "2026-04-25 12:00:00 UTC",
            "duration_ms": 3988.0,
            "total_tokens": 1801,
            "cost_usd": 0.0234,
        })
    ]
    runner = CliRunner()
    res = runner.invoke(cli_main, ["trace", "search", "--limit", "5"])
    assert res.exit_code == 0, res.output
    line = res.output.strip().splitlines()[0]
    import json as _j
    parsed = _j.loads(line)
    assert parsed["trace_id"] == "T1"
    assert parsed["agent_id"] == "claude-code-nas"


@patch("nexus.cli.bigquery.Client")
def test_trace_search_filters_passed_to_query(mock_client_cls):
    mock_client = mock_client_cls.return_value
    mock_client.query.return_value.result.return_value = []
    runner = CliRunner()
    res = runner.invoke(cli_main, [
        "trace", "search",
        "--agent", "claude-code-nas",
        "--session", "claude-code-nas-main",
        "--project", "claude-code",
        "--since", "24h",
    ])
    assert res.exit_code == 0
    args, kwargs = mock_client.query.call_args
    sql = args[0]
    job_config = kwargs.get("job_config")
    assert "agent_id = @agent" in sql
    assert "session_id = @session" in sql
    assert "project = @project" in sql
    assert "start_time >= TIMESTAMP_SUB" in sql
    param_names = {p.name for p in job_config.query_parameters}
    assert {"agent", "session", "project", "since_seconds", "limit"} <= param_names
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_cli.py -v -k trace_search`
Expected: FAIL — `No such command 'trace'`.

- [ ] **Step 3: Implement `trace search`**

In `src/nexus/cli.py`, add at the bottom (before `if __name__ == "__main__":`):

```python
import re

_SINCE_RE = re.compile(r"^(\d+)([smhd])$")


def _since_to_seconds(s: str) -> int:
    m = _SINCE_RE.match(s)
    if not m:
        raise click.BadParameter(f"--since must look like 24h, 30m, 7d (got {s!r})")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


@main.group()
def trace():
    """Query AgentWeave spans imported from Tempo."""


@trace.command("search")
@click.option("--agent", default=None, help="Filter by prov.agent.id")
@click.option("--session", "session_", default=None, help="Filter by session.id")
@click.option("--project", default=None, help="Filter by prov.project")
@click.option("--since", default="24h", help="Time window: 24h, 30m, 7d")
@click.option("--limit", default=20, type=int)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "jsonl"]),
    default="jsonl",
)
def trace_search(agent, session_, project, since, limit, fmt):
    """Search recent AgentWeave traces (one row per trace, root span used for display)."""
    project_id = _project_id()
    client = bigquery.Client(project=project_id)

    where = ["start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @since_seconds SECOND)"]
    params = [
        bigquery.ScalarQueryParameter(
            "since_seconds", "INT64", _since_to_seconds(since)
        ),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    if agent:
        where.append("agent_id = @agent")
        params.append(bigquery.ScalarQueryParameter("agent", "STRING", agent))
    if session_:
        where.append("session_id = @session")
        params.append(bigquery.ScalarQueryParameter("session", "STRING", session_))
    if project:
        where.append("project = @project")
        params.append(bigquery.ScalarQueryParameter("project", "STRING", project))

    sql = f"""
    WITH ranked AS (
      SELECT
        trace_id, service_name, name, agent_id, session_id,
        start_time, duration_ms, total_tokens, cost_usd, parent_span_id,
        ROW_NUMBER() OVER (
          PARTITION BY trace_id
          ORDER BY (CASE WHEN parent_span_id IS NULL THEN 0 ELSE 1 END), start_time
        ) AS rn
      FROM `{project_id}.lakehouse.spans`
      WHERE {' AND '.join(where)}
    )
    SELECT trace_id, service_name, name, agent_id, session_id,
           start_time, duration_ms, total_tokens, cost_usd
    FROM ranked
    WHERE rn = 1
    ORDER BY start_time DESC
    LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except Exception as e:
        raise click.ClickException(f"Error querying spans: {e}")

    items = [
        {
            "trace_id": r.trace_id,
            "service_name": r.service_name,
            "name": r.name,
            "agent_id": r.agent_id,
            "session_id": r.session_id,
            "start_time": str(r.start_time),
            "duration_ms": r.duration_ms,
            "total_tokens": r.total_tokens,
            "cost_usd": r.cost_usd,
        }
        for r in rows
    ]
    if fmt == "json":
        click.echo(json.dumps(items, indent=2))
    elif fmt == "text":
        for it in items:
            click.echo(
                f"{it['start_time']}  {it['trace_id']}  {it['agent_id'] or '-'}  "
                f"{it['name']}  {it['duration_ms']:.0f}ms  "
                f"toks={it['total_tokens']}  ${it['cost_usd']}"
            )
    else:  # jsonl
        for it in items:
            click.echo(json.dumps(it))
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cli.py -v -k trace_search`
Expected: PASS, both tests green.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli.py tests/test_cli.py
git commit -m "feat(cli): add 'nexus trace search' for AgentWeave spans"
```

---

## Task 6: TDD `nexus trace get`

**Files:**
- Modify: `src/nexus/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
@patch("nexus.cli.bigquery.Client")
def test_trace_get_returns_full_span_list_jsonl(mock_client_cls):
    mock_client = mock_client_cls.return_value
    mock_client.query.return_value.result.return_value = [
        _row({
            "trace_id": "T1", "span_id": "A", "parent_span_id": None,
            "name": "agent.turn", "service_name": "agentweave-proxy",
            "start_time": "2026-04-25 12:00:00 UTC", "end_time": "2026-04-25 12:00:04 UTC",
            "duration_ms": 4000.0, "agent_id": "claude-code-nas",
            "session_id": "claude-code-nas-main", "name_": None,
        }),
        _row({
            "trace_id": "T1", "span_id": "B", "parent_span_id": "A",
            "name": "llm.claude-opus-4-7", "service_name": "agentweave-proxy",
            "start_time": "2026-04-25 12:00:00 UTC", "end_time": "2026-04-25 12:00:04 UTC",
            "duration_ms": 3988.0, "agent_id": "claude-code-nas",
            "session_id": "claude-code-nas-main", "name_": None,
        }),
    ]
    runner = CliRunner()
    res = runner.invoke(cli_main, ["trace", "get", "T1"])
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 2
    import json as _j
    first = _j.loads(lines[0])
    assert first["span_id"] == "A"
    assert first["parent_span_id"] is None
    args, kwargs = mock_client.query.call_args
    sql = args[0]
    assert "trace_id = @trace_id" in sql


@patch("nexus.cli.bigquery.Client")
def test_trace_get_unknown_trace_exits_nonzero(mock_client_cls):
    mock_client = mock_client_cls.return_value
    mock_client.query.return_value.result.return_value = []
    runner = CliRunner()
    res = runner.invoke(cli_main, ["trace", "get", "missing"])
    assert res.exit_code != 0
    assert "no spans" in res.output.lower()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_cli.py -v -k trace_get`
Expected: FAIL — `No such command 'get'`.

- [ ] **Step 3: Implement `trace get`**

Append to `src/nexus/cli.py`, after the `trace_search` function:

```python
@trace.command("get")
@click.argument("trace_id")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "jsonl"]),
    default="jsonl",
)
def trace_get(trace_id, fmt):
    """Return the full span list for a single trace."""
    project_id = _project_id()
    client = bigquery.Client(project=project_id)
    sql = f"""
    SELECT trace_id, span_id, parent_span_id, name, service_name,
           start_time, end_time, duration_ms,
           agent_id, session_id
    FROM `{project_id}.lakehouse.spans`
    WHERE trace_id = @trace_id
    ORDER BY (CASE WHEN parent_span_id IS NULL THEN 0 ELSE 1 END), start_time
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("trace_id", "STRING", trace_id),
        ]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except Exception as e:
        raise click.ClickException(f"Error querying spans: {e}")

    if not rows:
        raise click.ClickException(f"No spans found for trace {trace_id}")

    items = [
        {
            "trace_id": r.trace_id,
            "span_id": r.span_id,
            "parent_span_id": r.parent_span_id,
            "name": r.name,
            "service_name": r.service_name,
            "start_time": str(r.start_time),
            "end_time": str(r.end_time) if r.end_time else None,
            "duration_ms": r.duration_ms,
            "agent_id": r.agent_id,
            "session_id": r.session_id,
        }
        for r in rows
    ]
    if fmt == "json":
        click.echo(json.dumps(items, indent=2))
    elif fmt == "text":
        for it in items:
            indent = "" if it["parent_span_id"] is None else "  "
            click.echo(
                f"{indent}{it['span_id']}  {it['name']}  "
                f"{it['duration_ms']:.0f}ms"
            )
    else:
        for it in items:
            click.echo(json.dumps(it))
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS, all CLI tests green (including pre-existing ones).

- [ ] **Step 5: Black**

Run: `black src/nexus/cli.py tests/test_cli.py`
Expected: clean or 1 file reformatted.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/cli.py tests/test_cli.py
git commit -m "feat(cli): add 'nexus trace get' for full span list"
```

---

## Task 7: TDD the Tempo puller

**Files:**
- Create: `tests/test_export_agentweave_tempo.py`
- Create: `scripts/export_agentweave_tempo.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_export_agentweave_tempo.py`:

```python
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make scripts/ importable as a package-less module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_agentweave_tempo as exp  # noqa: E402


def _resp(json_body):
    r = MagicMock()
    r.json.return_value = json_body
    r.raise_for_status.return_value = None
    return r


def test_run_writes_search_window_and_trace_files(tmp_path):
    search_body = {
        "traces": [{"traceID": "abc"}, {"traceID": "def"}]
    }
    trace_body = {
        "batches": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": []}]}]
    }

    def fake_get(url, *a, **kw):
        if "/api/search" in url:
            return _resp(search_body)
        if "/api/traces/" in url:
            return _resp(trace_body)
        raise AssertionError(f"unexpected url {url}")

    with patch("export_agentweave_tempo.requests.get", side_effect=fake_get):
        exp.run(
            tempo_base="http://t",
            output_dir=str(tmp_path),
            window_minutes=30,
            now_epoch=1_700_000_000,
        )

    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.json"))
    assert any(f.startswith("dt=") and "search-window-" in f for f in files)
    assert any(f.endswith("/traces/abc.json") for f in files)
    assert any(f.endswith("/traces/def.json") for f in files)


def test_existing_local_trace_files_are_not_refetched(tmp_path):
    search_body = {"traces": [{"traceID": "abc"}]}

    # Pre-create the trace file locally.
    dt_dir = tmp_path / "dt=2023-11-14"
    (dt_dir / "traces").mkdir(parents=True)
    (dt_dir / "traces" / "abc.json").write_text("{}")

    calls = {"trace": 0, "search": 0}

    def fake_get(url, *a, **kw):
        if "/api/search" in url:
            calls["search"] += 1
            return _resp(search_body)
        if "/api/traces/" in url:
            calls["trace"] += 1
            return _resp({"batches": []})
        raise AssertionError(url)

    with patch("export_agentweave_tempo.requests.get", side_effect=fake_get):
        exp.run(
            tempo_base="http://t",
            output_dir=str(tmp_path),
            window_minutes=30,
            now_epoch=1_700_000_000,
        )

    assert calls["search"] == 1
    assert calls["trace"] == 0  # already on disk


def test_tempo_unreachable_raises(tmp_path):
    import requests

    def fake_get(*a, **kw):
        raise requests.ConnectionError("nope")

    with patch("export_agentweave_tempo.requests.get", side_effect=fake_get):
        with pytest.raises(requests.ConnectionError):
            exp.run(
                tempo_base="http://t",
                output_dir=str(tmp_path),
                window_minutes=30,
                now_epoch=1_700_000_000,
            )
    assert not list(tmp_path.rglob("search-window-*.json"))


def test_per_trace_failure_does_not_fail_run(tmp_path):
    search_body = {"traces": [{"traceID": "good"}, {"traceID": "bad"}]}

    def fake_get(url, *a, **kw):
        if "/api/search" in url:
            return _resp(search_body)
        if "traces/bad" in url:
            r = MagicMock()
            r.raise_for_status.side_effect = Exception("boom")
            return r
        return _resp({"batches": []})

    with patch("export_agentweave_tempo.requests.get", side_effect=fake_get):
        exp.run(
            tempo_base="http://t",
            output_dir=str(tmp_path),
            window_minutes=30,
            now_epoch=1_700_000_000,
        )

    files = [p.name for p in tmp_path.rglob("*.json")]
    assert "good.json" in files
    assert "bad.json" not in files
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_export_agentweave_tempo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'export_agentweave_tempo'`.

- [ ] **Step 3: Implement the puller**

Create `scripts/export_agentweave_tempo.py`:

```python
#!/usr/bin/env python3
"""Pull AgentWeave OTel traces from Tempo and write raw JSON to disk.

Run by the systemd timer scripts/systemd/nexus-agentweave.timer every 15 min.
A separate shipper (sync_agentweave_logs.sh) rsyncs the output dir to GCS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

DEFAULT_TEMPO_BASE = os.environ.get("TEMPO_BASE", "http://192.168.1.70:31989")
DEFAULT_OUTPUT_DIR = os.environ.get(
    "NEXUS_TEMPO_EXPORT_DIR", "/var/lib/nexus/tempo-export"
)
DEFAULT_WINDOW_MIN = int(os.environ.get("NEXUS_TEMPO_WINDOW_MIN", "30"))
DEFAULT_SERVICES = os.environ.get(
    "NEXUS_TEMPO_SERVICES", "agentweave-proxy,mux-router"
).split(",")


def _build_query(services: list[str]) -> str:
    parts = [f'resource.service.name = "{s.strip()}"' for s in services if s.strip()]
    body = " || ".join(parts)
    return "{ " + body + " }"


def run(
    *,
    tempo_base: str = DEFAULT_TEMPO_BASE,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    window_minutes: int = DEFAULT_WINDOW_MIN,
    services: list[str] | None = None,
    now_epoch: int | None = None,
) -> None:
    services = services or DEFAULT_SERVICES
    now = now_epoch if now_epoch is not None else int(time.time())
    start = now - window_minutes * 60
    end = now
    dt = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%d")
    dt_dir = Path(output_dir) / f"dt={dt}"
    (dt_dir / "traces").mkdir(parents=True, exist_ok=True)

    q = quote(_build_query(services))
    search_url = f"{tempo_base}/api/search?q={q}&start={start}&end={end}&limit=1000"
    resp = requests.get(search_url, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    swin_path = dt_dir / f"search-window-{start_iso}-{end_iso}.json"
    swin_path.write_text(json.dumps(body))

    seen: set[str] = set()
    for t in body.get("traces", []) or []:
        tid = t.get("traceID")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        target = dt_dir / "traces" / f"{tid}.json"
        if target.exists():
            continue
        try:
            tr = requests.get(
                f"{tempo_base}/api/traces/{tid}?format=json", timeout=60
            )
            tr.raise_for_status()
        except Exception as e:
            print(f"[warn] failed to fetch trace {tid}: {e}", file=sys.stderr)
            continue
        target.write_text(json.dumps(tr.json()))

    print(
        f"[ok] window {start_iso}..{end_iso}: "
        f"{len(seen)} traces seen, dir={dt_dir}"
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tempo-base", default=DEFAULT_TEMPO_BASE)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MIN)
    args = p.parse_args()
    run(
        tempo_base=args.tempo_base,
        output_dir=args.output_dir,
        window_minutes=args.window_minutes,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Make it executable**

Run: `chmod +x scripts/export_agentweave_tempo.py`

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_export_agentweave_tempo.py -v`
Expected: PASS, all 4 tests green.

- [ ] **Step 6: Add `requests` to dev/runtime deps if needed**

Run: `grep -q '"requests"' pyproject.toml && echo present || echo missing`

If `missing`, edit `pyproject.toml` and add `"requests"` to the `dependencies` list (between `"click"` and the closing `]`).

Run: `pip install -e .` then `python -c "import requests; print(requests.__version__)"`
Expected: a version number printed.

- [ ] **Step 7: Black**

Run: `black scripts/export_agentweave_tempo.py tests/test_export_agentweave_tempo.py`

- [ ] **Step 8: Commit**

```bash
git add scripts/export_agentweave_tempo.py tests/test_export_agentweave_tempo.py pyproject.toml
git commit -m "feat(scripts): add Tempo puller for AgentWeave traces"
```

---

## Task 8: Add the GCS shipper

**Files:**
- Create: `scripts/sync_agentweave_logs.sh`

- [ ] **Step 1: Create the shipper**

Create `scripts/sync_agentweave_logs.sh`:

```bash
#!/usr/bin/env bash
# Shipper: rsync local AgentWeave Tempo exports to GCS raw archive.
# Pairs with scripts/export_agentweave_tempo.py.
#
# Run by the nexus-agentweave systemd timer (after the puller).
#
# Notes:
#   - No -d (issue #3): never delete from GCS just because local files aged out.
#   - GCS_BUCKET defaults to nexus-raw-logs-26.
set -euo pipefail

LOCAL_DIR="${NEXUS_TEMPO_EXPORT_DIR:-/var/lib/nexus/tempo-export}"
GCS_BUCKET="${NEXUS_RAW_BUCKET:-nexus-raw-logs-26}"
GCS_PREFIX="agentweave/tempo"

if [[ ! -d "$LOCAL_DIR" ]]; then
  echo "no local export dir ($LOCAL_DIR), nothing to ship"
  exit 0
fi

exec gsutil -m rsync -r "$LOCAL_DIR/" "gs://$GCS_BUCKET/$GCS_PREFIX/"
```

- [ ] **Step 2: Make it executable and lint**

Run: `chmod +x scripts/sync_agentweave_logs.sh && bash -n scripts/sync_agentweave_logs.sh`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add scripts/sync_agentweave_logs.sh
git commit -m "feat(scripts): add GCS shipper for AgentWeave Tempo exports"
```

---

## Task 9: systemd unit files and installer

**Files:**
- Create: `scripts/systemd/nexus-agentweave.service`
- Create: `scripts/systemd/nexus-agentweave.timer`
- Create: `scripts/install_agentweave_shipper.sh`

- [ ] **Step 1: Create the service unit**

Create `scripts/systemd/nexus-agentweave.service`:

```ini
[Unit]
Description=Nexus: pull AgentWeave Tempo traces and ship to GCS
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/env bash -c '%h/dev/nexus/scripts/export_agentweave_tempo.py && %h/dev/nexus/scripts/sync_agentweave_logs.sh'

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Create the timer unit**

Create `scripts/systemd/nexus-agentweave.timer`:

```ini
[Unit]
Description=Nexus: AgentWeave Tempo pull every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=60
Persistent=true
Unit=nexus-agentweave.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Create the installer**

Create `scripts/install_agentweave_shipper.sh`:

```bash
#!/usr/bin/env bash
# Idempotent installer for the AgentWeave Tempo shipper on this host.
# Usage: ./scripts/install_agentweave_shipper.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNIT_DIR" /var/lib/nexus/tempo-export 2>/dev/null || true

# /var/lib/nexus often isn't user-writable. Fall back to ~/.local/share/nexus.
if [[ ! -w /var/lib/nexus/tempo-export ]]; then
  EXPORT_DIR="${HOME}/.local/share/nexus/tempo-export"
  mkdir -p "$EXPORT_DIR"
  echo "[info] using $EXPORT_DIR (not /var/lib/nexus, not writable)"
fi

install -m 0644 "$REPO_ROOT/scripts/systemd/nexus-agentweave.service" "$UNIT_DIR/"
install -m 0644 "$REPO_ROOT/scripts/systemd/nexus-agentweave.timer" "$UNIT_DIR/"

systemctl --user daemon-reload
systemctl --user enable --now nexus-agentweave.timer
systemctl --user list-timers nexus-agentweave.timer --no-pager
```

- [ ] **Step 4: Make the installer executable**

Run: `chmod +x scripts/install_agentweave_shipper.sh && bash -n scripts/install_agentweave_shipper.sh`

- [ ] **Step 5: Commit**

```bash
git add scripts/systemd/nexus-agentweave.service scripts/systemd/nexus-agentweave.timer scripts/install_agentweave_shipper.sh
git commit -m "feat(systemd): add nexus-agentweave timer + installer"
```

---

## Task 10: Manual integration verification + README + follow-up issues

**Files:**
- Modify: `README.md`

This task verifies the pipeline end-to-end against live infrastructure. None of these steps are automated; they are checks the user (or operator) runs once.

- [ ] **Step 1: Apply Terraform**

Run from the user's IaC workflow (see `iac/README.md`):
```bash
cd iac
terraform plan
terraform apply
```
Expected: `google_bigquery_table.spans` is created.

- [ ] **Step 2: Deploy the updated Cloud Function**

Run: `bash scripts/deploy_etl.sh`
Expected: deploy succeeds; `gcloud functions describe nexus-etl --region=us-central1` shows the new revision.

- [ ] **Step 3: Run the puller manually once**

Run on the NAS:
```bash
python3 scripts/export_agentweave_tempo.py --output-dir /tmp/aw-test --window-minutes 60
ls /tmp/aw-test/dt=*/traces/ | head
```
Expected: at least one `<trace_id>.json` file present, and a `search-window-*.json`.

- [ ] **Step 4: Run the shipper manually**

Run:
```bash
NEXUS_TEMPO_EXPORT_DIR=/tmp/aw-test bash scripts/sync_agentweave_logs.sh
gsutil ls gs://nexus-raw-logs-26/agentweave/tempo/dt=*/traces/ | head
```
Expected: trace JSONs visible in GCS.

- [ ] **Step 5: Verify ETL → BigQuery**

Run: `bq query --use_legacy_sql=false 'SELECT COUNT(*) AS n FROM \`nexus-context-engine-26.lakehouse.spans\`'`
Expected: `n > 0` within ~1 minute of the GCS upload (Cloud Function trigger).

- [ ] **Step 6: Exercise the CLI**

Run:
```bash
nexus-cli trace search --since 24h --limit 5
nexus-cli trace get $(nexus-cli trace search --since 24h --limit 1 --format jsonl | jq -r '.trace_id')
```
Expected: JSONL output for both, full span list for the chosen trace.

- [ ] **Step 7: Install the timer**

Run on the NAS: `bash scripts/install_agentweave_shipper.sh`
Expected: `systemctl --user list-timers` shows `nexus-agentweave.timer` with a future `NEXT` time.

- [ ] **Step 8: Update README**

Edit `README.md`. In the "Stage 5" section, replace the existing `🔲` line about pulling AgentWeave traces with:

```markdown
- ✅ Pull AgentWeave traces from Tempo into `lakehouse.spans` via systemd timer + Cloud Function ETL
- ✅ `nexus trace search` and `nexus trace get` querying the spans table
- 🔲 Normalize `prov.*` / `gen_ai.*` spans into session graphs and decision records
```

- [ ] **Step 9: File follow-up GitHub issues**

Run `gh issue create` for each of the following. Use these exact titles and bodies:

```bash
gh issue create --title "Reconcile agent_id between AgentWeave spans and agent_events" --body "Lakehouse 'agent_events.agent_id' uses '<host>-<tool>' (e.g. 'nas-claude'); AgentWeave 'prov.agent.id' uses '<tool>-<host>' (e.g. 'claude-code-nas'). Joins return zero rows. Add an 'agent_aliases' mapping table or view that emits a canonical 'agent_key' for both pipelines. Source: docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md"

gh issue create --title "Derive session_links between Claude JSONL session UUIDs and AgentWeave logical sessions" --body "Claude Code emits per-file session UUIDs; AgentWeave emits long-lived logical session ids like 'claude-code-nas-main'. Build a derived 'session_links' table relating the two so trace+session joins work. Source: docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md"

gh issue create --title "nexus trace tail — streaming/recent-traces command" --body "Add 'nexus trace tail' that polls 'lakehouse.spans' for recent traces (configurable interval and filter). Useful for 'what just happened' debugging. Source: docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md (deferred from Stage 5a)."

gh issue create --title "nexus session graph <session-id> — span-tree reconstruction" --body "Reconstruct the parent/child span tree for a given session.id from 'lakehouse.spans'. Output as ASCII tree, JSON, or DOT. Source: docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md (deferred from Stage 5a)."

gh issue create --title "Embed selected AgentWeave span text into pgvector" --body "Feed prompt_preview / response_preview / task_label / root trace name from 'lakehouse.spans' into the existing pgvector serving index so semantic search covers traces. Source: docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md (deferred from Stage 5a)."

gh issue create --title "Decision extraction job — derive lakehouse.decisions from spans" --body "Use trace topology (root agent_turn -> child llm_call/tool_call) to derive 'lakehouse.decisions' rows: decision statement, rationale, alternatives, selected action, source trace/span ids. Source: docs/superpowers/specs/2026-04-25-agentweave-tempo-spans-design.md (deferred from Stage 5a)."
```

Expected: six issues created; capture their numbers in the commit message.

- [ ] **Step 10: Commit README update**

```bash
git add README.md
git commit -m "docs(readme): mark Stage 5a AgentWeave spans pipeline as live"
```

---

## Verification checklist (run before declaring done)

- [ ] `pytest tests/ -v` — all green (parser + CLI + puller + pre-existing).
- [ ] `black --check src tests scripts` — clean.
- [ ] `bq query 'SELECT COUNT(*) FROM nexus-context-engine-26.lakehouse.spans'` — non-zero.
- [ ] `nexus-cli trace search --since 24h --limit 5` — returns JSONL with at least one trace.
- [ ] `nexus-cli trace get <id>` — returns ≥1 span.
- [ ] `systemctl --user list-timers | grep nexus-agentweave` — timer present, `NEXT` populated.
- [ ] `gh issue list --state open` — six new follow-up issues filed.
