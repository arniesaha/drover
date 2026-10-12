# Rollout tooling investigation, Oct 12

Scope: approved Oct 11 rollout tooling fixes. Commit only. Studio evidence was
read without changing the service, updater configuration, runtime or backups.

## Evidence and attribution

Base main: `1e69016786f8510ae6d64f9e7f2075fa36e79481` (v0.6.6).
Investigated rollout release v0.6.5:
`72fe3c8fbf53e6ec2b65f795becb369cf779367c`.

Preserved evidence: `com.drover.server.plist.broken-array-20261011`, 470 bytes,
mtime Oct 10 2026 20:10:39 PT, SHA-256
`aa8c718b410c04283e9175871d0899167ed4f3f3bcdd401f4e7e2cb74f294ec7`.
Its root is a seven-element array, consisting of the service shell environment
wrapper and daemon arguments. It has no job Label, environment or KeepAlive.

On this macOS version, the following reproduces the failure using only a
throwaway complete job:

```sh
plutil -extract ProgramArguments xml1 temporary-job.plist
```

It exits successfully, emits no stdout, and replaces the input with its argv
array. Reconstructing a complete temporary job around the preserved arguments
then extracting produced a byte-identical copy of the evidence with the same
SHA-256. Serializing only the arguments with `plistlib.dumps` also produces
identical bytes, so the artifact alone cannot distinguish those two actions.
`-o -` instead leaves the input untouched and sends the array to stdout.
The automated characterization test uses synthetic arguments and temporary
files, without production data.

Repository searches covered installer, updater, cutover/rollout code and docs
for plistlib writes, plutil, defaults write and ProgramArguments. The v0.6.5
renderer and current renderer build a full job dictionary. The installer was
the only live Drover daemon plist writer and used direct `write_text`, with no
semantic validation or atomic replacement. Updater and cutover do not write
service plist contents. iOS export-options plist writers are unrelated.
No repository command or available rollout/history record identifies the
historical extraction invocation. This establishes a reproducible destructive
extraction mechanism, not the actor or exact command that ran on Oct 10.

The underlying tooling weakness is treating syntactically valid plist fragments
as service definitions, compounded by a direct unvalidated installer write.
The fix centralizes semantic validation and atomic replacement, makes preflight
reject fragments, and documents safe read-only extraction. It cannot intercept
an arbitrary external `plutil` command, so operator probes must use `-o -`.

## Implementation

- `server/service_units.py`: complete job validation, render validation,
  round-trip and staged-file validation, fsync, atomic replacement, preserved
  file permissions and cleanup after failure. `install.sh` uses this boundary.
- `server/lake/cutover.py`: ServiceView rejects incomplete/malformed jobs.
- `server/lake/rollout.py` and `cutover_cli.py`: read-only
  `cutover release-preflight`, separate from the existing full gate. It binds
  the service interpreter to its configured runtime, verifies staged artifacts
  against independent digests and installed package bytes, checks engine and
  extension integrity, and compares migrations and derived schema versions.
  It also checks pin/marker, disk, fresh verified backup and current health.
- `server/update_planner.py`: suppress a pin older than the active hub runtime.
- `server/harness/updater.py`: reject superseded pending activation and discard
  a recovery marker belonging to an older runtime than the current selection.
- `point-release-rollout.md`: staged release, backup, preflight, activation,
  health/rollback sequence and pin semantics. `lake-cutover.md` links to it.

The Studio runtime was observed at 0.6.5 with configured pin 0.6.4. Omitted
`update.enabled` defaults to true. Pins do not change during manual rollouts.
Previously the hub could advertise the old pin but `HostUpdater.observe`
already refused older targets. The independently reproduced downgrade defects
were stale pending activation and stale recovery after a newer manual flip.
The intentional watchdog rollback of its own failed activation is retained.

## Verification and deferred work

Baseline scoped tests: 115 passed. Newly added writer and updater regressions
first failed against the base behavior; point-release tests first failed before
implementation. Review findings were reproduced by four failing tests for
helper-based schema changes, unrelated service runtime, wrong engine version
and changed engine bytes, then fixed. Additional failing regressions caught
shared-base Python symlink binding and malformed XML verdict handling.

Final scoped verification: 231 Python tests passed across service units,
update planner, in-place activation, lake cutover/rollout, harness update
reporting/wiring/heartbeat/restarter/quiescence and runtime layout. All seven
installer shell suites passed, with 205 checks. Black, isort and diff whitespace
checks passed on the touched files; CLI help was exercised. Installer checks
use fake supervisors, artifacts and temporary homes; no live launchd job was
loaded or restarted. The macOS extraction test touches only a temporary plist.
Independent review identified the schema-helper, service binding and engine
integrity gaps; all were fixed and verified.

Deferred: exact historical actor/invocation attribution, live rollout and
real production backup/health execution. These are outside this commit-only
scope. Schema signatures deliberately err toward requiring the full gate for
DDL-producing function changes and require maintenance when new schema helper
APIs are introduced. Preflight provides a snapshot of evidence; operators must
freeze updates and avoid concurrent runtime/config changes during activation.
