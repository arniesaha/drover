# Drover support

Drover is self-hosted software for one trusted operator and their coding-agent
fleet. The most useful support report starts with the local setup path and
keeps credentials and work content out of public channels.

## Start here

1. Follow [Getting Started](getting-started.md) to install the hub and a local
   harness on the first computer.
2. Build the phone app with the [iOS guide](../apps/drover/README.md), then
   pair it with a private server address.
3. Confirm an authenticated supported agent and readable project can complete
   a small task before adding another host.

The supported network boundary is localhost, a trusted private LAN, or a
private Tailscale network. Do not expose the hub directly to the public
internet. The [security guide](security.md) and [multi-host guide](multi-host.md)
cover the operating model and adding another machine.

## Pairing and connection problems

A pairing code is single use and expires after ten minutes. Generate a fresh
one if it has expired. You can scan the QR code or use **Or enter it by hand**
in **Pair & Connect** when the camera is unavailable.

For a physical phone, the pairing address must be reachable from that phone.
The first-computer setup stores the advertised address in the hub configuration;
loopback works only for a simulator on the same computer. If the connection
path is unclear, run the read-only check below after selecting the host,
harness, and project:

```sh
drover-server setup-check --host HOST --harness HARNESS --project PROJECT
```

Add `--json` when you need structured local diagnostics. The check does not
change services or credentials.

## Notifications

Notifications require the iOS permission, a device token from Apple, and a hub
that has been configured to send APNs alerts. They are best effort; an alert is
not a promise of immediate background delivery. The app also refreshes its
session view while it is running and through scheduled background refreshes
when iOS permits them.

An APNs alert can show a session identifier, project directory name, harness
name, and a shortened agent-response preview. Treat notification surfaces as
potentially visible to others. Disable Drover notifications in iOS if that is
not suitable for your work.

## Removing a phone connection

Use **Sign Out** in the app to remove the local credential, saved server
address, and local chat-recovery data. It does not unregister the phone's
notification token or revoke the corresponding server credential. Disable
Drover notifications in iOS to stop alerts from being displayed, or have the
hub operator revoke the credential to clear its APNs registration and stop
hub-sent alerts:

```sh
drover-server credentials list
drover-server credentials revoke <credential-id>
```

This is the recommended step for a lost, sold, or shared phone. The hub's
session history, context data, backups, and any optional-provider data remain
under the operator's control; see the [privacy page](privacy.md) for details.

## Before opening a public issue

Check the hub first:

```sh
drover-server status
drover-server doctor
```

Then include the Drover version, iOS version, whether the phone is on a
private LAN or Tailscale, the feature you were using, expected and observed
behavior, and sanitized steps to reproduce. Remove bearer tokens, pairing
codes, private hostnames or addresses, logs, prompts, terminal output, and
screenshots containing private work.

File non-sensitive bugs and documentation requests in the
[public issue tracker](https://github.com/arniesaha/drover/issues). Do not use
public issues for a security vulnerability; follow the private reporting
instructions in [SECURITY.md](../SECURITY.md).

## Current scope

Drover is designed for a trusted personal fleet. It does not provide a hosted
control plane, multi-user isolation, RBAC, or SSO. Optional context, model,
and archive integrations are configured by the hub operator and can have their
own service terms and support channels.
