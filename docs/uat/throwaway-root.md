# UAT throwaway folder root

The New Session picker accepts only roots configured on the host daemon. Its
normal default is the daemon account's Home directory. Do **not** add `/tmp`,
`/`, or another broad shared directory just to exercise UAT: that would widen
the production host's folder policy.

Use a dedicated, private directory below the UAT daemon account's Home instead.
The default is `~/drover-uat` (mode `0700`). It is a picker root, not a
repository checkout; create folders below it during UAT and remove its test
contents afterward.

## Isolated foreground UAT

Run the UAT daemon as a separate foreground process. The helper creates the
root if needed and passes `DROVER_FOLDER_ROOTS` only to that child process; it
does not edit a launchd or systemd service.

```sh
scripts/uat-run.sh -- uv run drover-harnessd \
  --host-id studio-uat \
  --display-name 'Studio UAT' \
  --kind macos \
  --listen 127.0.0.1:7081 \
  --local-url http://127.0.0.1:7081 \
  --central-url http://127.0.0.1:7080
```

To use a different throwaway location, keep it below that account's Home:

```sh
scripts/uat-run.sh --root "$HOME/qa/drover-uat" -- uv run drover-harnessd ...
```

The helper rejects paths outside Home, traversal components, and a symlink at
the configured root. The daemon retains its descriptor-based checks and still
rejects symlink traversal, hidden paths, and every path outside this root.

## Existing Studio service: operator setup, only when scheduled

Do not change the Studio's running allowlist during ordinary development. When
an operator schedules live Studio UAT, add the following **additional** entry
to that UAT daemon's launchd job `EnvironmentVariables` dictionary, preserving
all existing entries and using the service account's absolute home path:

```xml
<key>DROVER_FOLDER_ROOTS</key>
<string>/Users/NAME/drover-uat</string>
```

Before restarting that UAT service, create and inspect the directory as the
service account:

```sh
install -d -m 700 "$HOME/drover-uat"
ls -ld "$HOME/drover-uat"
```

Restart only the dedicated UAT job using its documented label, then open the
picker and confirm its Locations list contains `drover-uat` but not `/tmp`.
After UAT, remove the `DROVER_FOLDER_ROOTS` entry from the UAT job or restore
its prior value, restart that UAT job, and delete only the test contents after
review. This repository change does not perform any of those host mutations.
