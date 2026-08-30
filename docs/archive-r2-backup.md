# Verified Pond backups to R2

This is a manual, experimental operator workflow. The local Pond store remains
the only live archive read and sync target. R2 holds immutable backup
generations and is used only to restore into a fresh local directory. It is
never a live recall target, a sync target, or a reason to remove any Drover
retrieval path.

Drover does not create Cloudflare resources, change lifecycle rules, schedule
backups, cut over the live store, or delete local or remote data. Keep every
value and artifact described below private.

## Private setup

Use Pond v0.16.3 from commit `23c7d0e` and the approved checksum-pinned binary.
All configured paths must be existing, canonical absolute paths with no
symlinks. The current operator must own both Pond config files and the backup
config at mode `0600`, and must own the receipt and eligibility directories at
mode `0700`. The receipt directory's parent is also private at mode `0700`.
The local store must be an existing directory owned by the current operator at
exact mode `0700`. The Pond binary must be executable.

Create the private directories outside Git. The angle-bracket values below are
placeholders, not working values:

```text
umask 077
install -d -m 0700 <private-receipt-parent>
install -d -m 0700 <private-receipt-parent>/<private-receipt-directory-name>
install -d -m 0700 <private-receipt-directory>/eligibility
chmod 0700 <absolute-path-to-existing-local-pond-store>
chmod 0600 <local-pond-config>
chmod 0600 <isolated-remote-pond-config>
chmod 0600 <private-backup-config>
```

The isolated remote Pond config resolves the credential outside Drover. Never
put that credential, the remote config, or the backup config in the live
Drover config, stdout, an operations ticket, or Git. The remote URL must not
contain credentials.

The backup config is an exact-field TOML document. Replace every placeholder,
then keep the file owner-only at mode `0600`:

```toml
schema_version = 1
pond_binary = "<absolute-path-to-checksum-pinned-pond>"
local_pond_config = "<absolute-path-to-private-local-pond-config>"
local_store = "<absolute-path-to-existing-local-pond-store>"
remote_pond_config = "<absolute-path-to-private-isolated-remote-pond-config>"
backup_root_url = "s3+https://<account-id>.r2.cloudflarestorage.com/<private-bucket>/<approved-backup-root>"
store_scope_id = "<random-version-4-uuid>"
receipt_directory = "<absolute-path-to-private-receipt-directory>"
copy_timeout_seconds = 1800
max_rss_bytes = 3221225472
max_physical_bytes = 4294967296
max_swap_growth_bytes = 536870912
```

The backup root must use `s3+https`, the account-scoped R2 authority, and a
nonempty bucket/root path without a trailing slash. It must have no user info,
port, query, fragment, percent-encoding, traversal, or `generations` segment.
`store_scope_id` is a new random version-4 UUID used only to bind private
receipts; it is not an account, bucket, host, or source identifier.

If an exact current metadata-only eligibility receipt is required, put its
owner-only `0600` JSON file directly in
`<private-receipt-directory>/eligibility`. Leave unrelated, stale, or
unbound receipts out. Preflight loads every `.json` entry in that mode-`0700`
directory and fails closed on any malformed or unsafe entry.

## Eligibility and local-sync prerequisites

Before every backup preflight or run:

1. Confirm this is the approved primary writer. It is the only host with the
   recurring Object Read & Write credential. No other host may sync or copy to
   the backup root.
2. Confirm the bucket is private, the root is approved, and the isolated remote
   Pond config resolves the primary credential. Do not expose its values.
3. Confirm Drover and the harness are healthy, the expected process identities
   are stable, and dropped-event and swap baselines are available.
4. Stop any concurrent Pond sync, optimize, copy, or other writer against the
   local store. Local read-only serving may remain running.
5. Explicitly sync current local sources into the configured local store:

   ```text
   pond --config-file <local-pond-config> --storage-path <local-pond-store> sync
   ```

6. Resolve every pending source. Preflight independently runs Pond sync in
   dry-run JSON mode and requires no unexplained pending source, complete
   source coverage, allowed source agents, and zero duplicate or collision
   gates.

An active source that changes during the backup window prevents a successful
freshness receipt. Do not treat a not-ready result as permission to prune a
source, registry record, or archive entry.

## Commands

These are the complete four-command interface and its options:

```text
drover-server archive backup preflight --config <private-backup-config>
drover-server archive backup run --config <private-backup-config> [--apply]
drover-server archive backup restore --config <private-backup-config> --receipt <private-receipt> --destination <fresh-local-directory> [--apply]
drover-server archive backup inspect-receipt --receipt <private-receipt>
```

`preflight` is local-only and never contacts R2. It validates configuration,
the pinned Pond release, current source and local Pond inventories, the Drover
registry snapshot, eligibility bindings, coverage, local Pond dry-run, health,
and resource gates. It writes no receipt. Its aggregate stdout has exactly
`schema_version`, `ready`, `source_inventory`, `pond_corpus`, `coverage`, and
`source_not_archive_eligible` at the top level.

Run `run` without `--apply` first. It performs local preflight, does not contact
R2, and prints exactly these fields:

```json
{"mode":"dry-run","preflight_ready":true,"remote_contacted":false,"schema_version":1}
```

A structurally valid but not-ready `preflight` prints its one aggregate object
with `ready: false` and exits `2`. A not-ready `run` without `--apply` prints
the same dry-run object with `preflight_ready: false` and exits `2`. Exit `2`
is an operator readiness block, not an internal failure and not permission to
apply.

