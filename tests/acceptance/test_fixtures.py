"""Fixture self-tests: determinism, session-size distribution, big session > 8 MiB, and prod_shaped restore."""

from __future__ import annotations

import hashlib
import statistics
from pathlib import Path

import duckdb
from lake_generator import BIG_SESSION_ID, generate_lake

from drover.server.control_store import postgres_control_store


def _lake_content_hash(lake_dir: Path) -> str:
    """Compute a deterministic hash over all events in the lakehouse."""
    digest = hashlib.sha256()
    for file in sorted(lake_dir.rglob("*.parquet")):
        digest.update(str(file.relative_to(lake_dir)).encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def test_lake_generator_determinism(tmp_path: Path):
    """Same seed produces identical content hash; different seed produces different hash."""
    dir_a = tmp_path / "lake_a"
    dir_b = tmp_path / "lake_b"
    dir_c = tmp_path / "lake_c"

    generate_lake(dir_a, seed=42, scale="small")
    generate_lake(dir_b, seed=42, scale="small")
    generate_lake(dir_c, seed=43, scale="small")

    hash_a = _lake_content_hash(dir_a)
    hash_b = _lake_content_hash(dir_b)
    hash_c = _lake_content_hash(dir_c)

    assert hash_a == hash_b, "Identical seed must produce identical lake hash"
    assert hash_a != hash_c, "Different seeds must produce different lake hashes"


def test_lake_generator_size_distribution(small_lake: Path):
    """Session sizes follow a realistic long tail across 120 days."""
    con = duckdb.connect()
    try:
        parquet_glob = str(small_lake / "agent_events" / "**" / "*.parquet")

        # Verify 120 days of date= partitions
        dates = [
            r[0]
            for r in con.execute(
                f"SELECT DISTINCT date FROM read_parquet('{parquet_glob}', hive_partitioning=true) ORDER BY date"
            ).fetchall()
        ]
        assert len(dates) == 120, f"Expected 120 date partitions, got {len(dates)}"

        # Verify session size distribution
        session_sizes = [r[1] for r in con.execute(f"""
                SELECT session_id, count(*) as event_count
                FROM read_parquet('{parquet_glob}', hive_partitioning=true)
                GROUP BY session_id
                ORDER BY event_count DESC
                """).fetchall()]

        # The big session is at index 0
        assert session_sizes[0] == 40_000

        # Remaining non-big sessions
        other_sizes = session_sizes[1:]
        assert len(other_sizes) >= 100, "Expected at least 100 non-big sessions"
        median_size = statistics.median(other_sizes)
        mean_size = statistics.mean(other_sizes)
        p90_size = statistics.quantiles(other_sizes, n=10)[8]

        # Long-tail properties: low median, moderate mean, long tail
        assert median_size < 50, f"Expected median < 50, got {median_size}"
        assert median_size <= mean_size, "Long tail implies mean >= median"
        assert max(other_sizes) >= 100, "Long tail should contain larger sessions"
        assert p90_size < max(other_sizes), "90th percentile should be well below max"
    finally:
        con.close()


def test_lake_generator_big_session_exists_and_exceeds_8mib(small_lake: Path):
    """The 40k-event session exists and has a raw payload > 8 MiB."""
    con = duckdb.connect()
    try:
        parquet_glob = str(small_lake / "agent_events" / "**" / "*.parquet")
        res = con.execute(f"""
            SELECT
              count(*) AS event_count,
              sum(length(raw_data)) AS raw_bytes
            FROM read_parquet('{parquet_glob}', hive_partitioning=true)
            WHERE session_id = '{BIG_SESSION_ID}'
            """).fetchone()

        event_count, raw_bytes = res
        assert event_count == 40_000, f"Expected 40,000 events, got {event_count}"
        assert raw_bytes > 8 * 1024 * 1024, (
            f"Expected raw payload > 8 MiB (8,388,608 bytes), got {raw_bytes} bytes "
            f"({raw_bytes / 1024**2:.2f} MiB)"
        )
    finally:
        con.close()


def test_prod_shaped_fixture_state(prod_shaped, postgres_dsn):
    """prod_shaped is restored from committed dump, not freshly migrated."""
    store = postgres_control_store(prod_shaped)
    with store.connection() as con:
        # Check migrations table has versions 1..11 from dump
        versions = [
            r[0]
            for r in con.execute(
                "SELECT version FROM control_schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == list(range(1, 12))

        # Check control_store_initialization row exists from dump
        init_rows = con.execute(
            "SELECT singleton, state, mode FROM control_store_initialization"
        ).fetchall()
        assert len(init_rows) == 1
        assert init_rows[0] == (True, "ready", "import")

        # Check total table count in schema is 31
        schema_name = getattr(prod_shaped, "schema", "drover_control")
        table_count = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_type = 'BASE TABLE'",
            [schema_name],
        ).fetchone()[0]
        assert table_count == 31


def test_lake_payload_distribution(small_lake):
    """Distinct text/tool payloads have a long tail and resist extreme compression."""
    files = list((small_lake / "agent_events").rglob("*.parquet"))
    with duckdb.connect() as con:
        count, median, p99, unique = con.execute(
            "SELECT count(*), quantile_cont(length(raw_data), .5), "
            "quantile_cont(length(raw_data), .99), count(DISTINCT raw_data) "
            "FROM read_parquet(?, hive_partitioning=true)",
            [str(small_lake / "agent_events/**/*.parquet")],
        ).fetchone()
    assert count == 50_000 and unique == count
    assert median > 800 and p99 > 3 * median
    # The 5M target is 200–400 B/event; allow extra small-lake partition overhead.
    assert 200 <= sum(p.stat().st_size for p in files) / count <= 600


def test_lake_cache_identity_and_reuse(tmp_path, monkeypatch):
    import lake_generator as generator

    calls = []
    original = generator.generate_lake

    def generate(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(generator, "generate_lake", generate)
    first = generator.get_cached_lake(seed=79, cache_dir=tmp_path / "one")
    assert generator.get_cached_lake(seed=79, cache_dir=tmp_path / "one") == first
    generator._MEM_CACHE.clear()
    assert generator.get_cached_lake(seed=79, cache_dir=tmp_path / "one") == first
    second = generator.get_cached_lake(seed=79, cache_dir=tmp_path / "two")
    assert second != first and len(calls) == 2
    assert generator.compute_cache_key(
        79, "small", "next"
    ) != generator.compute_cache_key(79, "small")
