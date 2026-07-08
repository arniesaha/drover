# nexus-collect launchd unit (per host)

Ships local agent session files to the Mac Mini's `~/.nexus/incoming/<host>/`
directory every 5 minutes. The Mac Mini's `nexus-server` watcher picks them
up and merges them into the lakehouse.

## Prereqs

- `nexus-collect init` has been run; `~/.nexus/collect.toml` exists.
- The relevant `[sources.*]` blocks in that file have `enabled = true`.
- Passwordless SSH from this host to `mac-mini.local` is configured (`ssh mac-mini.local true` exits 0).
- `nexus-collect` is on PATH inside the venv referenced in the plist (or edit the plist to point at your venv).

## Install

    cp scripts/launchd/com.nexus.collect.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.nexus.collect.plist

## Verify

    launchctl list | grep com.nexus.collect
    nexus-collect status
    tail -f ~/Library/Logs/nexus-collect.out.log

## Uninstall

    launchctl unload ~/Library/LaunchAgents/com.nexus.collect.plist
    rm ~/Library/LaunchAgents/com.nexus.collect.plist

## Notes on cadence and concurrency

`StartInterval = 300` runs the job every 5 minutes regardless of completion
time of the previous run. If a previous run hasn't finished, advisory
`flock` per source ensures the second invocation skips that source cleanly
rather than racing against itself (`CursorLocked` log line).

For Linux hosts, write a systemd user timer with `OnUnitActiveSec=5min` and
the same `nexus-collect run` invocation.
