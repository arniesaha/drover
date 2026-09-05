# Privacy

Last reviewed: 2026-09-05

Drover is self-hosted software for a trusted personal fleet of coding agents.
The iOS app connects to a Drover hub that you or another operator run; it does
not require a Drover-operated cloud account. This page describes the data
flows in the source project. If someone else operates your hub, that operator
also decides how the hub and any integrations are configured.

## Information the app and hub handle

On the phone, Drover handles the server address, a one-time pairing code, the
phone's device name, and the device credential that pairing returns. The
credential is stored in the iOS Keychain; the server address is stored in app
preferences. The app uses those values to show and control the sessions that
your hub makes available, including session metadata, agent prompts and
responses, terminal output, approvals, handoffs, and any attachments you send
through the app.

To make interrupted work recoverable, the app can keep a protected local
recovery record containing a draft, an attachment, or a pending/deferred turn.
It uses opaque file names and a credential-bound recovery namespace. A normal
saved draft becomes eligible for cleanup after seven days; unresolved or
deferred sends are retained until they can be handled safely, so they are not
silently discarded.

The hub receives the data needed to run the fleet. That can include paired
device records and names, credential verifiers, APNs device tokens when push
is enabled, session and host metadata, terminal and agent events, and the
content needed for the commands and context features you use. The default
local data directory is `~/.drover/`; an operator can change configuration and
storage locations.

## Permissions and connections

The iOS app requests only the permissions needed for the features you choose:

- **Camera:** scan a Drover pairing QR code. The app does not use the camera
  for another purpose.
- **Local network:** connect to a hub on your private network.
- **Notifications:** show alerts, badges, and sounds when you allow them.

The app connects directly to the hub address you configure. Drover supports
localhost, trusted private LANs, and private Tailscale networks; it is not
intended for public-internet exposure. The current self-hosted server does not
provide TLS itself. If you configure an HTTP hub address, traffic to that hub
is not encrypted by HTTPS, so use a network and transport arrangement you
trust.

## Notifications

When notification permission is granted and iOS provides a device token, the
app sends that token and its APNs environment to your hub. A hub configured
for APNs may send attention alerts through Apple. The alert can include a
session identifier, the project directory's final name, the harness name, and
a shortened preview of the latest agent response. It can therefore be visible
on a lock screen or notification surface.

Notifications are best effort. They are not a guarantee of timely delivery,
and the app remains usable without them. Turn notifications off in iOS if you
do not want alert content displayed. Signing out removes the phone's local
connection but currently leaves its server credential and APNs registration in
place. Revoke the device credential on the hub to clear its stored registration
and stop hub-sent alerts for that credential.

## Optional integrations

Drover can be configured with optional services chosen by the operator. For
example, summaries and briefs can use a configured model provider or a locally
installed CLI, and embeddings can use an OpenAI-compatible endpoint or local
Ollama. Those providers receive the content submitted to their configured
worker under their own terms. The optional Pond archive integration is
loopback-only and read-only in the documented configuration; it is disabled by
default.

The iOS app does not create a separate publisher-run forwarding service for
these integrations. Review every provider, proxy, backup target, and network
service that your operator enables.

## Retention and deletion

There is no single retention period for all hub data. The operator controls
the hub, its backups, and its optional integrations. The default configuration
sets a seven-day retention period for processed incoming audit copies; it does
not automatically erase all session history, context data, credentials, or
operator-created backups.

To remove the phone's local connection, use **Sign Out** in the app. It removes
the phone's credential, saved server address, and local chat-recovery store
after credential deletion. If iOS protected storage is unavailable, the app
reports that cleanup is incomplete so you can retry. Signing out does not
revoke the corresponding server credential. From the hub, list and revoke the
device credential with `drover-server credentials list` and
`drover-server credentials revoke <credential-id>`.

Because the hub is self-hosted, there is no Drover-hosted account or central
data store for the project to erase on your behalf. The person operating the
hub must also manage retained context, backups, and data sent to optional
providers.

## Security and questions

The app keeps its bearer credential in the iOS Keychain. The hub stores a
verifier for that credential rather than the plaintext credential, but its
local data can still contain sensitive work content. Keep the hub and its data
directory private, and review the [security guide](security.md) before changing
network exposure or sharing a device.

For non-sensitive product questions or documentation corrections, use the
[public issue tracker](https://github.com/arniesaha/drover/issues). Do not post
credentials, private addresses, logs, prompts, or session content there. For a
security vulnerability, follow the private reporting instructions in
[SECURITY.md](../SECURITY.md).
