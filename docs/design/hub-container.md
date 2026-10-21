# Hub Anywhere Design

## Purpose and decision

Drover's hub should be able to run in an operator-owned container on an
operator-owned cloud machine. This gives an operator without a spare,
always-on computer a durable hub while retaining the local-first execution
boundary: `drover-harnessd`, its agent CLIs, PTYs, worktrees, and local spools
continue to run on the operator's own machines. The hub coordinates; it never
becomes a remote agent-execution service.

The first supported targets are a small VPS, Fly.io or Railway where their
storage and private-network facilities meet the requirements below, and
Kubernetes/k3s. The deployment remains self-hosted: the operator owns the
cloud account, control database, lake data, backups, credentials, and network
policy.

The default network posture is private. The hub joins the operator's tailnet,
either through a Tailscale sidecar or `tailscale serve`, and clients use the
hub's HTTPS `ts.net` name. No hub port is published to the public internet.

`deploy/Dockerfile` is the reference hub image build. It runs only
`drover-server` as an unprivileged user and deliberately does not include a
harness CLI, Git, `sqlite3`, PostgreSQL client tools, Tailscale, a service
manager, or troubleshooting utilities. Compose and Helm templates remain
follow-up work; platform configuration must still meet the persistent-service
contract below.

For Railway or another generic container platform, provide (1) a private
PostgreSQL service for the control store and DuckLake catalog, (2) one durable
volume mounted at `/var/lib/drover` for the configured lake, incoming, DuckDB
selector and scratch paths, and (3) a mounted `/etc/drover/config.toml` plus
platform secrets. At minimum set `DROVER_CONTROL_DSN` to the control-store
DSN named by `[control_store].dsn_env`; set the catalog DSN variables named by
your `[analytics]` configuration when DuckLake is enabled, and set
`DROVER_API_TOKEN` (or mount the token file). The container command is its
default `drover-server --config /etc/drover/config.toml run`. Give it a
restart policy and use `/healthz` for liveness and `/readyz` for readiness.
Never use ephemeral filesystem storage for the lake. `systemd` and `launchd`
do not apply in a container; Tailscale is optional and belongs in a sidecar or
platform-supported private-network integration, never in the hub image.

## Goals and non-goals

Goals:

- Run the existing hub runtime on Linux/amd64 and Linux/arm64 without a macOS
  host assumption.
- Keep one trusted operator as the security boundary and keep harness hosts
  reachable through their existing outbound relay connection.
- Make persistent state, backup, restore, upgrades, resource limits, and
  readiness explicit and reproducible.
- Preserve the current private HTTPS pairing experience for iOS and web
  clients.

Non-goals:

- Moving `drover-harnessd`, agent processes, filesystem access, or local
  worktree execution into the cloud.
- Making public ingress a normal installation mode.
- Introducing a Drover-operated managed hub. That is a later,
  demand-driven product option, not an alternative implementation of this
  work. It would require real multi-tenancy and authorization boundaries,
  accounts/OAuth, a data-custody model, end-to-end encryption for transcripts,
  billing, incident response, and published availability and support SLOs.

## Runtime inventory

### Processes and state

The normal combined `drover-server` (`runtime.role = "all"`) is the initial
container entrypoint. It owns the authenticated HTTP/WebSocket fleet API,
pairing, relay endpoint, readiness and metrics surface, ingest watcher,
control-outbox exporter, advisory and content workers, provider refresh,
derived-memory workers, and optional MCP and OTLP listeners. It also starts
short-lived, bounded query children for verified lake reads. Split API and
analytics roles are already supported with PostgreSQL, but are not needed for
the first small deployment; the chart may expose them later.

The production control plane is PostgreSQL. It holds fleet and credential
records, event metadata and hot payloads, the durable outbox, derived-memory
jobs, summaries, briefs, embeddings, and serving projections. A PostgreSQL
container or operator-managed PostgreSQL instance is therefore required for
the supported cloud profile; a hub-local DuckDB control store is compatibility
mode, not the production target.

The analytical lake has two inseparable persistent parts:

