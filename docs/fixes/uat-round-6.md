# UAT round 6

## Host setup guidance

The New Session folder picker deliberately defaults to the daemon's Home
directory. A picker showing only Home is therefore expected, not a failure;
it does not make `/tmp` available. For scheduled UAT, use a private root such as
`$HOME/drover-uat` and set that UAT daemon service environment to
`DROVER_FOLDER_ROOTS="$HOME/drover-uat"`. Do not add `/tmp` or `/` to a
production daemon allowlist. Do not set it to an empty value: that explicitly disables
all picker roots. The default has not been widened because filesystem roots
are a host security boundary.

See [UAT throwaway folder root](../uat/throwaway-root.md) for the isolated runner and later Studio operator procedure.
