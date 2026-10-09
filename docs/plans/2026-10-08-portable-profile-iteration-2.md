# Portable profile iteration 2

References: #545, #558. Base: origin/main at 0c96054.

## Scope and sequence

1. Make markdown import private by default. Explicit tiers are ceilings:
   sensitive ancestor headings and obvious PII can only restrict visibility.
   Show heading keys and classification reasons in dry runs and retain reasons
   in proposal history and accepted item provenance. Cover synthetic headings,
   body patterns, nesting, fences and explicit tiers.
2. Add operator tier changes through the existing proposal and revision audit
   path. Issue and revoke credentials using the PostgreSQL credential store and
   profile_agents registry. Keep trusted reads on authenticated HTTP.
3. Add oldest rendered item age alongside the existing rendered-source watermark.
   Test bounded single-call HTTP and MCP bundles, including empty and omitted data.
4. Document copyable client startup integrations under docs/integrations for
   Claude Code, Codex and OpenClaw/Hermes. Clients own startup orchestration.

Reuse existing JSON provenance and credential tables where possible; no schema
migration is anticipated. If needed, append migration 16 with a hash pin.
No production import, enablement, generated content, UI changes or external
configuration edits are part of this work.

## Verification and delivery

Run only:

```sh
uv run --extra dev pytest -q tests/test_profile_*.py tests/test_mcp_contract.py tests/test_mcp_server.py tests/test_postgres_schema_migrations.py
```

Check git diff --check and added text for private data and em dashes. Commit
small implementation steps, then open a draft PR into main referencing both
issues. Do not merge.


## Review 1: credential scope isolation

Keep import behavior unchanged. Replace issued host credentials with a dedicated
profile scope and no host identity. Use a literal method/path allowlist permitting
only GET /profile, enforced before HTTP dispatch so public and special-case
handlers cannot bypass it. Exclude restricted scopes from generic token auth
and browser-session exchange. Generic operator listing and revocation retain
support for profile credentials.

The control_credentials scope column has no database CHECK constraint, so no
migration is needed. Test trusted reads, non-profile route denials, operator
listing/revocation, browser-session refusal, and retirement of a same-named host.
Run the original scoped verification plus tests/test_web_auth.py. Leave the
unrelated analytical timing test unchanged.
