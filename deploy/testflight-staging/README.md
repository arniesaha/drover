# Isolated internal TestFlight staging

This is an operator-run Mac Mini runtime for one reviewed candidate. GitHub
Actions never receives shell access to this machine. Run the local tool from
a trusted checkout; it creates detached candidate worktrees, installs with
`uv sync --frozen --no-dev`, and manages only these two launchd labels:

| Job | Listener | Advertised route |
| --- | --- | --- |
| `com.drover.testflight-server` | `127.0.0.1:17080` | `<STAGING_PUBLIC_ORIGIN>` |
| `com.drover.testflight-harnessd` | `127.0.0.1:17081` | `http://127.0.0.1:17081` |

Any later public tunnel must target **only port 17080**. Port 17081 stays on
loopback. Tunnel provisioning, Apple uploads, CI workflows, and remote repair
are outside this tool. Use the logged-in operator's launchd GUI domain.

## Operator sequence

Choose an absolute, dedicated root corresponding to `~/.drover-testflight`.
In the commands below, `STAGING_ROOT`, `REPOSITORY`, `RELEASE_SHA`,
`PREVIOUS_SHA`, and `STAGING_PUBLIC_ORIGIN` are operator-supplied placeholders.
Set `STAGING_HOME="$STAGING_ROOT/home"`; `<STAGING_HOME>` in the reference
plists means this same isolated home.
Never commit their real values, account details, or credentials. The source
repository must be clean, and each SHA must be a full lowercase 40-hex commit
reachable from the locally fetched `origin/main`. Fetch/review that ref before
preparation; the tool does not fetch or decide which candidate to trust.

1. Prepare the candidate without starting services:

   ```sh
   python3 scripts/testflight/stage.py prepare --repository "$REPOSITORY" \
     --root "$STAGING_ROOT" --sha "$RELEASE_SHA" \
     --public-url "$STAGING_PUBLIC_ORIGIN"
   ```

