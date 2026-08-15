# Security Threat Model

This document describes the security architecture and threat model for Drover v0.3.
It complements the technical details in [Security](./security.md) and outlines the
assumptions, trust boundaries, and threat mitigations that inform the design.

## Scope

This threat model applies to the Drover v0.3 codebase and its standard deployment
configurations:

- `drover-server`: central fleet API and context store
- `drover-harnessd`: per-host agent control daemon
- iOS client application
- DuckDB + Parquet local context store

## Trust Model

### Primary Assumptions

1. **Single trusted operator**: All users and devices belong to one operator who
   controls the machines and network. Drover is not designed for multi-tenant or
   shared access.

2. **Trusted network boundaries**: All communication occurs within:
   - localhost on a single machine
   - a trusted private LAN
   - a private Tailscale network controlled by the operator

3. **Operator trust**: Anyone with access to a bearer credential or pairing code
   is trusted to execute arbitrary commands on any registered harness host.

4. **OS-level trust**: Agent CLIs run with full filesystem and process access on
   their host machine; Drover does not sandbox command execution.

### Trust Boundaries

```
+------------------+     Authenticated HTTP/WS      +------------------+
|   iOS Client     |------------------------------->|  drover-server   |
| (Device)         | <-----------------------------|  (Fleet API)     |
| - Token in       |    Authenticated HTTP/WS     |  - hashed tokens |
|   Keychain       |                                  |  - fleet state   |
+------------------+                                  +--------+---------+
                                                            |
                                                            | bearer token
                                                            v
+------------------+                                   +----------+-----------+
| drover-harnessd  |<---------------------------------->| drover-harnessd    |
| (Host A)         |    Authenticated HTTP/WS         | (Host B)           |
| - agent processes|                                   | - agent processes|
| - PTY sessions   |                                   | - PTY sessions     |
+------------------+                                   +-------+------------+
         |                                                    ^
         | unprivileged agent CLI                             | unprivileged agent CLI
         v                                                    |
+------------------+                                   +-------v------------+
| Claude Code,     |                                   | Claude Code,     |
| Codex, etc.      |                                   | Codex, etc.      |
+------------------+                                   +------------------+
```

## Threat Categories

### 1. Authentication Attacks

**Threat**: An attacker attempts to authenticate as a legitimate user or host without valid credentials.

**Attack Vectors**:
- Credential theft from `credentials.json` or token files
- Exploiting unauthenticated endpoints
- Replaying expired or invalid pairing codes
- Man-in-the-middle on unencrypted connections

**Mitigations**:
- Tokens are stored hashed on the server (`sha256("drover-cred-v1\0" + token)`), not in plaintext
- Pairing codes are single-use, time-limited (10 minutes for devices, 15 minutes for hosts)
- When authentication is enabled, all routes except `/healthz`, `/readyz`,
  `/auth/login`, `/auth/pair`, and `/harness/probe` require a bearer token or
  session cookie
- `/readyz` reports the ready verdict and which store failed, but withholds the
  underlying database error (which quotes filesystem paths) unless the caller
  is authenticated
- Pair endpoint rate-limits: 5 failure attempts per minute
- The legacy shared token is accepted by default for upgrade compatibility and
  can be disabled after devices and hosts are paired

**Residual Risk**: If `credentials.json` or token file is compromised, attacker gains
full fleet access until the affected credential is revoked.

### 2. Network Exposure Attacks

**Threat**: Drover is exposed beyond its trusted network boundary, enabling unauthorized remote access.

**Attack Vectors**:
- Server bound to 0.0.0.0 without firewall rules
- Accidental public port forwarding (exposing port 7080)
- Tailscale Funnel or similar public exposure mechanisms
- Compromised router or network infrastructure
- ARP spoofing or DNS hijacking on LAN

**Mitigations**:
- Central listeners bind to `127.0.0.1` by default
- Host daemon binds to `127.0.0.1:7081` by default
- Pairing QR code points to `advertised_url`, validated as private address
- Explicit security documentation warning against public exposure
- Supported network boundaries limited to localhost, private LAN, or Tailscale

