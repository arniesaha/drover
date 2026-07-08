import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drover.parsers import parse_agentweave_trace

FIXTURE = Path(__file__).parent / "fixtures" / "agentweave_trace_sample.json"
OPENCLAW_CONTRACT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "agentweave_openclaw_trace_contract.json"
)


@pytest.fixture
def trace_dict():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def openclaw_contract_trace():
    return json.loads(OPENCLAW_CONTRACT_FIXTURE.read_text())


def test_parse_agentweave_openclaw_contract_extracts_harness_session_key_and_routing(
    openclaw_contract_trace,
):
    spans = parse_agentweave_trace(
        openclaw_contract_trace, raw_object_uri="file://fake"
    )

    root = next(s for s in spans if s["name"] == "agent.turn")
    llm = next(s for s in spans if s["name"] == "llm.call")
    tool = next(s for s in spans if s["name"] == "tool.call")

    assert root["harness"] == "openclaw"
    assert root["session_id"] == "018f-openclaw-main-0001"
    assert root["session_key"] == "agent:main:main"
    assert root["cwd"] == "/tmp/nexus-demo"
    assert root["repository"] == "https://github.com/example/nexus-demo.git"
    assert root["repo_owner"] == "example"
    assert root["repo_name"] == "nexus-demo"
    assert root["branch"] == "feature/demo"
    assert root["redaction_level"] == "preview"
    assert root["sensitivity"] == "unknown"

    assert llm["routing_provider"] == "fake-provider"
    assert llm["routing_model"] == "fake-model-small"
    assert llm["routing_reason"] == "low-latency-test-route"
    assert llm["redaction_level"] == "redacted"

    assert tool["session_id"] is None
    assert tool["session_key"] == "agent:main:main"
    assert tool["routing_provider"] == "fake-tool-provider"
    assert tool["routing_model"] == "fake-tool-model"
    assert tool["routing_reason"] == "tool-span-route"


