# Normal private server update

This is the default path for a compatible, ordinary **server-only** private
update. It changes the server runtime only. It does not edit the service plist,
configuration, or environment; restart harnessd; publish a release; tag; or
upgrade another host. Read the classification before starting.

## Classify the change first

| Change | Path |
| --- | --- |
| Compatible, server-only implementation change | This runbook. |
| Documentation- or tests-only change | Review, exact-head CI, and merge only. Do not simulate a runtime rollout. |
| Control/lake migration, coherent snapshot need, multi-role compatibility change, or exclusive writer | [Advanced point-release maintenance](point-release-rollout.md) or the applicable [lake cutover](lake-cutover.md) procedure. Keep its backup, recovery, and migration/rollback proof. |

If the classification is uncertain, use the advanced path. A version number is
not compatibility evidence. Do not select an older runtime or tag merely to
quiet an updater/watchdog.

## Compatible server-only sequence

1. **Establish the candidate.** Independently review the exact merged source
   head and require the repository's required exact-head CI. Run only
   task-delta-relevant local tests; do not weaken workflows or substitute CI
   evidence from another commit.
2. **Stage, without selecting.** Build or obtain the dependency-complete wheel
   for that exact source head and stage it beside the active runtime. Verify the
   artifact identity and locked dependencies independently of the running
   environment. Preserve the previous runtime, current configuration,
   environment file, and launchd plist unchanged.
3. **Select atomically.** Re-check that the staged runtime is the intended
   target, then atomically replace the `runtime/current` link without following
   a replacement link. This is a runtime selection only: do not run the full
   installer, rewrite a plist, change a pin/configuration, or restart harnessd.
4. **Restart the server normally.** Send `SIGTERM` to the existing
   KeepAlive-managed server through its normal `launchctl` service identity.
   Do not use bootout/bootstrap, kqueue polling, normalization loops, or a
   second controller. Let launchd start the selected runtime.
5. **Verify the process actually serving.** Confirm the new server PID,
   selected runtime, and listener binding; then use the existing authenticated
   health and readiness probes against that listener. Run one targeted feature
   smoke appropriate to the change. Record only what those checks prove.
6. **Close out truthfully.** State the exact source head, staged runtime,
   successful probes, and smoke result. A private install is not public
   publication, signing/notarization, a fleet upgrade, or autonomous-owner
   end-to-end/soak acceptance.

If selection, startup, health/readiness, or the targeted smoke fails, restore
the retained prior `runtime/current` link, restart that same launchd service,
and repeat the authenticated health/readiness check. Stop there if restoration
does not pass; do not edit configuration, restart harnessd, or downgrade to an
older tag as an improvised recovery.

## Evidence boundaries

The normal path proves only one private server installation. Keep these
decisions separate:

- **Private install:** exact runtime selection, server restart, authenticated
  health/readiness, and the targeted feature smoke above.
- **Public release:** signing, notarization, tags, publication, and release
  assets require explicit human scope and their own evidence.
- **Autonomous-owner E2E/soak:** optional owner workflow evidence is neither a
  substitute for server acceptance nor automatically implied by it.
- **Fleet changes:** every additional host and every harnessd change has its
  own compatibility decision; neither is a default consequence of a private
  server update.

The older [rollout tooling investigation](rollout-tooling-investigation.md) is
historical context for the advanced safeguards, not the normal-update default.
