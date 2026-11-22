# Cursor Agent CLI harness

The `cursor` harness drives the locally installed Cursor Agent CLI in its
documented per-turn print mode:

```sh
cursor-agent --print --output-format stream-json --stream-partial-output
```

It supports structured streaming, interruption, documented `--resume
<session-id>` continuation, dynamic model discovery from `cursor-agent models`,
and the standard interactive auth flow. It intentionally does not advertise
approvals, attachments, worktree isolation, usage accounting, or a separate
reasoning-effort control because the reviewed CLI does not provide compatible
headless contracts for those features. The driver never adds `--force`,
`--yolo`, `--trust`, or a sandbox override.

Models are never embedded in Drover: the authenticated local CLI is the source
of model IDs. An unauthenticated or malformed `status`/`models` response yields
an unavailable catalog rather than a default model or successful launch.
The catalog accepts up to 512 individually validated CLI rows and rejects a
larger listing rather than truncating it. Its public serialized envelope remains
bounded at 256 KiB.

## Authentication

Check the local state without starting a model turn:

```sh
cursor-agent status --format json
```

If it reports unauthenticated, the user signs in privately on the Studio:

1. In Drover, open the Studio host's Cursor harness authentication sheet.
2. Select **Sign In**. Drover runs `cursor-agent login` with Cursor's
   documented `NO_OPEN_BROWSER=1` handoff, displays the private URL returned
   by the CLI, and offers **Open Browser**.
3. Complete Cursor's normal browser login. Cursor stores credentials locally
   on the Studio; when its login process exits, Drover performs one bounded
   `cursor-agent status --format json` probe and shows authenticated or the
   returned error state.

Cursor documents this as a browser callback flow, not a device-code flow, so
Drover does not present a code-paste field. Do not copy tokens, callback URLs,
or credentials into Drover configuration, logs, or tickets. For break-glass
terminal troubleshooting only, Cursor's native command is `cursor-agent login`;
normal owner authentication should begin from the Drover entry point above.

## Included-use and billing boundary

As fetched from Cursor's [Models & Pricing](https://prod.cursor.com/docs/models-and-pricing)
on 2026-11-21, the new X Subscription has one weekly Cursor-model usage pool
shared by Cursor and Grok. Its included Cursor models are Grok 4.7, Grok 4.6,
Grok 4.5, and Composer 2.5. Third-party models are not included: on Premium
and above they use prepaid credit at API list price. Legacy Cursor plans use
different, monthly pools.

Arnab's stated $20 Grok subscription entitlement is unverified. Do not infer
that it covers Cursor until the authenticated Cursor account confirms plan and
model access. This harness neither purchases credit nor enables automatic
spend, and it does not change existing Codex defaults.

The reviewed Cursor CLI `status` schema establishes authentication, not
remaining allowance; `models` establishes availability, not entitlement. Its
documented help lists no quota or usage command, and Drover has no Cursor usage
probe. Drover therefore leaves Cursor quota unavailable rather than inventing a
percentage or treating a listed model as included-use confirmation.
