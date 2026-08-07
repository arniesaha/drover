---
name: drover-local-lakehouse
description: Use when Drover MCP tools are insufficient and an agent needs read-only DuckDB or Parquet schema inspection, queue diagnostics, or direct context-store data-quality queries.
---

# Drover Local DuckDB and Parquet Reference

## Overview

This is the advanced direct-query companion to the `drover` skill. Use MCP for
ordinary recall and handoff. Use this reference only for schema checks, bounded
quality audits, or SQL diagnostics.

## Locations

- Live database: `~/.drover/drover.duckdb`
- Parquet lake: `~/.drover/parquet/`
- Canonical schema: `src/drover/schema.py`
- Server readers: `src/drover/server/mcp/tools.py`

Open the live database read-only:

```python
from pathlib import Path
import duckdb

database = Path("~/.drover/drover.duckdb").expanduser()
connection = duckdb.connect(str(database), read_only=True)
```

## High-signal probes

Freshness:

```sql
SELECT agent_id, COUNT(*) AS events,
       MAX(TRY_CAST(timestamp AS TIMESTAMP)) AS last_seen
FROM agent_events
WHERE TRY_CAST(timestamp AS TIMESTAMP) > now() - INTERVAL 24 HOUR
GROUP BY 1 ORDER BY last_seen DESC;
```

Derived queues:

```sql
SELECT 'summarize' AS queue, status, COUNT(*) FROM summarize_jobs GROUP BY 1,2
UNION ALL
SELECT 'embed', status, COUNT(*) FROM embed_jobs GROUP BY 1,2
UNION ALL
SELECT 'span_embed', status, COUNT(*) FROM span_embed_jobs GROUP BY 1,2;
```

Canonical deduplication:

```sql
SELECT COUNT(*) - COUNT(DISTINCT dedup_key) AS duplicate_dedup_keys
FROM agent_events
WHERE dedup_key IS NOT NULL;
```

Recent span linkability:

```sql
SELECT COUNT(*) AS spans,
       MAX(TRY_CAST(start_time AS TIMESTAMPTZ)) AS latest_span,
       COUNT(*) FILTER (WHERE session_id IS NULL OR session_id = '') AS missing_session,
       COUNT(*) FILTER (WHERE project IS NULL OR project = '') AS missing_project
FROM spans
WHERE TRY_CAST(start_time AS TIMESTAMPTZ) > now() - INTERVAL 24 HOUR;
```

## Schema pitfalls

- `agent_events.timestamp` may require `TRY_CAST`.
- `spans` uses `start_time` or `date`, not `timestamp`.
- `session_summaries` does not directly own repository attribution.
- Source IDs can duplicate historically; `dedup_key` is canonical.
- Summary and embedding queues can lag independently of ingestion.
- Historical `nexus.*` fields may remain in stored records; do not rewrite them
  during a read-only audit.

## Reporting contract

Report ingestion freshness, rollup consistency, derived queues, deduplication,
attribution coverage, and span linkability separately. A warning on one surface
does not justify calling the entire context store unhealthy or mutating it.

## Common mistakes

| Mistake | Correction |
| --- | --- |
| Connecting read-write | Always use `read_only=True`. |
| Scanning all Parquet partitions | Bound reads by date first. |
| Using an old checkout database | Use `~/.drover/drover.duckdb`. |
| Repairing while diagnosing | Report evidence; obtain explicit repair scope. |
