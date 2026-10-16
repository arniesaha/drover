# UAT round 6

## Host setup guidance

The New Session folder picker deliberately defaults to the daemon's Home
directory. A picker showing only Home is therefore expected, not a failure;
it does not make `/tmp` available. To include `/tmp` on macOS/Linux, set the
host daemon service environment to `DROVER_FOLDER_ROOTS="$HOME:/tmp"` and
restart that daemon. Do not set it to an empty value: that explicitly disables
all picker roots. The default has not been widened because filesystem roots
are a host security boundary.
