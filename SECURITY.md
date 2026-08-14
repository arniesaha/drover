# Security Policy

We take the security of Drover seriously. This document outlines our security policy,
vulnerability disclosure process, and best practices for users.

## Reporting Security Issues

**DO NOT** file security issues as public GitHub issues.

If you discover a potential security vulnerability, please report it privately:

- **Email**: [security@arniesaha.com](mailto:security@arniesaha.com)
- **PGP Key**: Available via key server or request via email

### What to Include

When reporting a vulnerability, please provide:

1. **Description**: Clear explanation of the vulnerability
2. **Impact**: What an attacker could achieve
3. **Steps to Reproduce**: Detailed steps demonstrating the issue
4. **Proof of Concept**: Code, URLs, or other reproducible evidence
5. **Environment**: Drover version, OS, configuration details
6. **Estimated Severity**: Your assessment of criticality (optional)

### Response Timeline

- **Acknowledgement**: Within 3 days of report submission
- **Initial Response**: Detailed assessment within 7 days
- **Resolution**: Target 30 days for high/critical vulnerabilities
- **Disclosure**: Coordinated with reporter before public announcement

## Supported Versions

We recommend running the latest version of Drover for security updates.

| Version | Supported          |
| ------- | ------------------ |
| v0.2.x  | :white_check_mark: |
| v0.1.x  | :x:                |
| < 0.1.0 | :x:                |

**Note**: Drover is in active development. Always run the latest release for best
security posture.

## Best Practices

### For Operators

1. **Network Binding**: Keep listeners on `127.0.0.1` by default. Only bind to public
   interfaces with explicit firewall rules for controlled private access.

2. **Credential Rotation**: Rotate credentials if:
   - A device is lost or stolen
   - Suspicious activity detected
   - Personnel changes affecting access

   ```bash
   # List credentials
   drover-server credentials list

   # Revoke compromised credential
   drover-server credentials revoke <credential-id>
   ```

3. **Legacy Token Deprecation**: Once all devices are paired, disable shared token:
   ```toml
   # ~/.drover/config.toml
   [auth]
   legacy_token_enabled = false
   ```

4. **File Permissions**: Ensure `~/.drover/` has restrictive permissions:
   ```bash
   chmod 700 ~/.drover
   ```

5. **Backup Security**: Encrypt backups and store separately from primary system.

### For Developers

1. **No Secrets in Code**: Never commit tokens, secrets, or credential files.
2. **Environment Variables**: Use `.env` files (not tracked) for configuration.
3. **Log Sanitization**: Strip sensitive data from application logs.
4. **Dependency Updates**: Keep Python dependencies current.
5. **Secure Development**: Follow OWASP guidelines for web application security.

## Security Features

### Authentication

- Bearer token authentication for all API endpoints
- Per-device and per-host credentials (not shared)
- Credential hashing: `sha256("drover-cred-v1\0" + token)`
- Pairing codes with time limits (10 min device, 15 min host)
- Rate limiting on pairing endpoint

### Network Security

- Default localhost-only bindings
- TLS-optional for remote connections
- Private network boundary enforcement (no public exposure)

### Data Security

- Credentials.json stores hashed tokens, never plaintext
- Sensitive data stored locally (`~/.drover/`)
- No external cloud storage or transmission
- Redaction policy support for summaries and context data

## Incident Response

If you suspect a security breach:

1. **Immediate Actions**:
   - Rotate credentials immediately
   - Revoke suspicious authentication tokens
   - Check access logs for unauthorized activity
   - Document all observed anomalies

2. **Gather Information**:
   - Capture evidence before remediation
   - Record timestamps of suspicious activity
   - Identify potential attack vectors

3. **Coordinate with Reporter**:
   - Provide updates to reporter if applicable
   - Confirm remediation success before disclosure

4. **Post-Incident**:
   - Document incident and lessons learned
   - Update security measures as needed
   - Coordinate public disclosure timeline

## Existing Security Documentation

For detailed technical security information, see:
- [Security Overview](./docs/security.md): Authentication, trust model, network configuration
- [Threat Model](./docs/threat-model.md): Comprehensive security threat analysis

## Vulnerability Disclosure Program

We maintain a responsible disclosure program for security vulnerabilities:

- **Acknowledgments**: Valid reports will be acknowledged in release notes
- **Bounty**: No monetary bounty at this time (community-run project)
- **Credit**: Contributors receive public credit unless they request otherwise

## Compliance

Drover v0.1 is designed for trusted personal use. It does not provide:
- Multi-tenant isolation
- SOC 2 or ISO 27001 compliance
- Enterprise audit logging
- Formal penetration testing certification

For enterprise deployments, perform your own security assessment and compliance
review.

## Open Source Contribution

Drover is Apache 2.0 licensed. We welcome security contributions via pull requests
and issue reporting through the proper channels.

---

_Last updated: 2024-12-XX_
