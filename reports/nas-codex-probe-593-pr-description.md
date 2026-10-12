# Fix NAS Codex quota runtime and timeout diagnostics

Fixes #593. Quota reads reuse the session preset's resolved Codex executable and now prepend that executable's nvm bin directory to PATH, as session launches already do. The version check uses the same environment. Log the selected path and validated CLI version once per probe instance, plus startup and RPC phase durations and failure categories. No account responses or arbitrary version output enter these diagnostics.

`DROVER_CODEX_USAGE_TIMEOUT_SECONDS` controls the total startup/initialize/account/read/rateLimits/read deadline. Default: 20 seconds. Nonpositive, nonfinite and malformed environment values fall back to that default with a value-free warning. Explicit constructor budgets retain precedence. The separate version check runs once per instance, is bounded to at most three seconds and caps output. Existing bounded cleanup and last-good quota retention remain in place.

Executable resolution keeps explicit paths first, then the user's login-shell PATH, then the newest executable installation by numeric nvm Node version. A login-shell error or timeout now also reaches the existing versioned fallback. We preserve the session executable selection rather than independently picking a different quota CLI.

## Report

Read issue #593 and `audit/provider-quota` report commit `4f46189c9bee236bedae9a7b2ec5f59f09f9cb19`. Base main: `cebacebe9ded25f00dcc42fe06141c330b53ba32`.

The audit establishes an advertised Node v24.13.0 / Codex 0.147.0 installation versus the login shell's Node v24.19.0 / Codex 0.160.1. It reproduced a five-second timeout, followed by an old-install success in 1.36 seconds with a twenty-second budget. It does not prove that CLI age caused the intermittent latency.

`install.sh` generates a systemd PATH containing the Drover runtime, local bin and system prefixes, without nvm. `service_units.py` writes that PATH into `Environment=`. `auth.resolve_executable` runs `command -v` in a login shell and otherwise scans nvm. Presets retain that selection until daemon restart. `resolve_harness_presets` prepends the selected CLI's Node bin for session commands. `_codex_usage_snapshot` already reuses the absolute preset executable, but previously spawned it with the daemon environment. The runtime difference is fixed here; phase logs allow the remaining latency cause to be measured.

### Proposed NAS ops change, written only

Run these commands as the existing NAS service user after deploying this commit through the normal deployment workflow. These commands are documented only and have not been executed. They select the audited v24.19.0 installation in the unit environment and set the quota budget explicitly. `%h` is expanded by systemd, so no private home path needs to appear in this report.

```sh
test -x "$HOME/.nvm/versions/node/v24.19.0/bin/node"
test -x "$HOME/.nvm/versions/node/v24.19.0/bin/codex"
mkdir -p "$HOME/.config/systemd/user/drover-harnessd.service.d"
cat > "$HOME/.config/systemd/user/drover-harnessd.service.d/593-codex-quota.conf" <<'UNIT'
[Service]
Environment="PATH=%h/.nvm/versions/node/v24.19.0/bin:%h/.drover/runtime/current/bin:%h/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
Environment="DROVER_CODEX_USAGE_TIMEOUT_SECONDS=20"
UNIT
systemctl --user daemon-reload
systemctl --user restart drover-harnessd.service
journalctl --user -u drover-harnessd.service --since '5 minutes ago' --no-pager \
  | grep -E 'codex usage probe executable=|codex app-server phase=|codex usage probe failed'
```

At the next scheduled quota read, verify the selected path ends in `v24.19.0/bin/codex`, version is `0.160.1`, budget is `20.000`, and all three RPC phases succeed. Observe subsequent scheduled probes and last-success advancement. If login-shell startup explicitly overrides nvm selection, reconcile that shell configuration through the normal deployment workflow before treating the rollout as verified. A larger budget does not establish the original timeout cause. No live NAS validation, restart, credential operation or unit edit was performed for this change.

Rollback the ops override by removing only `593-codex-quota.conf`, then daemon-reload and restart the user service through the deployment workflow.

## Validation

Scoped tests cover explicit-path and login-PATH precedence, numeric newest-nvm fallback for missing and timed-out shells, nvm runtime preservation when the daemon PATH has no Node, path/version logging without account payloads, timeout default/configuration/validation, explicit-budget precedence, and a shared deadline across multiple delayed RPCs. The harnessd endpoint test verifies that the resolved preset executable and environment timeout reach the probe. Existing Codex transport, cleanup, provider retention and catalog tests are included in scoped validation.

Results:

- `uv run pytest -q tests/test_provider_usage.py tests/test_harness_auth.py tests/test_model_catalog_codex.py tests/test_model_catalog_agy.py tests/test_harness_daemon.py -k 'codex or resolve_harness_presets or nvm'`: 58 passed, 255 deselected.
- `uv run pytest -q tests/test_provider_usage.py tests/test_model_catalog_agy.py`: 69 passed. These overlap the first scope.
- Black and isort checks on all seven changed Python files: passed.
- `git diff --check`: passed.
- Public-release audit of all eight changed files: zero findings.