2. Configure separate staging provider and APNs material. Both jobs run with
   `HOME=<STAGING_ROOT>/home` and a cleared environment. Sign the selected
   provider using dedicated low-quota staging credentials. For Claude, provision
   `<STAGING_HOME>/.drover/anthropic_api_key` as an owner-only `0600` regular file
   containing a dedicated Anthropic API key. Do not export the key or place it in
   a command argument. Claude's API-key helper reads that file through a private
   pipe; the key never enters a plist or process environment. The CLI must support
   [bare mode](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode),
   which skips OAuth and the system Keychain. Staging disables subscription
   login/status commands and OAuth usage readers; this lane does not validate
   subscription billing behavior. Do not copy personal CLI state or credentials.
   Install the selected CLI so the launchd PATH shown in the rendered plist can
   find it. Keep any staging APNs key and configuration paths below the staging
   root, and edit only `<STAGING_HOME>/.drover/config.toml`. The tool preserves
   operator additions on subsequent prepares, while enforcing its paths,
   authentication, and listener settings. The config example embeds no token.
   Background summarizer, briefs, embeddings, OTLP, MCP, and automatic updates
   are disabled. Both jobs explicitly identify their dedicated launchd label
   through `XPC_SERVICE_NAME`; no fallback personal label is used.

   For Codex, use only the dedicated `CODEX_HOME=<STAGING_HOME>/.codex` and log in
   locally with `-c 'cli_auth_credentials_store="file"'`. Both the structured
   driver and provider app-server force that setting; the credential stays in
   this home rather than the OS Keychain. See [Codex credential storage](https://learn.chatgpt.com/docs/auth#credential-storage).
   The app does not start login flows in staging. It accepts only structured
   Claude/Codex sessions using runtime-selected commands.

3. Activate:

   ```sh
   python3 scripts/testflight/stage.py activate --root "$STAGING_ROOT" \
     --sha "$RELEASE_SHA"
   ```

   The server starts first, creates its owner-only token in the empty staging
   home, and must pass loopback liveness, readiness, and exact release identity.
   Only then does harnessd start. Its token resolver reads the same isolated
   default token file. **No `--host-token` argument is passed:** the existing CLI
   requires a value for that option, while omitting it selects the default file.
   Activation succeeds only with the one online staging host registered at the
   expected loopback address. A failed readiness check leaves activation failed;
   it does not trigger an automatic repair or change another service.

4. Issue the preflight credential locally, using the staged executable and
   isolated config. This command prints a secret; keep it in the operator's
   private credential handling flow, never a transcript, repo, or build log:

   ```sh
   env -i HOME="$STAGING_ROOT/home" PATH=/usr/bin:/bin \
     "$STAGING_ROOT/worktrees/$RELEASE_SHA/.venv/bin/drover-server" \
     --config "$STAGING_HOME/.drover/config.toml" \
     credentials issue-preflight --label internal-testflight
   ```

   The preflight credential is for read-only candidate checks. The local probe
   uses the isolated owner token because it needs to create a session.

5. Run the bounded structured probe after staging provider login:

   ```sh
   python3 scripts/testflight/stage.py probe --root "$STAGING_ROOT" \
     --sha "$RELEASE_SHA" --harness claude-code
   ```

   `--harness codex` selects Codex instead. The probe runs one fixed test turn in
   `<STAGING_ROOT>/workspace`, waits for the exact expected assistant response
   via the central messages API, and terminates the session on success or
   failure. It polls at most 60 times; each HTTP request has a five-second
   timeout and a one-MiB response bound. Cleanup must confirm the exact session
   and host, `terminated: true`, and `status: terminated` before
   publishing `<STAGING_ROOT>/staging-probe.json` atomically with mode `0600`.
   The attestation contains only `source_sha`, `host_id`, a timezone-bearing
   `completed_at`, and `session_id_sha256`. It has no endpoint, credential,
   prompt, reply, or raw session identifier. Failure preserves the previous
   successful attestation and removes temporary output. If transport failure
   prevents session termination, inspect and terminate it locally before retrying.

6. Roll back to a previously prepared candidate when needed:

   ```sh
   python3 scripts/testflight/stage.py rollback --root "$STAGING_ROOT" \
     --sha "$PREVIOUS_SHA"
   ```

   This regenerates only the two staging definitions and repeats the ordered
   restart and checks. It does not revert database schemas or provider state;
   select a schema-compatible candidate. A previous probe is accepted by the
   server only when its SHA matches the running release. Re-run the probe for
   the rolled-back candidate before distributing it.

## Local state boundary

All runtime artifacts are under the chosen root: `worktrees/<sha>`, `state`,
`home`, `workspace`, `logs`, `tmp`, `cache`, `launchd`, and `releases`.
`active-release.json` records the prepared/activated SHA and installed package
version, and is owner-only. Preparation may advance that record before a
restart; verify `/release-identity` to establish the running candidate.
The generated plist environment and an `env -i` wrapper prevent inherited
launchd provider settings or token overrides from selecting personal accounts.
Staging never shares personal databases, pairing state, credentials, provider
homes, or logs. The tool rejects symlinks throughout staging runtime state,
including implicit provider homes and individual stdout/stderr files. Only
cache-internal links are allowed, for uv wheel archives; dangling links and
links outside the cache are rejected. Candidate code must contain the enforced
credential-boundary module; preparation and
activation refuse older releases that lack it.

The checked-in plist files are examples with placeholders; `stage.py` renders
actual definitions under the staging root. Do not install examples directly.
Lifecycle operations are sequential operator actions; do not run two copies of
the tool concurrently or manage the same staging labels from multiple roots.
The tool never prunes old candidate worktrees or deletes database state.

## Verification without live side effects

```sh
uv run --frozen --extra dev pytest tests/test_testflight_stage.py -q
uv run --frozen --extra dev black --check scripts/testflight/stage.py tests/test_testflight_stage.py
uv run --frozen --extra dev isort --check-only scripts/testflight/stage.py tests/test_testflight_stage.py
```

Tests use disposable temporary roots and mocked subprocess/HTTP boundaries,
including the prepare/activate/rollback CLI sequence. They do not boot launchd,
contact a provider, or operate on a real staging root. A successful mocked run
is not evidence that a live candidate has been deployed or probed.