- The DuckLake PostgreSQL catalog, including metadata and snapshot history.
- The lake data root, containing Parquet and delete files referenced by that
  catalog. A DuckDB selector/compatibility file, incoming data, and scratch
  space also need a writable persistent data volume where configured.

Persist the hub data root and PostgreSQL data on durable volumes. Do not put
either on the image filesystem, an empty ephemeral volume, or an object-store
mount without a separately reviewed DuckLake storage design. Parquet files
alone are not a recoverable lake.

Configuration should be a non-secret mounted config file plus explicit
environment-variable names for secret values. Secrets include the control and
catalog DSNs, API/boundary/host credentials, and any configured model-provider
credentials. They belong in the platform Secret mechanism or operator secret
manager, never in an image, chart values committed to source, command line, or
logs. If push is enabled, mount the APNs signing key read-only and supply its
key ID, team ID, bundle ID, and key path through the same secret/config
boundary. The hub makes outbound HTTPS calls to APNs when push is enabled, to
configured model, embedding, or provider endpoints, and to release/update
metadata when automatic updates are enabled. Tailscale also needs outbound
connectivity to its coordination and relay infrastructure.

### Resource profile

The image must ship Linux RSS measurement and expose the existing memory
block through `/readyz`. Current selected-DuckLake synthetic evidence measured
about 67 MiB steady parent footprint and a 73 MiB lifetime peak for the
exercised route set; it is useful sizing evidence, not a production promise.
The release gate is stronger: the full hub-memory fixture's
`peak_hub_memory` must remain below **1.5 GiB**. Run that gate on Linux in the
published image, rather than treating a developer macOS result as container
evidence.

The reference starter deployment is a 1-vCPU, 2-GB VPS running the combined
hub, local PostgreSQL, and a Tailscale sidecar. It is a low-concurrency
baseline, not capacity planning: reserve the bulk of RAM for the hub,
constrain PostgreSQL, leave headroom for Tailscale and the kernel, and alert on
memory/volume pressure. The current disposable query-child ceiling is 2 GiB,
so P1 must prove a safe container-specific child limit below the pod or VM
limit; it must not rely on overcommit. More concurrent clients, a large
backlog, embeddings, or broad analytical history require a larger profile.
CPU steady state is light (the API, workers, and polling loops), while startup,
DuckLake export, background derivation, and an admitted query child are the
peak CPU cases. One vCPU is acceptable for the reference profile only because
admission serializes expensive lake work; two vCPUs is the recommended first
scale-up.

### Portable replacements for host assumptions

The hub's macOS service story currently relies on launchd: `KeepAlive`, a
generous SIGTERM timeout for checkpointing, a managed environment file, and
the macOS update/runtime layout. A Linux container instead needs an init-free
foreground entrypoint, orchestrator restart policy, explicit `terminationGracePeriodSeconds`
or Compose `stop_grace_period`, and a pre-stop/drain contract which sends
SIGTERM and waits for the existing cleanup.

Provider helpers can read macOS Keychain credentials; that cannot be a cloud
hub dependency. Cloud configuration uses mounted secrets or a secret manager.
The harnesses keep their own local Keychain and CLI credentials because they
remain on their original machines. The container must also replace the
host-oriented wait-for-volume behavior with an init/startup check: wait only
for the named persistent volume and PostgreSQL readiness with a bounded
deadline, fail visibly if either is unavailable, and never silently start on
an empty fallback directory.

## Image design

Build a small, reproducible Linux image from a pinned Debian slim or distroless
runtime base, using a separate builder stage to install the locked Python
environment and required DuckDB/DuckLake assets. Pin the base image by digest,
the Python/runtime dependencies by the project lock, and every external
runtime artifact by version and digest/checksum. Publish a manifest list for
linux/amd64 and linux/arm64 from the same source revision.

The final image runs as a fixed, unprivileged UID/GID and has no package
manager, shell requirement, or writable application directory. Run it with a
read-only root filesystem, `allowPrivilegeEscalation: false`, dropped Linux
capabilities, and writable mounts only for the declared data, temporary/spill,
and (when needed) Tailscale state locations. A startup command creates neither
users nor persistent state outside those mounts.

