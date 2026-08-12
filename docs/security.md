# Security

Drover v0.1 is designed for one trusted operator on machines and networks they
control. It is not a multi-tenant service.

## Supported Boundary

- Localhost on a single machine
- A trusted private LAN
- A private Tailscale network whose members you control

Tailscale Funnel and other public-internet exposure are not supported. The
relay protocol forwards requests that create and control agent sessions and
carries bidirectional terminal streams. Drover v0.1 does not bind individual
hosts to individual credentials.

## Authentication

Each paired device and each harness host holds its own bearer credential,
issued by redeeming a pairing code. The server stores only
`sha256("drover-cred-v1\0" + token)` and never the token, so `credentials.json`
cannot be replayed if it leaks, and revoking one device leaves every other
credential working.

Pairing codes are held in the running server's memory and never written to
disk, so restarting the server invalidates every outstanding code. A code is
single use, expires after ten minutes for a device and fifteen for a host, and
carries its own scope, so a device code cannot be redeemed into a host
credential.

`POST /auth/pair` is the only unauthenticated write in the API, because a
device being paired has no credential yet. It answers identically for unknown,
already used, and expired codes, and refuses a source after five failed
attempts in a minute.

Treat any credential as equivalent to interactive access to every registered
harness host. Revoke rather than rotate when a single device is lost:

```bash
drover-server credentials list
drover-server credentials revoke <credential-id>
```

### Legacy shared token

The original single shared bearer token is still accepted while
`[auth] legacy_token_enabled` is true, which is the default so that upgrading
does not lock out an existing host or phone. Turn it off once every device and
host has been paired. Resolution order is:

1. `DROVER_API_TOKEN`
2. `[auth].api_token` in `~/.drover/config.toml`
3. Auto-generated `~/.drover/api_token`

The generated token file uses mode `0600`, as does `~/.drover/credentials.json`.
Treat any token as equivalent to interactive access to every registered harness
host:

- Do not commit it or paste it into issue bodies, logs, screenshots, or shell
  commands that will be shared.
- Prefer scanning a pairing code over copying a token; the app stores what it
  receives in the iOS Keychain.
- Rotate the shared token, or revoke the affected credential, if any
  participating machine or account becomes untrusted.
- Keep authentication enabled outside isolated local development.

## Trust Model

Drover does not currently provide:

- Multiple users or tenant isolation
- RBAC, SSO, or scoped API permissions
- Per-host tokens or cryptographic host identity binding
- A sandbox around commands launched by an agent harness
- A hosted backup, recovery, or availability service

Every registered host and every client holding the shared token belongs to the
same trust domain. Run agent CLIs with the operating-system account and file
permissions you intend them to have.

## GitHub Actions Runner

The trusted GitHub Actions runner executes code using its existing macOS
account. Anyone who can change the protected `main` branch or control that
account is therefore inside the runner's trust domain. Public pull requests
remain on GitHub-hosted runners; labels alone do not protect the Mac, and
approving an external workflow does not authorize it there. Follow the
[trusted GitHub Actions runner runbook](github-actions-runner.md) to install,
operate, update, and remove the host-owned runner hooks safely.

## Data Handling

The context store may contain prompts, responses, repository paths, diffs,
tool calls, and telemetry. It remains on the configured local storage unless
you explicitly ship events between your own machines or configure an external
model/embedding provider.

Before sharing logs, database extracts, screenshots, or issue reports, remove
credentials, private hostnames, personal paths, repository secrets, and user
content. Git history is not a suitable place for sensitive runtime evidence.

## Network Checklist

Central OTLP, MCP, and cockpit listeners bind to `127.0.0.1` by default. The
host daemon also defaults to `127.0.0.1:7081`. Reaching Drover from another
machine therefore requires an explicit bind override.

Before binding beyond `127.0.0.1`:

1. Confirm unauthenticated `/harness/hosts` returns `401` or `403`.
2. Confirm authenticated access works through the intended private address.
3. Restrict the listener with the host firewall or Tailscale policy.
4. Confirm no Funnel, public reverse proxy, or public port forward is active.
5. Rotate the shared token after removing a host from the trust domain.

Report security issues privately to the repository owner rather than opening a
public issue containing exploit details or sensitive evidence.
