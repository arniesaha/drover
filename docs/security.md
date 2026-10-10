# Security

Drover is designed for one trusted operator on machines and networks they
control. It is not a multi-tenant service. This document describes the latest
tagged release; see [SECURITY.md](../SECURITY.md#supported-versions) for the
support policy.

## Supported Boundary

- Localhost on a single machine
- A trusted private LAN
- A private Tailscale network whose members you control

Tailscale Funnel and other public-internet exposure are not supported. The
relay protocol forwards requests that create and control agent sessions and
carries bidirectional terminal streams. Host credentials are now bound to their
issued host identity, but public relay ingress remains unsupported until these
controls and an explicit relay threat review are complete.

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

Device credentials grant interactive fleet access. Host credentials are bound
to an explicit host ID and cannot address another host's HTTP routes or sessions.
Host registration, heartbeats, relay attachment, and event ingest require an
active matching host credential, even when legacy operator auth is enabled.
Event batches validate stored session ownership before writing. Mismatches are
rejected with 403 and a clear reason in the log. A hello-only relay mismatch
closes with WebSocket status 1008 after upgrade; newer clients declare identity
in the upgrade request and receive HTTP 403. Relay authorization is rechecked
during use, so revoked or replaced credentials lose their attachment.

The running hub issues bound credentials with `credentials issue-host --host-id`
and replaces only that host's credentials with `credentials rotate-host --host-id`.
Issuance and revocation of host credentials require the operator bearer.
See [the exact upgrade steps](multi-host.md#upgrade-to-host-identity-binding)
for reissuing shared or unbound credentials. Existing matching bindings survive
upgrade without changing the token; missing bindings are never guessed from
labels or client claims. Host tokens cannot be exchanged for unbound browser
cookies. Profile credentials permit only tier-filtered profile reads; preflight
credentials permit only their HTTP readiness allowlist. Revoke rather than
rotate when a single device is lost:

```bash
drover-server credentials list
drover-server credentials revoke <credential-id>
```

### MCP

MCP requires bearer authentication even on loopback, with the same active
credentials, revocation and legacy-token policy as HTTP. Every tool is guarded.
Host/device credentials retain HTTP authority; profile credentials can only
read `drover_profile` using their registered tier and cannot submit proposals
or enqueue session-close work. The legacy operator bearer retains private
profile access while enabled. Cookies cannot authenticate MCP.

There is no anonymous escape hatch. `[auth] enabled = false` refuses MCP startup.
Non-loopback MCP binds are refused because protected remote transport is not
configured. Private LAN or Tailscale membership alone does not protect the MCP
port. Client bearer setup and the capability table are in [MCP](mcp.md) and
[client integrations](integrations/README.md). Profile import remains private
by default; MCP authentication does not change import or promotion policy.

### Legacy shared token

The original single shared bearer token is still accepted while
`[auth] legacy_token_enabled` is true. It retains operator and device-facing
access, but cannot authenticate host ingress. Hosts using it must follow the
reissue steps above. Host credential administration also requires this setting;
if you disable it after enrollment, enable it on the private hub and restart
before the next issuance, rotation, or revocation. Resolution order is:

1. `DROVER_API_TOKEN`
2. `[auth].api_token` in `~/.drover/config.toml`
3. Auto-generated `~/.drover/api_token`

The generated token file uses mode `0600`, as does `~/.drover/credentials.json`.
Protect every token as a secret. Scope checks do not replace the trusted
operator and private-network boundary:

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
- General RBAC or SSO beyond the existing credential-scope allowlists
- Cryptographic machine identity or protection against theft of a host's token
- A sandbox around commands launched by an agent harness
- A hosted backup, recovery, or availability service

Every registered host, every paired device, and every client holding the
shared token belongs to the same trust domain. Run agent CLIs with the
operating-system account and file permissions you intend them to have.

## GitHub Actions Runner

Repository workflows run only on GitHub-hosted runners. Do not attach a
self-hosted runner to this public repository: an unsafe workflow change could
otherwise execute untrusted code on the machine that holds fleet data and
operator credentials. See the [GitHub Actions runner model](github-actions-runner.md).

## Data Handling

A fresh central installation keeps fleet serving state in a PostgreSQL control
store; the installer stores its DSN in `~/.drover/server.env` with mode `0600`
and never in TOML or service arguments. A managed control store binds only to
`127.0.0.1`. Upgrading does not migrate an existing DuckDB control store
automatically; that cutover is an explicit offline operator step described in
[PostgreSQL control store](postgresql-control-store.md). The analytical lake
and each host's local spool remain DuckDB and Parquet.

The context store may contain prompts, responses, repository paths, diffs,
tool calls, and telemetry. It remains on the configured local storage unless
you explicitly ship events between your own machines, configure an external
model/embedding provider, enable APNs push notifications (relayed by Apple), or
run a manual archive backup to R2.

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
