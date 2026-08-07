# Roadmap

## Now

- Stabilize the public v0.1 source release and keep Python/iOS CI green.
- Make the source-build path reproducible for the server and iOS client.
- Publish the command-plane architecture and context-store data model.
- Complete public Drover naming while preserving documented read compatibility
  for historical `nexus.*` telemetry.
- Verify private LAN and Tailscale use on a physical iPhone.

## Next

- Replace the shared fleet token with host-bound relay credentials.
- Improve install, upgrade, and service packaging for macOS and Linux.
- Complete supported sign-in and launch flows across agent harnesses.
- Improve derived-context freshness, repair workflows, and observability.
- Add a push path for timely background attention notifications.
- Lay the groundwork for long-horizon agent work: standing goals that outlive a
  single session, a durable iteration record, and per-iteration cost
  accounting. Starts with an instrumented trial loop run against Drover itself
  before any schema is committed — see
  [the Loop Engine design](superpowers/specs/2026-08-06-loop-engine-phase-0-design.md).

## Later

- Define portable context and provenance interchange contracts.
- Expand context enrichment, retrieval, and cross-agent handoff integrations.
- Evaluate a smaller host daemon after the protocol stabilizes.
- Explore longer-horizon trace storage and analytics.
- Surface fleet usage, quota headroom, and skill effectiveness as an operator
  home page over the long-horizon record.

The [public issue tracker](https://github.com/arniesaha/drover/issues) contains
accepted user-visible work. Private deployment details and dogfood operations
are intentionally tracked elsewhere.
