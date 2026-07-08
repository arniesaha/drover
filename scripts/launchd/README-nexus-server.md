# nexus-server launchd unit (Mac Mini)

## Prereqs
- `nexus-server init` has been run; `~/.nexus/config.toml` exists.
- `nexus-server` is on PATH inside the venv referenced in the plist.

## Install
    cp scripts/launchd/com.nexus.server.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.nexus.server.plist

## Verify
    launchctl list | grep com.nexus.server
    tail -f ~/Library/Logs/nexus-server.out.log

## Uninstall
    launchctl unload ~/Library/LaunchAgents/com.nexus.server.plist
    rm ~/Library/LaunchAgents/com.nexus.server.plist