Only after the dry run is ready, run the same `run` command with `--apply`.
Applied backup creates one new empty generation, performs exactly one local to
remote copy, runs read-only `copy --verify-only`, proves pre/post local equality
and exact remote counts, then publishes a private receipt. It never retries a
copy automatically. Its aggregate stdout, and the output of `inspect-receipt`,
contain exactly:

- `schema_version`, `pond_version`, `sessions`, `messages`, `parts`,
  `source_not_archive_eligible`, `collision_counts`, `copy_duration_ms`,
  `verify_duration_ms`, `health_samples`, `health_p95_ms`, `peak_rss_bytes`,
  `peak_physical_bytes`, `swap_delta_bytes`, and `result`;
- inside `collision_counts`: `duplicate_source_groups`,
  `cross_harness_native_id_groups`,
  `archive_logical_duplicate_candidate_groups`, and
  `archive_signature_unverifiable`.

For a restore drill, use an independent checksum-pinned Pond v0.16.3 binary
and an Object Read credential in the isolated remote config. Restore does not
run `pond storage check`, so it does not require a write-capable probe
credential. Without `--apply`, restore validates the private receipt chain and
fresh destination locally, makes no remote contact, starts no store, and
prints exactly:

```json
{"destination_valid":true,"mode":"dry-run","receipt_chain_valid":true,"remote_contacted":false,"schema_version":1,"store_started":false}
```

With `--apply`, the destination must not exist; its existing parent must be a
private local directory. Drover creates the destination at mode `0700`, refuses
the configured live store, copies only the receipt's scoped generation, runs
read-only verification and inventory gates, and prints exactly
`schema_version`, `verified`, `sessions`, `messages`, `parts`,
`current_source_coverage`, `health_samples`, `health_p95_ms`, `peak_rss_bytes`,
`peak_physical_bytes`, `swap_delta_bytes`, and `store_started`.
`store_started` remains `false`. The restored store remains stopped: do not
cut over Pond or Drover, and do not change the live loopback archive config.

## Safety gates and retained evidence

Every applied backup and restore is bounded at 3 GiB RSS, 4 GiB physical
footprint, and 512 MiB swap growth. A successful applied operation needs at
least 30 health samples with `health_p95_ms` below 100 ms, stable Drover and
harness process identity, no health failure or hub restart, and no increase in
dropped events. Crossing a gate fails closed.

Successful receipts are owner-only `0600` files named
`backup-<generation-id>.json` directly below the receipt directory. They bind
private inventories and coverage through digests but contain only approved
aggregate output when inspected. They never authorize deletion.

Diagnostic material is private and intentionally retained:

- every standalone preflight leaves a mode-`0700`
  `<private-receipt-directory>/.drover-backup-preflight-<unpredictable-token>`
  workspace on success, not-ready, and failure;
- every applied backup leaves mode-`0700` phase directories below
  `<private-receipt-directory>/.backup-runs/<generation-id>` whether or not a
  receipt is published;
- an applied restore leaves a mode-`0700`
  `<destination-parent>/.drover-restore-<unpredictable-token>` diagnostic
  workspace beside the restored directory;
- bounded process `*.stdout` and `*.stderr`, source and Pond inventories,
  coverage reports, and corpus snapshots inside those workspaces are mode
  `0600` private artifacts.

Drover has no automatic lifecycle cleanup for these local workspaces. Keep
them private while investigating and inventory them under the operator's
separate local retention procedure. Any later removal is a deliberate
operator action outside this workflow; the commands above neither select nor
delete them.

An interrupted or failed remote generation has no successful receipt. It is
not eligible for restore, is never reused or modified, and remains untouched
for diagnosis and the separately approved R2 lifecycle or operator process.
The workflow performs no remote deletion.

Public failures are only these nine fixed categories; detailed values remain
in the retained private artifacts:

- `archive backup config failed`
- `archive backup preflight failed`
- `archive backup local changed`
- `archive backup storage unavailable`
- `archive backup copy failed`
- `archive backup verify failed`
- `archive backup receipt failed`
- `archive backup restore failed`
- `archive backup resource limit`

## Manual Cloudflare and adoption evidence

Drover does not call the Cloudflare API. Record these checks privately and do
not paste account, bucket, prefix, endpoint, credential, generation, host, or
path values into public records:

- bucket privacy and absence of public access, custom domains, and unintended
  CORS exposure;
- the primary Object Read & Write credential scope, the independent restore
  Object Read credential scope, and revocation of any temporary write-capable
  probe credential used only at a separate non-corpus location;
- the lifecycle rule and backup interval that normally preserve at least two
  independently verified immutable generations;
- R2 storage bytes plus Class A and Class B operations for each run, then the
  measured monthly projection, which must remain below USD 5;
- three consecutive verified primary-host generations, one matching restore
  on the primary host, and one matching restore on a second physical machine;
- one older generation observed to expire through lifecycle while the newest
  two verified generations and their restore evidence remain intact.

Cloudflare R2 metrics retain 31 days of data and can lag a run. Record an empty
immediate query as `unavailable`, never as zero, and re-read it within the
retention window. The operator, not this workflow, owns analytics collection,
lifecycle review, public-access review, and any remote retention action.

Until every adoption gate passes, keep this workflow manual. R2 remains only
an immutable backup/restore boundary; local Pond remains the live recall and
sync source.
