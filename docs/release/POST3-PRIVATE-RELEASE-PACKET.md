# Drover post3 private release packet

## Artifact identity

- Candidate wheel: drover-0.6.56.post3-py3-none-any.whl
- Wheel SHA-256: ab0be22a6af2e829ce7c73ea0c941670b64152a70f4469d68fcfba12ea1e09f6
- Source identity is proven in the dedicated Studio source checkout /Users/arnab/scratch/drover-post3-cumulative: f1bd088e04c263d00422b34c2b21d8b9a5eb5a32. This packet worktree is independently based on private a17b4da, so its local directory is not used as post3 provenance.
- Private 0.6.56.post3 is an acceptance artifact, not a proposed public tag.

## Accepted installed-wheel evidence

Fresh Studio venv /Users/arnab/scratch/drover-post3-fresh-20261108 imported from its own site-packages, with DuckDB 1.5.5 and engine SHA-256 85fad85339c7e345eabb66a33a25d8503c3ee8881f9d298fa8499a97617dbf68.

A newly initialized owned PostgreSQL 17 Unix-socket cluster plus matching-root DuckLake used product rebuild, verify, and configure_analytics APIs and pinned extensions. The fixture used exact provider/outbox schemas, including VARCHAR[].

- drover_session_replay(post3-session): status ok, marker present, 0.970 s.
- drover_search(marker, post3-session): status ok, two real marker hits, 0.984 s.
- Rebuild/verify: 2 canonical events, 1 provider snapshot, 1 outbox row; cross-partition check passed.
- Owned fixture PostgreSQL stopped in finally.

Evidence: /Users/arnab/scratch/drover-post3-acceptance-8b3a9cd6ea9a/acceptance.json and probe.log. Summary/handoff was intentionally not exercised; no live handoff success is claimed.

## Fixture regression fixed

The first standalone builder failed with KeyError: VARCHAR[] while materializing the exact provider_usage_snapshots schema. The accepted builder maps that declared type to a PyArrow string list and passed the product rebuild/verify route. This is acceptance-fixture coverage, not a source-suite pass. Focused CI must instantiate all declared provider/outbox types and assert rebuild/verify round-trip.

## Public release assessment

- Public main: 47d962c2e32e04c70ae92c12b797076f79b9ac10; public v0.6.56: 84f30212a87486ea12243eca6b97c1a39f92144b.
- Latest observed CI for public main is successful, but this worktree is private a17b4da, not public main.
- No public version or source freeze is proposed. Minimal integration: apply the MCP-read change and exact-schema fixture test onto clean public main, build a new uniquely-versioned candidate, and run focused Studio acceptance plus CI.
- Do not promote post3 to a public tag merely because private acceptance passed. A public candidate requires immutable public source, wheel and lock digests, and CI for that commit.

## Required focused CI

1. Lake-backed MCP replay/search/summary skip redundant precheck while retaining disposable serving reads.
2. Exact-schema VARCHAR[] provider fixture rebuild/verify.
3. Installed-wheel Studio replay/search matching-root fixture.
4. Public CI for the selected immutable release commit.

## Activation and known limitation

Post1 remains last-known-good. Post3 source/wheel/lock binding is now proven: Studio source f1bd088e, wheel SHA-256 ab0be22a6af2e829ce7c73ea0c941670b64152a70f4469d68fcfba12ea1e09f6, and uv.lock SHA-256 c64a84ba229e80eb83d362ae475e4e27eeb16229367cffe5fa2406d0eebdc88a. The product service-role backup failed closed because drover_v052 lacks SELECT on two owner-created ops_backup_intents tables. The existing sanctioned owner backup route then captured the full 74-table drover_control schema with PostgreSQL pg_dump snapshot semantics (writers were not quiesced), included both required table-data entries, and passed product verify_backup. Verified receipt: /Users/arnab/scratch/drover-post3-activation-evidence/v2/20261108T130538Z; dump SHA-256 137fdc33853038cf9724e1cc98413d7604cf91864aaf5e393dbf3079aaca57df; 4,370,913,208 bytes.

The public-semver cutover release-preflight rejects 0.6.56.post3, but it is not the private rollout route. The existing supported Studio controller scripts/studio_rollout.py accepts a unique private runtime alias while enforcing plist/environment parity, engine pin, settled work queue, health, relay reconnection, planned-stop clear, and rollback preservation. It activated private-0.6.56.post3-f1bd088 from the fresh exact-wheel venv with byte-identical candidate plists. It passed loopback/Tailscale hub and harnessd health, relay reconnection (Studio and NAS), and queue checks; a new last-good plist snapshot is retained.

Live post3 MCP evidence: /Users/arnab/scratch/drover-post3-private-rollout-candidate/live-mcp-post3.json. The recorded fixture session harness-80dceee0-233e-443b-b6fd-a2ae276b5106 returned status ok and DROVER_LIVE_TURN_OK through both search (5.086 s) and replay (5.101 s). This proves only the recorded fixture reads; no handoff success is claimed. Studio Claude CLI OAuth (calm-river) is expired. When the user is ready, complete the existing native Studio Claude CLI login, then rerun only the queued recap/handoff read. Do not create another flow, copy tokens, or claim handoff passed first.

## Publication

No public tag, release, push, PR, post, email, deployment, or App Store action has occurred. Final publication needs human approval.