def test_parser_derives_repo_from_safe_agentweave_cwd_without_changing_session() -> (
    None
):
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "agentweave-proxy"},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "0123456789abcdef0123456789abcdef",
                                "spanId": "0123456789abcdef",
                                "name": "agent.turn",
                                "startTimeUnixNano": "1770000000000000000",
                                "attributes": [
                                    {
                                        "key": "prov.harness",
                                        "value": {"stringValue": "openclaw"},
                                    },
                                    {
                                        "key": "prov.session.id",
                                        "value": {
                                            "stringValue": "openclaw-native-session"
                                        },
                                    },
                                    {
                                        "key": "prov.session.key",
                                        "value": {"stringValue": "openclaw-key"},
                                    },
                                    {
                                        "key": "prov.cwd",
                                        "value": {
                                            "stringValue": "/home/Arnab/dev/openclaw/plugins/cursor"
                                        },
                                    },
                                    {
                                        "key": "prov.project",
                                        "value": {"stringValue": "OpenClaw"},
                                    },
                                    {
                                        "key": "prov.agent.id",
                                        "value": {"stringValue": "openclaw-nas"},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    row = parse_agentweave_trace(trace, raw_object_uri="otlp://agentweave")[0]

    assert row["session_id"] == "openclaw-native-session"
    assert row["session_key"] == "openclaw-key"
    assert row["repo_owner"] == "arniesaha"
    assert row["repo_name"] == "openclaw"
    assert row["project"] == "OpenClaw"


def test_parser_leaves_mux_spans_unattributed_when_no_safe_path() -> None:
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "mux-router"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "abcdefabcdefabcdefabcdefabcdefab",
                                "spanId": "abcdefabcdefabcd",
                                "name": "mux.route",
                                "startTimeUnixNano": "1770000000000000000",
                                "attributes": [
                                    {
                                        "key": "prov.activity.type",
                                        "value": {"stringValue": "llm_call"},
                                    },
                                    {
                                        "key": "prov.agent.id",
                                        "value": {"stringValue": "nix-v1"},
                                    },
                                    {
                                        "key": "process.command",
                                        "value": {
                                            "stringValue": "/home/Arnab/clawd/projects/mux/dist/server.js"
                                        },
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    row = parse_agentweave_trace(trace, raw_object_uri="otlp://mux")[0]

    assert row["service_name"] == "mux-router"
    assert row.get("repo_owner") is None
    assert row.get("repo_name") is None
    assert row.get("project") is None


def test_session_key_never_overwrites_session_id_when_canonical_id_missing():
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
                                "name": "key-only",
                                "startTimeUnixNano": "1000000000",
                                "attributes": [
                                    {
                                        "key": "prov.session.key",
                                        "value": {"stringValue": "agent:main:main"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    row = parse_agentweave_trace(trace, raw_object_uri="file://fake")[0]

    assert row["session_id"] is None
    assert row["session_key"] == "agent:main:main"


def test_parse_agentweave_openclaw_contract_marks_preview_truncation():
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
                                "name": "long-preview",
                                "startTimeUnixNano": "1000000000",
                                "attributes": [
                                    {
                                        "key": "prov.llm.prompt_preview",
                                        "value": {"stringValue": "P" * 2500},
                                    },
                                    {
                                        "key": "prov.llm.response_preview",
                                        "value": {"stringValue": "R" * 1999},
                                    },
                                    {
                                        "key": "prov.tool.output_preview",
                                        "value": {"stringValue": "T" * 2100},
                                    },
                                    {
                                        "key": "prov.tool.input_preview",
                                        "value": {"stringValue": "I" * 2101},
                                    },
                                    {
                                        "key": "custom.tool_input_preview",
                                        "value": {"stringValue": "W" * 2102},
                                    },
                                    {
                                        "key": "custom.tool_output_preview",
                                        "value": {"stringValue": "O" * 2103},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    row = parse_agentweave_trace(trace, raw_object_uri="file://fake")[0]

    assert len(row["prompt_preview"]) == 2000
    assert row["preview_truncated"] is True
    assert row["preview_bytes"] == 2000
    assert (
        len(row["attributes_json"]["prov.llm.prompt_preview"].encode("utf-8")) <= 2000
    )
    assert (
        len(row["attributes_json"]["prov.llm.response_preview"].encode("utf-8")) <= 2000
    )
    assert (
        len(row["attributes_json"]["prov.tool.output_preview"].encode("utf-8")) <= 2000
    )
    assert (
        len(row["attributes_json"]["prov.tool.input_preview"].encode("utf-8")) <= 2000
    )
    assert (
        len(row["attributes_json"]["custom.tool_input_preview"].encode("utf-8")) <= 2000
    )
    assert (
        len(row["attributes_json"]["custom.tool_output_preview"].encode("utf-8"))
        <= 2000
    )


def test_preview_truncation_is_utf8_byte_aware_and_preserves_valid_strings():
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
                                "name": "utf8-preview",
                                "startTimeUnixNano": "1000000000",
                                "attributes": [
                                    {
                                        "key": "prov.llm.prompt_preview",
                                        "value": {"stringValue": "😀" * 600},
                                    },
                                    {
                                        "key": "prov.tool.output_preview",
                                        "value": {"stringValue": "🐍" * 600},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    row = parse_agentweave_trace(trace, raw_object_uri="file://fake")[0]

    assert row["preview_truncated"] is True
    assert len(row["prompt_preview"].encode("utf-8")) <= row["preview_bytes"]
    assert (
        len(row["attributes_json"]["prov.llm.prompt_preview"].encode("utf-8"))
        <= row["preview_bytes"]
    )
    assert (
        len(row["attributes_json"]["prov.tool.output_preview"].encode("utf-8"))
        <= row["preview_bytes"]
    )
    assert (
        row["prompt_preview"].encode("utf-8").decode("utf-8") == row["prompt_preview"]
    )
    assert (
        row["attributes_json"]["prov.tool.output_preview"]
        .encode("utf-8")
        .decode("utf-8")
        == row["attributes_json"]["prov.tool.output_preview"]
    )


def test_returns_one_dict_per_span(trace_dict):
    spans = parse_agentweave_trace(
        trace_dict, raw_object_uri="gs://b/agentweave/tempo/dt=2026-04-25/traces/x.json"
    )
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
    assert child["agent_id"] == "nas-claude"
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
    assert (
        child["attributes_json"]["prov.llm.prompt_preview"]
        == "Review this repo and propose..."
    )


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
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [
                                    {
                                        "key": "prov.llm.prompt_preview",
                                        "value": {"stringValue": "A" * 5000},
                                    }
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


def test_attribute_with_null_value_does_not_crash():
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
                                "attributes": [
                                    {"key": "weird", "value": None},
                                    {"key": "weird2"},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert len(spans) == 1
    assert spans[0]["attributes_json"]["weird"] is None


def test_session_id_falls_back_to_prov_session_id():
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
                                "attributes": [
                                    {
                                        "key": "prov.session.id",
                                        "value": {"stringValue": "my-session"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert spans[0]["session_id"] == "my-session"


def test_span_with_no_start_time_is_skipped(capsys):
    trace = {
        "batches": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "t",
                                "spanId": "good",
                                "name": "ok",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [],
                            },
                            {
                                "traceId": "t",
                                "spanId": "bad",
                                "name": "no-time",
                                "endTimeUnixNano": "2000000000",
                                "attributes": [],
                            },
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert [s["span_id"] for s in spans] == ["good"]
    err = capsys.readouterr().err.lower()
    assert "skipping" in err and "bad" in err


def test_int_attr_with_unexpected_string_value_becomes_none():
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
                                "attributes": [
                                    {
                                        "key": "prov.llm.prompt_tokens",
                                        "value": {"stringValue": "not-a-number"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert spans[0]["prompt_tokens"] is None


def test_base64_trace_and_span_ids_are_decoded_to_hex():
    """Tempo /api/traces returns OTel proto-JSON: bytes are base64-encoded."""
    trace = {
        "batches": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                # base64 of bytes 0x10..0x1f (16 bytes), 0x20..0x27 (8), 0x28..0x2f (8)
                                "traceId": "EBESExQVFhcYGRobHB0eHw==",
                                "spanId": "ICEiIyQlJic=",
                                "parentSpanId": "KCkqKywtLi8=",
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
    assert spans[0]["trace_id"] == "101112131415161718191a1b1c1d1e1f"
    assert spans[0]["span_id"] == "2021222324252627"
    assert spans[0]["parent_span_id"] == "28292a2b2c2d2e2f"


def test_hex_trace_id_passthrough_lowercased():
    """Tempo /api/search returns traceID as hex; the parser normalizes case."""
    trace = {
        "batches": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "7B15218059664734D870EC48B999E97F",
                                "spanId": "AABBCCDDEEFF1122",
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
    assert spans[0]["trace_id"] == "7b15218059664734d870ec48b999e97f"
    assert spans[0]["span_id"] == "aabbccddeeff1122"


def test_agent_alias_mapping():
    from drover.parsers import parse_agentweave_trace

    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "prov.agent.id",
                            "value": {"stringValue": "claude-nas"},
                        },
                        {"key": "service.name", "value": {"stringValue": "claude"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "YmFzZTY0dHJhY2VpZA==",
                                "spanId": "YmFzZTY0c3A=",
                                "name": "think",
                                "startTimeUnixNano": "1700000000000000000",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    rows = parse_agentweave_trace(trace, "gs://uri")
    assert len(rows) == 1
    # 'claude-nas' should be remapped to 'nas-claude'
    assert rows[0]["agent_id"] == "nas-claude"


def test_historical_mac_agent_alias_mapping():
    """Historical AgentWeave mac ids should ingest as agent_events ids.

    The same alias map is also used by read-time CLI filters via canonicalize(),
    so this guards parser/read-time drift for the macmini Claude identity.
    """
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
                                "attributes": [
                                    {
                                        "key": "prov.agent.id",
                                        "value": {"stringValue": "claude-code-mac"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    rows = parse_agentweave_trace(trace, "gs://uri")

    assert rows[0]["agent_id"] == "macmini-claude"


def test_was_associated_with_canonicalized():
    """prov.wasAssociatedWith holds the parent agent's id in AgentWeave's
    <tool>-<host> form; the parser canonicalizes it the same way agent_id is."""
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
                                "attributes": [
                                    {
                                        "key": "prov.agent.id",
                                        "value": {"stringValue": "nix-v1"},
                                    },
                                    {
                                        "key": "prov.wasAssociatedWith",
                                        "value": {"stringValue": "claude-code-nas"},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert spans[0]["agent_id"] == "nas-openclaw"
    assert spans[0]["associated_with"] == "nas-claude"


def test_was_associated_with_absent_is_none():
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
    assert spans[0]["associated_with"] is None


def test_agent_model_and_stop_reason_extracted():
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
                                "attributes": [
                                    {
                                        "key": "prov.agent.model",
                                        "value": {"stringValue": "claude-opus-4-7"},
                                    },
                                    {
                                        "key": "prov.llm.stop_reason",
                                        "value": {"stringValue": "tool_use"},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")
    assert spans[0]["agent_model"] == "claude-opus-4-7"
    assert spans[0]["stop_reason"] == "tool_use"


def test_project_label_falls_back_to_agentweave_header_attrs():
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
                                "attributes": [
                                    {
                                        "key": "x-agentweave-project",
                                        "value": {"stringValue": "healthos"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    spans = parse_agentweave_trace(trace, raw_object_uri="gs://x")

    assert spans[0]["project"] == "healthos"
