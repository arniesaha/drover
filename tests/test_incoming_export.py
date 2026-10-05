"""Synchronous incoming publication selects the sink and reports incomplete work."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drover.server.lake import lifecycle, task_projection


@pytest.mark.parametrize("backend", ["legacy", "ducklake"])
def test_incoming_export_selects_backend_and_checks_canonical_keys(
    monkeypatch, backend
):
    config = SimpleNamespace(
        analytics=SimpleNamespace(backend=backend), duckdb_path="isolated-test"
    )
    exporter = Mock()
    exporter.fence = object()
    legacy = Mock(return_value=exporter)
    lake = Mock(return_value=nullcontext(exporter))
    monkeypatch.setattr(lifecycle, "selected_exporter", legacy)
    monkeypatch.setattr(lifecycle, "LakeOutboxExporter", lake)
    monkeypatch.setattr(lifecycle, "lake_spec", lambda *_args, **_kwargs: "test-spec")
    monkeypatch.setattr(lifecycle, "catalog_identity", lambda _: "same-catalog")
    monkeypatch.setattr(lifecycle, "HistoryConnection", lambda _: nullcontext(Mock()))
    control = Mock()
    control.execute.return_value.fetchall.side_effect = [
        [("pending", None)],
        [("acknowledged", None)],
    ]
    monkeypatch.setattr(
        "drover.server.db.control_plane_connection", lambda _: nullcontext(control)
    )
    refresh = Mock()
    monkeypatch.setattr(task_projection, "refresh_if_provisioned", refresh)
    lifecycle.export_ingested_events(config, ["canonical-key", "canonical-key"])
    assert exporter.run_once.call_count == 2
    assert control.execute.call_args.args[1] == ["canonical-key"]
    if backend == "legacy":
        legacy.assert_called_once_with(config)
        lake.assert_not_called()
        exporter.run_once.assert_called_with(force_flush=True)
        refresh.assert_not_called()
    else:
        lake.assert_called_once_with(control_path="isolated-test", spec="test-spec")
        legacy.assert_not_called()
        exporter.run_once.assert_called_with()
        refresh.assert_called_once_with("isolated-test", lake_fence=exporter.fence)


@pytest.mark.parametrize("limits", [{"max_passes": 1}, {"timeout_seconds": 0}])
@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("rejected", "input exceeds 32 MiB")], "input exceeds 32 MiB"),
        ([("pending", None)], "events remain unacknowledged"),
        ([], "events remain unacknowledged"),
    ],
)
def test_incoming_export_does_not_report_incomplete_events_as_success(
    monkeypatch, rows, message, limits
):
    config = SimpleNamespace(
        analytics=SimpleNamespace(backend="legacy"), duckdb_path="isolated-test"
    )
    exporter = Mock()
    monkeypatch.setattr(lifecycle, "selected_exporter", lambda _: exporter)
    control = Mock()
    control.execute.return_value.fetchall.return_value = rows
    monkeypatch.setattr(
        "drover.server.db.control_plane_connection", lambda _: nullcontext(control)
    )
    with pytest.raises(RuntimeError, match=message):
        lifecycle.export_ingested_events(config, ["key"], **limits)
    exporter.run_once.assert_called_once_with(force_flush=True)
