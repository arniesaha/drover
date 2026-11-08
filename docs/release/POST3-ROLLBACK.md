# Private post3 rollback

The last-good private runtime before post3 is private-0.6.56.post1-a17b4da. The guarded Studio rollout preserved an additional last-good plist snapshot at /Users/arnab/.drover/rollout/history/20261108T130835.942789Z.

Use the existing scripts/studio_rollout.py rollback behavior only if health, relay, or queue evidence fails. It restores the recorded runtime and plist copies, restarts services, and verifies health. Do not manually repoint runtime/current, edit a plist, repair the registry, or run a data migration.

The 4.37 GB verified owner backup is evidence for rollback investigation, not permission to restore production data.
