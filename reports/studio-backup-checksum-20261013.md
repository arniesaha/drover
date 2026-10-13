# Studio NAS checksum failure investigation

Base: `b17bde6` (backup parity, #602). All machine and NAS inspection was
read-only. No live scripts, services, databases or NAS generations were changed.

## Evidence and limits

The backup log shows NAS checksum verification starting Oct 13 at 03:48:57
and failure at 03:51:35 (local time), with helper exit 1. The Oct 12 attempt
started verification at 11:26:19 and failed at 11:29:07. These were verification
failures after both lake passes, not transfer failures.

Live script SHA-256 values:

- `studio_backup.py`: `fc75fba58b8e0f0d033f69451ea2d14010dc956332395e748fce59e6005e0058`
- `nas_snapshot.py`: `caf9924fffd5e237478d11f30cea93181e1172e639c53a37df11f3fe0e846643`

The live Studio script differs from the base in target-root configuration and
exception logging; its lake copy/hash sequence is the same. The NAS helper
matches the base. It saves `MANIFEST.json` only after every artifact passes.
The Oct 13 pending generation contains no manifest, so the original expected
hashes and exact failing path set cannot be recovered from this evidence.

A subsequent read-only comparison of retained NAS files with current sources
found these differing database bytes:

| Relative path | Pending NAS SHA-256 | Current source SHA-256 |
| --- | --- | --- |
| `drover-data/drover.registry.duckdb` | `8c2645ab611a0088edef26839bd1cfcf0b1ab63a719fcc51be87b0e39f7ee7c4` | `eb79a32bdc29a0e98f01a02b4b91e810abc5406b0fa6c1bc55a24e34604abbf8` |
| `drover-data/drover.duckdb` | `3b54c35e7d37ab6e3f7161fce1e63fdef973ce5a10ba0a9ef609bd60c1be427b` | `3b8156f8000e50037b94aeea0509670373a8055a3ae31a3fe2e2192579097639` |

The other top-level stores compared, `harnessd.registry.duckdb`,
`harnessd.duckdb` and `loop-phase0.duckdb`, matched. No registry WAL was present
in the pending tree at inspection. The home-registry metadata copy is a separate
store from the data-root registry. These are later observations, not a recovered
Oct 13 manifest comparison. They support the live-file hypothesis but do not
prove which artifact first failed that night.

## Cause and correction

The old sequence copies each live lake tree, then hashes that live source and
compares those later hashes with the already-copied NAS files. A change after
rsync but before hashing causes a false checksum failure. Metadata checks during
hashing do not cover that interval. Raw lake passes also include DuckDB pages,
WAL and locks, and the old registry copy is not a transactional snapshot.

Both raw passes now exclude database/WAL/lock/socket files and snapshot scratch.
DuckDB stores use source-volume atomic DB/WAL clones with before/after identity
and timestamp checks. A changed pair is retried; unsupported cloning fails
closed. DuckDB recovers and checkpoints only the private clone. Postgres retains
its existing consistent dumps.

Lake hashing occurs on the frozen NAS copy after the final pass. Staged metadata
still uses independently computed pre-transfer hashes. Verification saves the
expected manifest before checking, records all failing relative paths and
reasons, and logs escaped filenames without artifact contents or child output.
Publication requires a successful verification report and archive readback.
Failures preserve pending trees under unique `.failed-<date>-<time>` names;
interrupted pending trees are preserved during preparation so retries proceed.
Cross-store atomicity and catalog reference completeness remain unproven.

## Scoped validation

`python -m unittest discover -s tests/backup -v`: 21 tests pass on macOS,
including real GNU rsync exclusions and a real DuckDB/APFS clone test with a
writer changing the WAL mid-clone. Regression coverage includes source changes
after the final transfer, same-size NAS tampering, changes during verification,
all-path diagnostics, failed publication, failed-generation retention, retry,
permissions, retention and safe path handling.
