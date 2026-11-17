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

Resolve any classification uncertainty before changing the runtime. Use the
advanced path only when the actual change needs it, not because information is
missing or because of a patch tag. There is no schema-risk waiver. Do not select
an older runtime or tag merely to quiet an updater/watchdog.

## Compatible server-only sequence

1. **Establish the candidate.** Independently review the exact merged source
   head and require the repository's required exact-head CI. Run only
   task-delta-relevant local tests; do not weaken workflows or substitute CI
   evidence from another commit.
2. **Check switch conflicts.** Identify actual active turns, queued intents,
   and any updater able to select the same runtime; defer until no affected
   in-flight work remains. A long-lived `running` or `awaiting input` session
   alone is not an active writer. Preserve the current update policy. If it can
   race the selection, resolve that specific conflict under its appropriate
   scope; do not globally disable or restart services.
3. **Stage, without selecting.** Build or obtain the exact wheel for that
   source head, then stage it in an independently verified,
   dependency-complete runtime with locked/preserved dependencies. A
   `--no-deps` wheel install does not populate an empty environment. Verify the
   artifact identity independently of the running environment.
4. **Record rollback evidence.** Copy and hash-verify the current
   configuration, environment file, and launchd plist; record the old
   `runtime/current` target and server PID/start time. The originals stay
   unchanged. A compatible code-only update needs no database dump or restore
   drill.
5. **Select atomically.** Re-check that the staged runtime is the intended
   target, then atomically replace the `runtime/current` link without following
   a replacement link. This is a runtime selection only: do not run the full
   installer, rewrite a plist, change a pin/configuration, or restart harnessd.
6. **Restart the server normally.** Send `SIGTERM` to the existing
   KeepAlive-managed server through its normal `launchctl` service identity.
   Do not use bootout/bootstrap, kqueue polling, normalization loops, or a
   second controller. Let launchd start the selected runtime.
7. **Verify the process actually serving.** Confirm the new server PID,
   selected runtime, and listener binding; then use the existing authenticated
   health and readiness probes against that listener. Run one targeted feature
   smoke appropriate to the change. Record only what those checks prove.
8. **Close out truthfully.** State the exact source head, staged runtime,
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
