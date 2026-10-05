# DuckLake v2 acceptance harness

The control fixture restores the committed production schema and migration rows
at versions 1–11. It never constructs a freshly migrated substitute. All database
creation uses the disposable `postgres_dsn` test fixture; no production config,
DSN or catalog is discovered.

The small lake is 50,000 events; the Studio lake is exactly 5,000,000. Both have
120 date partitions, Pareto session lengths and fresh seeded text/tool payloads.
A 40,000-event session exceeds 8 MiB raw. Cache identity includes seed, scale,
generator version and cache root; a successful cached generation is reused.

Provision the pinned extensions before running lake contracts. CI caches them
by DuckDB version and runtime hash, then verifies the hashes on every run.
Download or verification failures fail the job; binaries are never committed.

```sh
scripts/fetch-lake-extensions.sh "$HOME/.cache/drover-lake-ext"
export DROVER_TEST_LAKE_EXTENSIONS="$HOME/.cache/drover-lake-ext"
pytest tests/acceptance -m 'not acceptance_scale' -q -p no:cacheprovider
scripts/acceptance-scale.sh -q -p no:cacheprovider
scripts/acceptance-scale.sh -q --runxfail -p no:cacheprovider
```

Scale execution refuses GitHub Actions / CI. The normal PR Postgres job runs all
small contracts and self-tests; the pgvector job also runs both drift guards so
`vector(768)` is compared, including its type modifier. Only an unavailable
extension permits omitting the conditional embeddings table, with a warning.

Each strict xfail names a known product gap and accepts only assertion failures.
A2 has separate legacy and DuckLake cases. A5 restores versions 1–11 and verifies that startup applies only migration 12,
preserving the existing migration rows, before the first exporter pending read. Scale tests rebuild and verify the
lake through production APIs, then register analytics exactly as the hub does.
A1 starts the real hub exporter lifecycle and writes through the collector's
actual atomic JSONL writer.
The only model substitute is a deterministic LLM response; all SQL, process
limits, job transitions and HTTP handlers execute normally.

A4 uses the current API spelling `activity.status == "ok"` to mean available.
It measures five full HTTP requests and production `_rss(child_pid)` samples,
including samples from children that fail before returning a reply. A3 observes
the actual summarizer query replies and errors, then checks ledger success,
byte limits and truncation for both a large and a short session.

S2's rollback CLI is specified as `outbox replay --sink legacy` against the
isolated config's durable control outbox. S4's import CLI options name isolated
lake/catalog and legacy source roots explicitly. S2 replay is executable; S4 import remains a future contract. A7's lake imports only the last date; the first-day session exists in
the retained legacy archive and control registry, with its native ID mapping
and historical start time. This partition boundary is the
harness's explicit import watermark until S4 persists that metadata; the harness
does not invent a product watermark table or read path.
