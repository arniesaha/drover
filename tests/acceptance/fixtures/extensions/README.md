DuckDB extension binaries are provisioned outside the repository.

Run `scripts/fetch-lake-extensions.sh DIR`, then export
`DROVER_TEST_LAKE_EXTENSIONS=DIR`. The helper uses curl and verifies each
uncompressed artifact against `EXTENSION_HASHES` in the lake runtime.
Matching files are reused; download or hash failures terminate provisioning.

The PostgreSQL CI job caches `~/.cache/drover-lake-ext` by DuckDB version and
the runtime source hash, and runs provisioning even on cache hits. Missing
or invalid artifacts fail the contracts rather than skipping them.
