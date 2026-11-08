# Post3 claim matrix

| Claim | State | Evidence |
| --- | --- | --- |
| Cumulative source includes provider harness VARCHAR[] and MCP lake-read fixes | proven | 83bc264d then f1bd088e in Studio checkout |
| Exact public-main candidate wheel/lock | proven | 5793130, locked venv acceptance, wheel 71e6490d..., lock c64a84ba... |
| Fresh locked-vendor installed-wheel rebuild/verify/replay/search | proven | /Users/arnab/scratch/drover-publicmain-locked-acceptance-9c096a6ee7d5/acceptance.json |
| Private rollout health/relay/queue/planned-stop | proven | studio_rollout JSON and last-good snapshot |
| Existing recorded fixture search/replay after activation | proven | live-mcp-post3.json |
| Reads meet a sub-5-second server budget | not claimed | observed figures include CLI wall time |
| Real recap/handoff | open | Studio Claude CLI auth required |
| Public source freeze/version/tag/release | prepared/review pending | local exact head 5793130; v0.6.57 PATCH PROPOSAL only; tag availability and approval required |
| GitHub CI for exact public candidate | not run | requires later authorized push; local Studio tests are not CI |
