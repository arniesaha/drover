"""The endpoint the fleet actually polls must not recompute on every call.

`/harness/hosts` asks for `include_sessions=False`. The render cache only
covered the full variant, on the stated assumption that partial renders are
rare -- and this is the most-polled endpoint on the hub. Measured on the live
hub over ~24h: 11,533 of 13,986 client disconnects were on `/harness/hosts`,
and it answered in 7-9ms every call while `/harness`, returning six times more
data, answered from cache in 0.5ms.

The cost is not the query. Every uncached render opens a DuckDB connection
under the registry's connect lock, so a poll queues behind whatever writer
holds it -- one such call was measured at over 45 seconds while harnessd was
ingesting.
"""

from __future__ import annotations

from drover.schema import bootstrap
from drover.server.metrics import MetricsCollector


def _collector(tmp_path) -> MetricsCollector:
    duckdb_path = tmp_path / "drover.duckdb"
    bootstrap(parquet_dir=tmp_path / "parquet", duckdb_path=duckdb_path)
    return MetricsCollector(
        duckdb_path=duckdb_path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )


def _counting(collector) -> list[dict]:
    """Record the kwargs of every snapshot the collector actually computes."""
    calls: list[dict] = []
    original = collector.harness_snapshot

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    collector.harness_snapshot = counted  # type: ignore[method-assign]
    return calls


def test_the_hosts_only_render_is_cached(tmp_path):
    collector = _collector(tmp_path)
    calls = _counting(collector)

    first = collector.render_harness_json(include_sessions=False)
    second = collector.render_harness_json(include_sessions=False)

    assert first == second
    assert len(calls) == 1, "the polled endpoint recomputed instead of caching"


def test_each_variant_is_computed_for_itself(tmp_path):
    """A cached hosts-only render must never be served as the full one.

    Asserted on what was computed rather than on the two bodies: with an empty
    fleet both variants render the same bytes, so comparing them would pass
    whether or not the key separated them.
    """
    collector = _collector(tmp_path)
    calls = _counting(collector)

    collector.render_harness_json(include_sessions=False)
    collector.render_harness_json()

    assert [c["include_sessions"] for c in calls] == [False, True]


def test_a_non_default_archived_cap_is_still_not_cached(tmp_path):
    """Caching a caller-supplied cap would let any client grow the cache.

    It would also be wrong: serving the cached default to someone who asked
    for 100 hands back the default number of sessions.
    """
    collector = _collector(tmp_path)
    calls = _counting(collector)

    collector.render_harness_json(archived_limit=100)
    collector.render_harness_json(archived_limit=100)

    assert len(calls) == 2


def test_invalidation_clears_every_variant(tmp_path):
    """A session action invalidates the cache; a stale variant must not survive."""
    collector = _collector(tmp_path)
    calls = _counting(collector)

    collector.render_harness_json(include_sessions=False)
    collector.invalidate_harness_cache()
    collector.render_harness_json(include_sessions=False)

    assert len(calls) == 2


def test_a_cached_variant_expires_with_the_ttl(tmp_path):
    """The staleness the cache buys is bounded, and this pins the bound.

    Host status is what the fleet view renders, so it matters that a stale
    answer cannot outlive the TTL. `/harness` has always carried the same
    bound on the same field.
    """
    collector = _collector(tmp_path)
    collector.harness_ttl_seconds = 0.0
    calls = _counting(collector)

    collector.render_harness_json(include_sessions=False)
    collector.render_harness_json(include_sessions=False)

    assert len(calls) == 2, "a zero TTL must not serve a cached answer"
