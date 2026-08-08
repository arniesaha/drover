# Security

Drover v0.1 is designed for one trusted operator on machines and networks they
control. It is not a multi-tenant service.

## Supported Boundary

- Localhost on a single machine
- A trusted private LAN
- A private Tailscale network whose members you control

Tailscale Funnel and other public-internet exposure are not supported. The
relay protocol can carry powerful session and terminal operations, and v0.1
does not bind individual hosts to individual credentials.

## Authentication

The central HTTP API uses one shared bearer token. Resolution order is:

1. `DROVER_API_TOKEN`
2. `[auth].api_token` in `~/.drover/config.toml`
3. Auto-generated `~/.drover/api_token`

The generated token file uses mode `0600`. Treat the token as equivalent to
interactive access to every registered harness host:

- Do not commit it or paste it into issue bodies, logs, screenshots, or shell
  commands that will be shared.
- Store it in the iOS Keychain through the app's settings flow.
- Rotate it if any participating machine or account becomes untrusted.
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
