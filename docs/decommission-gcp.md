# Decommissioning GCP after the Local DuckDB + Parquet Migration

> **Status (2026-05-09):** GCP teardown executed. All `nexus-*`
> resources destroyed in `nexus-context-engine-26`. Terraform state
> drained. Local backend impersonation disabled. See "Post-mortem"
> at the bottom for what went sideways and how it was unstuck.

This was the manual checklist that closed the loop on PR #39's GCP exit.
**Every step here either touches shared infrastructure or destroys
data, so they live outside the agent's autonomous flow.** Run them by
hand when you're confident the local lakehouse is steady-state.

Spec reference: `docs/superpowers/specs/2026-05-08-nexus-architecture-redesign-design.md` §8 step 9, §8.2.

---

## 0. Pre-flight (none of this is destructive)

```bash
# All three should report >0 rows for the dates you care about
nexus-server doctor
nexus-server status
nexus-server compact     # tidy up small parquet files first
```

If `doctor` reports any warnings, **stop**. Investigate before tearing
anything else down.

---

## 1. Seed historical data into the new DuckDB + Parquet store

The migration script lives at `scripts/migrate_to_duckdb.py`. Default
output is `~/.nexus/`.

```bash
# Dry run first — parses CSVs without writing Parquet
python scripts/migrate_to_duckdb.py --dry-run

# Real run (writes ~3.9 GB of CSV → Parquet, ~150 MB compressed)
python scripts/migrate_to_duckdb.py

# Re-run nexus-server doctor to confirm row counts grew as expected
nexus-server doctor
```

Custom paths if you want to seed somewhere other than `~/.nexus/`:

```bash
python scripts/migrate_to_duckdb.py \
  --source-dir ~/local_backup \
  --output-dir /tmp/nexus-test/parquet \
  --db-path /tmp/nexus-test/nexus.duckdb
```

⚠️ **USER ACTION REQUIRED** — the script reads ~3.9 GB of CSV and
writes to your production lakehouse. It's not destructive (idempotent
via dedup_key MERGE) but it's slow and uses real disk.

---

## 2. Watch for dual-running drift (~30 days)

Per spec §8 step 9, leave AgentWeave's existing Tempo→GCS→BQ pull
pipeline running in parallel for ~30 days. Compare row counts daily:

```bash
# Local DuckDB
nexus-server doctor

# BigQuery — adjust dataset name to match your project
bq query --use_legacy_sql=false \
  "SELECT date(start_time) AS d, count(*) FROM lakehouse.spans
   WHERE date(start_time) > current_date() - 7
   GROUP BY 1 ORDER BY 1"
```

Drift > 1% means something is rotting. Don't proceed to step 3 until
you see ≥7 consecutive days of clean parity.

---

## 3. Tear down GCP infrastructure

⚠️ **USER ACTION REQUIRED — DESTRUCTIVE.** Make sure step 2 confirmed
parity. Once you run `terraform destroy`, the BigQuery dataset and
Cloud Function are gone.

```bash
cd iac/

# Show what will be destroyed
terraform plan -destroy

# Read the plan output. If it includes anything OTHER than:
#   - google_bigquery_dataset.lakehouse
#   - google_pubsub_topic.* (the ETL trigger topic)
#   - google_cloudfunctions2_function.nexus_etl
#   - associated IAM bindings
# STOP and figure out why before applying.

terraform destroy
```

After destroy, AgentWeave on NAS k3s no longer needs the
Tempo→GCS shipper:

```bash
# On NAS:
sudo systemctl disable --now nexus-agentweave.timer
sudo systemctl disable --now nexus-agentweave.service
```

---

## 4. Delete dead code from this repo

```bash
git rm -r src/nexus/cloud_function/
git rm scripts/sync_*.sh
git rm scripts/test_bq_insert.py scripts/test_ssh.py scripts/test_vertex_*.py
git rm scripts/init_db.py scripts/hydrate_lakehouse.py 2>/dev/null || true

# Update pyproject.toml: remove unused GCP-only deps
#   google-cloud-storage, google-cloud-bigquery, google-cloud-aiplatform,
#   sqlalchemy, pg8000, cloud-sql-python-connector[pg8000]
# Keep `requests` if it's still used elsewhere.

git add -A
git commit -m "chore: remove GCP-era code paths after lakehouse migration"
```

