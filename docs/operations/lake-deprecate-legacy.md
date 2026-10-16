# Deprecating legacy-import rows

`lake deprecate-legacy` retires the unreceipted rows imported before the
receipt-aware exporter existed. They are not adopted as a serving baseline.
After a successful run, the live lake contains only the rebuild baseline and
rows established by exporter receipts; normal `lake verify` and serving proof
validation then use their unchanged rules.

The command is read-only by default. Its selection is deliberately narrow:
an `agent_events` row is selected only when `_import_file LIKE
'legacy-import:%'` and it exceeds the receipt-version count for its
`(date, _row_sha256)`. Non-legacy exporter rows consume that count first. Thus
a receipted row on a crossover day is retained even if an old tool labelled it
as legacy, while a duplicate legacy copy cannot survive on the same hash and
day. Legacy `control_outbox_batches` and provider observations have no
equivalent exporter evidence and are selected by the honest legacy provenance
rule. The retired `import_watermark` cursor is rewritten to the earliest
retained event day (rather than removed), so pre-cutover cockpit windows remain
marked as archived. `activity_daily` is refreshed for every affected day,
rather than being deleted by provenance.

## Procedure

1. First create and independently verify a backup of the catalog and data
   root. The deprecated source copies intentionally remain available under
   `/Volumes/M2 1/drover-ducklake*`, plus the cutover and rollout backups; this
   procedure does not erase those copies.
2. Stop the hub for the maintenance window and confirm that no exporter owns
   the catalog mutation fence.
3. In the administrator environment that supplies the independently verified
   DuckLake extension variables and catalog admin DSN, inspect the inventory:

   ```sh
   drover-server lake deprecate-legacy --data-root "$DROVER_LAKE_ROOT/$LAKE" \
     --catalog-dsn-env DROVER_LAKE_V2_ADMIN_DSN
   ```

   Save this JSON: it reports every selected table and day without writing.
4. Re-run with `--apply` only after reviewing the counts:

   ```sh
   drover-server lake deprecate-legacy --data-root "$DROVER_LAKE_ROOT/$LAKE" \
     --catalog-dsn-env DROVER_LAKE_V2_ADMIN_DSN --apply
   ```

   Apply acquires the drained mutation fence, deletes only the selected rows in
   one DuckLake transaction, refreshes the affected activity facts, rewrites
   the archive watermark, and checks the same baseline-plus-receipt accounting
   used by `lake verify` before the transaction can commit. An unexplained
   row or a NULL event date rolls the transaction back with
   `lake_deprecation_would_fail_verify`. It records the exact
   selection/counts and before/after snapshots in
   `verification/legacy-deprecations.json`, then performs offline verification
   while the fence remains held. That verification publishes the new serving
   proof before the hub can resume. This is intentionally not a maintenance
   receipt: maintenance receipts attest unchanged logical content, whereas
   deprecation changes it.
5. Save the JSON report and copy its standalone
   `[analytics] verification_sha256 = "…"` line into the selected hub config.
   Validate that config with the hub loader, then start the hub and check
   readiness:

   ```sh
   "$TOOL_PY" -c 'from drover.server.__main__ import _resolve_config; import sys; _resolve_config(sys.argv[1])' "$CONFIG"
   ```

   Confirm the report's verification and serving snapshot fields. Do not edit a
   proof, checkpoint, or evidence file to recover a failed run; restore the
   verified backup or investigate offline instead.

A second `--apply` is a no-op. The retired `lake import` implementation now
fails with `lake_legacy_import_deprecated`; use the receipted exporter or
reconcile workflow for new live data. There is no trusted-legacy mode,
allowlist, or verification exception.