Use `/healthz` for liveness: it answers whether the process is running even
when an analytical dependency is recovering. Use `/readyz` for readiness: it
is allowed to be non-200 when a served store is not usable. Do not put either
probe behind a public ingress or authenticate it from an unrelated pod. The
entrypoint must handle SIGTERM, stop accepting new work, allow the server's
existing cleanup/checkpoint path to finish within the declared grace period,
and exit nonzero on forced shutdown so the platform can replace it.

Set a measured initial compressed-image target of **under 300 MiB** for each
architecture. The exact size is a release metric, not justification for
removing required native database libraries or trust roots. CI emits an SBOM
for each image and architecture, scans it under the repository's release
policy, signs image digests and attestations with cosign, and records the
source revision and build provenance. Deployment references immutable image
digests; tags such as a release number are convenience selectors only.

## Packaging and operations

### Compose

P1 supplies a Compose deployment containing four explicit ownership
boundaries:

| Service | Responsibility | Persistent state |
| --- | --- | --- |
| hub | `drover-server` combined runtime, API, relay, workers, and query children | hub/lake data volume and mounted config/secrets |
| postgres | control store and DuckLake catalog, using separate databases/roles | PostgreSQL volume |
| tailscale | tailnet membership and private HTTPS proxy to the hub | narrowly scoped Tailscale state volume |
| backup job | operator-invoked, version-pinned backup/verification work | writes a new backup generation outside live volumes |

The hub and PostgreSQL have no published public ports. The hub listens on an
internal network address; the Tailscale sidecar is the only component reaching
it. Tailscale HTTPS terminates at its `ts.net` name and forwards privately to
the hub. Compose health dependencies are only startup ordering; the hub still
checks PostgreSQL and mounted state itself and readiness remains false until
they are usable.

### Helm

P2 supplies a chart with the combined hub in a StatefulSet, a PVC for hub/lake
data, Secret references for all credentials, a ConfigMap or mounted config for
non-secret options, resource requests and limits, and liveness/readiness
probes mapped to `/healthz` and `/readyz`. The default is one replica: it owns
local lake state and currently has no high-availability coordination contract.
The chart either deploys a private PostgreSQL dependency with its own PVC or
requires an operator-provided PostgreSQL endpoint and documents the catalog
and control-store database/role split. It must not expose PostgreSQL publicly.

The chart includes a Tailscale sidecar or an equivalent operator-approved
private tailnet integration. NetworkPolicy permits hub-to-PostgreSQL and only
the required outbound traffic; it denies public ingress by default. PVC class,
size, backup destination, node affinity, and restoration are explicit values,
not hidden assumptions about a local disk.

### Backup, restore, and upgrade

Back up a consistency-bound generation containing a PostgreSQL dump of the
control store, the DuckLake catalog, and every catalog-referenced lake file,
with checksums, manifest, watermark/receipt information, and encrypted
operator-owned storage. A PostgreSQL volume snapshot or Parquet copy alone is
not a backup. Restore only into fresh, isolated PostgreSQL and lake volumes;
verify catalog references, checksums, control readiness, and sample recall
before any cutover. P1 may automate a rehearsal but must not label an
unverified file copy as a successful backup.

Upgrades pin the current image digest, take or verify a current backup,
deploy one reviewed target digest, wait for `/readyz`, and exercise an
outbound-relay host and a paired client before retiring the previous digest.
Rollback is a controlled image/config rollback only after checking database
and lake compatibility. No automatic rollback may overwrite a newer volume.

Container releases use the same behavioral gates as native releases: the
memory fixture's 1.5-GiB peak requirement, analytical/control lock and
concurrency gates, migration/readiness tests, and a clean startup/shutdown
smoke. They run in CI against the built, read-only, non-root image on each
supported architecture or a documented architecture-equivalent runner. An
image is not releasable because its unit tests passed outside the image.

## Pairing and network flows

The paired iOS and web clients use the hub's private `https://<name>.ts.net`
advertised URL. The pairing QR embeds that HTTPS URL and the normal single-use
pairing material; users still review the destination before pairing. The
`ts.net` certificate removes the iOS/plain-HTTP limitation without making the
hub publicly reachable.