---

## 5. Sanity check

```bash
# Re-run the test suite — nothing in the new server depends on GCP libs
pytest tests/

# Re-run nexus-server doctor — should be unchanged from before deletion
nexus-server doctor
```

---

## Rollback

If GCP teardown surfaces a problem within the first 24 hours:

```bash
cd iac/
terraform apply        # re-create infra from the .tf files (still in git)
# Re-enable the NAS shipper:
sudo systemctl enable --now nexus-agentweave.timer
```

After 24h the old data is gone; rollback only restores the empty
schema.

---

## Post-mortem (2026-05-09 actual run)

What we hit and how we worked around it:

### 1. Pre-existing drift from another agent

The user had previously asked another agent to delete some GCP
resources directly. By the time we ran `terraform plan -refresh-only`,
these were already gone in GCP but still present in state:

| Resource | Outcome |
|---|---|
| `google_bigquery_dataset.lakehouse` (+ tables) | 404 in GCP |
| `google_sql_database_instance.nexus_db` (+ DB) | 404 in GCP |
| `google_storage_bucket.raw_logs` | 404 in GCP |

Fix: `terraform state rm` for each ghost. Nine resources removed in
one batch — see `git log` around 2026-05-09.

### 2. Cloud Function destroy raced its own SA

`google_service_account.iac_agent` is the SA terraform impersonates.
When destroy ran, terraform deleted the SA before the async Cloud
Function delete operation finished polling, producing
`ACCOUNT_STATE_INVALID` on subsequent operation polls. The function
*was* deleted in GCP — only the state-tracking poll failed.

Fix: dropped impersonation in `iac/providers.tf` and `iac/backend.tf`
(user has `roles/owner` directly), `terraform state rm
google_cloudfunctions2_function.nexus_etl`, replan, reapply.

### 3. Service Networking Connection won't release

`google_service_networking_connection.private_vpc_connection` failed
with `FLOW_SN_DC_RESOURCE_PREVENTING_DELETE_CONNECTION`. The Cloud SQL
tenant project (`*-tp` peer) hadn't released the producer-side route
even though the parent instance was destroyed.

Fix: `gcloud compute networks peerings delete
servicenetworking-googleapis-com --network=nexus-vpc` (compute-side
delete, bypasses the tenant handshake). After that, the next
`terraform apply` cleared the rest of the VPC stack cleanly.

### 4. Doctor view binder errors over migrated parquet

Unrelated but surfaced during verification: `nexus-server doctor`
filtered on `dedup_key IS NOT NULL` and called `strftime(timestamp,
...)`, both of which failed against the legacy migration parquet
(no `dedup_key` column, `timestamp` is VARCHAR). Switched to
`id IS NOT NULL` and `strftime(TRY_CAST(timestamp AS TIMESTAMP),
...)`. Schema views in `src/nexus/schema.py` got the same
TRY_CAST treatment in a separate commit.

### Final inventory (2026-05-09 morning)

In `nexus-context-engine-26` after destroy:

- **Compute / network:** none (only `default` VPC remains).
- **Cloud Functions:** none.
- **Cloud SQL:** none.
- **Pub/Sub:** none.
- **BigQuery:** none.
- **GCS:** `nexus-tf-state-26` (terraform backend), plus unrelated
  buckets (`agent-foundry-data`, `sentinel-phase0`,
  `gcf-v2-sources-*`, `nexus-context-engine-26_cloudbuild`).
- **Service accounts:** GCP-managed defaults plus `jenny-agent`
  (kept — separate from this stack).
- **APIs:** still enabled (`disable_on_destroy = false`); harmless.

Local lakehouse (`~/.nexus/`):

- agent_events: 357,236 rows
- spans: 11,125 rows
- pr_events: 64
- routing: 1,990
