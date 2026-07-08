#!/usr/bin/env python3
"""Backfill prompt_preview and response_preview for spans with double-encoded attributes_json.

Background (issue #33)
-----------------------
Rows ingested before the ETL was stabilised have ``attributes_json`` stored as a
double-encoded JSON *string* (BigQuery JSON_TYPE = 'string') instead of a JSON
*object* (JSON_TYPE = 'object').  This means:

    JSON_VALUE(attributes_json, '$.\"prov.llm.prompt_preview\"')

…returns NULL for those rows even though the preview text IS present inside the
serialised string.

This script issues a single BigQuery UPDATE that:
1. Targets rows where JSON_TYPE(attributes_json) = 'string' AND
   prompt_preview IS NULL but the double-decoded JSON contains the key.
2. Extracts the value via PARSE_JSON(JSON_VALUE(attributes_json)).
3. Sets prompt_preview / response_preview (truncated to 500 chars).

Run
---
    export GCP_PROJECT=nexus-context-engine-26
    python scripts/backfill_prompt_preview.py [--dry-run] [--project PROJECT_ID]

Requirements
------------
    pip install google-cloud-bigquery
"""

import argparse
import sys

try:
    from google.cloud import bigquery
except ImportError:
    print("ERROR: google-cloud-bigquery not installed. Run: pip install google-cloud-bigquery", file=sys.stderr)
    sys.exit(1)


BACKFILL_SQL = """
UPDATE `{project}.lakehouse.spans`
SET
  prompt_preview   = SUBSTR(
    JSON_VALUE(PARSE_JSON(JSON_VALUE(attributes_json)), '$.\"prov.llm.prompt_preview\"'),
    1, 500
  ),
  response_preview = SUBSTR(
    JSON_VALUE(PARSE_JSON(JSON_VALUE(attributes_json)), '$.\"prov.llm.response_preview\"'),
    1, 500
  )
WHERE
  -- Only rows where attributes_json is a double-encoded string
  JSON_TYPE(attributes_json) = 'string'
  -- At least one preview field is missing
  AND (prompt_preview IS NULL OR response_preview IS NULL)
  -- And the underlying JSON string actually contains at least one preview key
  AND (
    REGEXP_CONTAINS(JSON_VALUE(attributes_json), r'prov\\.llm\\.prompt_preview')
    OR
    REGEXP_CONTAINS(JSON_VALUE(attributes_json), r'prov\\.llm\\.response_preview')
  )
"""

DRY_RUN_SQL = """
SELECT
  COUNT(*) AS rows_to_update,
  COUNTIF(
    REGEXP_CONTAINS(JSON_VALUE(attributes_json), r'prov\\.llm\\.prompt_preview')
  ) AS has_prompt_preview,
  COUNTIF(
    REGEXP_CONTAINS(JSON_VALUE(attributes_json), r'prov\\.llm\\.response_preview')
  ) AS has_response_preview
FROM `{project}.lakehouse.spans`
WHERE
  JSON_TYPE(attributes_json) = 'string'
  AND (prompt_preview IS NULL OR response_preview IS NULL)
  AND (
    REGEXP_CONTAINS(JSON_VALUE(attributes_json), r'prov\\.llm\\.prompt_preview')
    OR
    REGEXP_CONTAINS(JSON_VALUE(attributes_json), r'prov\\.llm\\.response_preview')
  )
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="nexus-context-engine-26",
                        help="GCP project ID (default: nexus-context-engine-26)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show how many rows would be updated without modifying data")
    args = parser.parse_args()

    client = bigquery.Client(project=args.project)

    if args.dry_run:
        sql = DRY_RUN_SQL.format(project=args.project)
        print(f"[DRY RUN] Counting rows that would be updated in {args.project}.lakehouse.spans …")
        job = client.query(sql)
        for row in job.result():
            print(f"  rows_to_update      : {row.rows_to_update:,}")
            print(f"  has_prompt_preview  : {row.has_prompt_preview:,}")
            print(f"  has_response_preview: {row.has_response_preview:,}")
        print("\nRe-run without --dry-run to apply the update.")
        return

    sql = BACKFILL_SQL.format(project=args.project)
    print(f"Running backfill UPDATE on {args.project}.lakehouse.spans …")
    print("(This may take 30–60 s on a ~10k-row table)")
    job = client.query(sql)
    result = job.result()
    print(f"✓ Backfill complete. Rows affected: {result.num_dml_affected_rows:,}")


if __name__ == "__main__":
    main()
