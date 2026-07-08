# Nexus GCP → Local Migration Archive

> **Archived, non-discoverable skill content.** This file is intentionally stored outside `skills/` and has no skill frontmatter so shared agents do not load it for current Nexus work.
>
> **Deprecated historical reference. Do not execute these commands for current Nexus operations.**

Nexus moved off GCP/BigQuery on 2026-05-09. Current Nexus data lives in local DuckDB at `/Users/arnabmac/.nexus/nexus.duckdb`, with MCP at `http://Arnabs-Mac-mini.local:7077/mcp/`. Use the main `nexus` skill for current recall and `nexus-local-lakehouse` for direct read-only SQL.

This skill exists only to preserve migration history and avoid future confusion when old docs mention:

- GCP project: `nexus-context-engine-26`
- BigQuery dataset: `lakehouse`
- GCS bucket: `nexus-raw-logs-26`
- retired BigQuery tables such as `agent_events` and `spans`
- old GCP-era shippers/forwarders

## Historical context

The old stack exported agent events/spans to BigQuery and GCS. It was retired in favor of the local Mac Mini DuckDB+Parquet stack for lower cost, local-first operation, and better fit with personal/open-source agent harnesses.

## Safety rules

- Do not run `bq query` for current Nexus data.
- Do not activate GCP service accounts for current Nexus recall.
- Do not recreate BigQuery/GCS/Cloud SQL resources for Nexus unless explicitly asked to investigate the historical migration.
- Do not run destructive teardown commands from old notes (`bq rm`, `gsutil rm`, `gcloud sql instances delete`) unless Arnab explicitly asks for GCP archaeology and confirms scope.
- If an old doc says “lakehouse,” translate current operation to local DuckDB/Parquet unless it is clearly a historical migration note.

## Current replacement paths

- Current recall/handoff: MCP tools in the `nexus` skill.
- Current direct SQL/schema checks: `nexus-local-lakehouse`.
- Current ingestion/service ops: maintainer runbooks, not this archive.

## Migration lessons retained

- Large historical exports required date chunking and row-count verification.
- Source IDs could duplicate due to retries; current health should use canonical `dedup_key`.
- CSV exports were fragile around stderr/progress output and oversized JSON fields.
- Historical GCP data-quality notes are useful as background, not as current status.