Every harness host should use relay mode. `drover-harnessd` opens an
authenticated outbound WebSocket to the hub's `/harness/relay` endpoint over
the tailnet and retains authority for its local agent process, filesystem,
PTY, worktrees, and host-local DuckDB spool. The cloud hub never dials a
harness host, so NAT, residential networks, and cloud-to-home inbound routing
are not new requirements.

The required hub changes are deliberately narrow: accept configuration via
environment variables and mounted files, make every data/scratch path
configurable beneath declared writable mounts, make startup dependency checks
portable, and remove launchd/Keychain/wait-for-volume assumptions from the
hub runtime path. Pairing and advertised-URL validation must accept the
private `https://*.ts.net` URL and continue to reject an accidental public
destination. Harnessd needs no cloud execution mode.

### Advanced: public ingress

Public ingress is explicitly unsupported by default and is not a one-click
option. An operator who deliberately enables it expands the attack surface
from a private single-operator network to internet discovery, credential
theft/replay attempts, TLS/proxy configuration mistakes, denial of service,
and exposure of health, pairing, API, WebSocket, MCP, and optional OTLP
surfaces. Bearer credentials are powerful: possession can control agents on
registered private machines.

Any future public-ingress path must be separately reviewed and opt-in, with
TLS terminated by an operator-controlled proxy, strict firewall and allowlist
policy, no public PostgreSQL or internal listeners, rate limits, request/body
limits, audit/alerting, constrained pairing, secret rotation and revocation,
and an updated threat-model acceptance test. It does not turn the product into
a multi-tenant service.

## Delivery plan

### P1 - image and Compose on a VPS (medium)

Deliver the signed multi-architecture image, Compose stack, private Tailscale
sidecar setup, persistent-volume contract, portable configuration/data paths,
and backup/restore rehearsal. Validate from a clean 1-vCPU/2-GB VPS: hub
starts from empty durable volumes, `/readyz` becomes healthy only after its
stores are ready, an iOS or web client pairs through the `ts.net` URL, and a
remote harnessd relays and completes a basic session. CI runs the image's
memory, lock, startup, and graceful-shutdown gates. Track in #735.

### P2 - Helm and k3s dogfood (medium)

Deliver the StatefulSet/PVC/Secret/probe/resource chart and private-network
policy, then dogfood it on Arnab's k3s cluster. Acceptance includes private
tailnet pairing, relay-host operation, an upgrade and rollback rehearsal, and
a restore drill to isolated storage. The chart must prove that its resource
limits and read-only security context pass the same image release gates.
Track in #736.

### P3 - operator documentation and one-click templates (small)

Publish deployment, security, upgrade, backup, restore, and incident-recovery
guidance; then add Fly and Railway templates which preserve the signed image,
durable volumes, Secret-backed configuration, and private-by-default network
posture. Validate templates from a clean operator account without embedding
any environment-specific endpoint or credential. Track in #737.

## Open questions

1. **Which Tailscale integration should Compose make default?** Recommend the
   official sidecar with a dedicated, tagged machine identity and persistent
   Tailscale state; keep `tailscale serve` as the documented bare-host option.
2. **Should P1 use one PostgreSQL instance for control store and DuckLake
   catalog?** Recommend one private instance with separate databases, roles,
   passwords, and least-privilege grants for the small profile; support
   operator-separated instances without requiring them.
3. **What is the container query-child memory limit on the 2-GB profile?**
   Recommend a measured 1.25-GiB child ceiling plus a 1.5-GiB hub release gate
   before calling the profile supported; reject admission rather than letting
   the cgroup OOM-kill the hub.
4. **Where do verified backup generations live?** Recommend an
   operator-controlled encrypted object store or separate attached volume with
   immutable/versioned retention; do not co-locate the only backup with the
   live VPS volume.
5. **When should public ingress be supported?** Recommend no P1–P3 work on it.
   Reconsider only after private self-hosted demand is established and a
   dedicated public-exposure threat-model review is accepted.
