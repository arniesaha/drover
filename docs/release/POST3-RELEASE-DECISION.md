# Post3 release decision

## Decision

Private activation is accepted. Public release remains open, not approved.

## Accepted evidence

- Cumulative Studio source: f1bd088e04c263d00422b34c2b21d8b9a5eb5a32, after 83bc264d (provider harness VARCHAR[] preservation) and public baseline 84f30212.
- Exact wheel: drover-0.6.56.post3-py3-none-any.whl, SHA-256 ab0be22a6af2e829ce7c73ea0c941670b64152a70f4469d68fcfba12ea1e09f6.
- Exact lock: uv.lock SHA-256 c64a84ba229e80eb83d362ae475e4e27eeb16229367cffe5fa2406d0eebdc88a.
- Fresh installed-wheel matching-root acceptance passed product rebuild/verify plus replay/search.
- Private Studio rollout succeeded through scripts/studio_rollout.py with health, relay, queue, planned-stop, and rollback safeguards.
- Current runtime alias: private-0.6.56.post3-f1bd088.
- Recorded live fixture session replay/search both returned status ok and DROVER_LIVE_TURN_OK. CLI wall times were 5.101 s and 5.086 s respectively; these are not server-query-budget measurements.
- Fresh owner-route metadata backup retained at /Users/arnab/scratch/drover-post3-activation-evidence/v2/20261108T130538Z. It contains 74 control-schema tables, passed product verify_backup, and is a PostgreSQL-consistent dump snapshot, not an all-store-quiesced snapshot.

## Rejected candidate

Post2 is not a release candidate because, although it included the provider harness VARCHAR[] schema/type correction, it lacks the later accepted MCP history-read change that routes replay/search/summary through _lake_read instead of _canonical_read.

## Public source and version decision

Public main is 47d962c2e32e04c70ae92c12b797076f79b9ac10; latest public release/tag is v0.6.56 at 84f30212a87486ea12243eca6b97c1a39f92144b. Public main CI observed successful.

A local integration branch is prepared from public main 47d962c2: 3406a1a applies the 83bc264d provider-harness correction, 37650a0 applies the f1bd088 MCP lake-read correction, and f5a3da8 assigns the unique private package identity 0.6.56.post3+publicmain47d962c. The four modified core files hash-identically to the accepted cumulative post3 source. Studio focused provider-schema tests passed 3/3. A local candidate wheel was built at /Users/arnab/scratch/drover-public-release-candidate-artifact/drover-0.6.56.post3+publicmain47d962c-py3-none-any.whl with SHA-256 3d5369ab19db7c2cb01006a4a94f08c0bc7531c296a852e884c1ab264182668f. The separate installed-wheel rebuild/MCP acceptance and live fixture replay/search evidence applies to the hash-identical core patch; this new local packaging artifact was not activated. No next public semantic version is proposed. Parent review must choose the public version only after this exact candidate has a lock/checksums, focused CI, and a separately accepted installed-wheel run. 0.6.56.post3 and the local version are private evidence, not public tags.

## Remaining gate

Run on Studio when ready: /Users/arnab/.local/bin/claude auth login --claudeai

Then retry the existing recap job and consume the real handoff. No new OAuth flow, token copy, or synthetic summary is permitted.
