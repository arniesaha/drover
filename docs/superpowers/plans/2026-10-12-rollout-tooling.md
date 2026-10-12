# Rollout tooling implementation plan

Goal: complete the approved rollout fixes, commit only, with no live Studio writes.

Design: use one validated atomic launchd writer for installation and documented
edits. Add a read-only point-release preflight alongside the existing lake gate.
Determine compatibility from the installed and target migration definitions,
lake schema declarations and runtime pins, rather than release-number guesses.
Keep schema changes on the full cutover path. Recheck activation targets and
recovery markers against the current runtime before permitting automatic flips.

Constraints: base 1e69016786f8510ae6d64f9e7f2075fa36e79481, scoped tests only,
no push, PR, merge, deployment, private data, or live service changes.

1. Add rejection, atomicity and macOS extraction reproduction tests to
   tests/test_service_units.py. Implement validate_launchd and write_launchd in
   service_units.py; wire install.sh to the writer and validate ServiceView loads.
2. Add stale activation/recovery and old-pin tests. Guard maybe_activate and
   verify_after_restart; suppress hub pins older than its active runtime.
3. Add release preflight tests for schema routing, pin mismatch, missing/corrupt
   artifacts, disk, verified backup and health. Implement release preflight in
   lake/rollout.py and expose cutover release-preflight without altering gate.
   Probe both installed interpreters using the same compatibility script.
4. Document commands, evidence and operator sequence. Run only touched package
   tests plus installer shell tests, review the diff, and commit.

Review focus: preserve original plist on failed validation or replacement;
block unknown schema metadata; reject stale backups and stale updater markers;
verify target artifact hashes against independently supplied release digests;
never let an automatic recovery undo a newer manual deployment.