**Residual Risk**: Deliberate misconfiguration (binding to 0.0.0.0 without firewall)
can expose the service to network-wide attack surface. Operator responsible for
network hardening.

### 3. Data Leakage Threats

**Threat**: Sensitive data stored in the context store is exfiltrated or accessed by unauthorized parties.

**Attack Vectors**:
- Disk compromise or theft of central storage machine
- Backups not encrypted or stored insecurely
- Log files containing sensitive information
- Screen capture or terminal logging
- Shared token or credential file exposure in shell history or logs
- Git history containing secrets (e.g., accidentally committed `~/.drover` files)

**Mitigations**:
- Context store data stored locally under `~/.drover/` by default
- Tokens stored with restrictive permissions (mode `0600`)
- Credentials.json hashes tokens, never stores plaintext tokens
- `raw_objects/` directory stores large payloads by URI
- Operator responsible for backup and storage security
- No external cloud storage or transmission by default

**Residual Risk**: If central machine is compromised, attacker gains access to all
stored agent events, summaries, decisions, and embeddings. No disk-level encryption
at the application layer; relies on OS-level encryption.

### 4. Device Compromise Threats

**Threat**: A registered device or host is compromised, enabling attacker to control agent sessions or exfiltrate data.

**Attack Vectors**:
- Malware on operator's machine scanning for Drover token files
- Compromised agent CLI injecting malicious commands
- Terminal session hijacking via PTY access
- Keychain compromise on iOS device
- Physical device theft

**Mitigations**:
- Each device holds its own bearer credential
- Server-side credential revocation available via `drover-server credentials revoke`
- Pairing codes expire, preventing persistent unauthorized access
- Local agent processes run in controlled environment
- No per-host identity binding yet (credential grants full host access)

**Residual Risk**: A compromised credential provides full access to all registered
hosts. No granular credential scoping or per-host identity verification.

### 5. Supply Chain Attacks

**Threat**: Compromised dependencies or release artifacts introduce malicious code.

**Attack Vectors**:
- PyPI package compromise
- GitHub Actions workflow compromise
- Malicious pip install targets
- Compromised Docker images or installation scripts

**Mitigations**:
- Source distribution only (no official PyPI package as of v0.3)
- All code on GitHub repository, open source
- Signed releases (recommended verification by operators)
- Installer scripts are reviewed before release
- `uv` dependency resolver for Python packages

**Residual Risk**: Operator must trust the source repository and verification
processes. No formal third-party security audit or continuous verification.

## Known Limitations (v0.3)

These security features are intentionally not provided:

1. **Multi-tenant isolation**: No support for multiple operators
2. **Per-host credentials**: All devices can access all hosts with valid token
3. **RBAC/SCOPED permissions**: No fine-grained command-level authorization
4. **SSO integration**: No enterprise identity provider support
5. **Sandboxed execution**: Agent commands execute with full host privileges
6. **Automatic backup**: No built-in encrypted backup or disaster recovery
7. **Host identity binding**: No cryptographic machine identity
8. **Audit logging**: No tamper-evident audit trail for admin actions
9. **Encrypted storage**: Relies on OS-level encryption only
10. **Network segmentation**: No micro-segmentation of host-to-server traffic

## Incident Response

If you suspect a security incident:

1. **Immediate actions**:
   - Rotate the shared token via config: drop `api_token`, regenerate on restart
   - Revoke compromised credentials via `drover-server credentials revoke`
   - Check `harness_hosts` for unauthorized registration attempts

2. **Report to maintainer**:
   - Email: security@arniesaha.com (placeholder)
   - Include: suspected attack vector, impact assessment, evidence
   - Do not publish details publicly until mitigated

3. **Post-incident**:
   - Review access logs from `harness_events` table
   - Audit agent commands executed during incident window
   - Rotate all credentials if host-level compromise suspected

## Acknowledgments

This threat model was written to guide Drover's security design. It reflects known
limitations and intentional design decisions for v0.3. Updates will be made as new
threats are identified and features are added.

## Version History

- 0.2 (2026-08-13): Updated for the v0.3.0 release tag
