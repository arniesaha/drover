# Drover Paperclip completion contract

Paperclip can coordinate Drover reliability work, but Drover only accepts work as
complete when the board state, GitHub state, repository state, and runtime
evidence agree. A Paperclip comment that says "done" is not enough.

This contract applies to every Drover issue delegated through Paperclip,
especially work linked to GitHub issues.

## Required completion evidence

A Paperclip issue may be accepted as `done` only when its final comment or
handoff contains all applicable fields:

| Evidence | Requirement |
| --- | --- |
| Paperclip issue | Paperclip identifier and final status. |
| GitHub issue | Linked GitHub issue URL/number and final state, when one exists. |
| Durable artifact | Branch, commit, PR, merged PR, documentation path, or explicit no-PR rationale. |
| Verification | Test command/output, CI URL, live validation command/output, or explicit not-tested rationale. |
| Runtime/data quality | `drover-server quality --json` snapshot when the change can affect ingestion, workers, embeddings, summaries, ledgers, attribution, or handoff quality. |
| Deployment | Deployed commit and service health when the change affects the Mac Mini runtime. |
| Backup | Backup path before any live DuckDB/PVC/state mutation. |

## Status alignment rules

- `done` Paperclip issues linked to GitHub issues must point to a closed GitHub
  issue, or explain why the GitHub issue remains open as a follow-up.
- `done` implementation issues must point to a durable artifact outside the
  Paperclip pod PVC. A pod-local workspace path is recovery evidence, not
  completion evidence.
- `in_review` may be used for work awaiting user/board review, but the review
  comment must say what is missing before `done`.
- `blocked` must name the blocking authority or state, such as board-only plan
  confirmation, missing credentials, or an upstream runtime failure.
- If a Paperclip run claims implementation work and no branch/commit/PR exists,
  inspect the PVC workspace before declaring the work missing.

## Runtime-facing issue checklist

For runtime-facing Drover changes, the final handoff should include:

```text
Paperclip: AGE-<id> <status>
GitHub: #<id> <open|closed>
Artifact: PR #<id> / commit <sha> / docs path / no-PR rationale
Validation: test command + result, CI status, or not-tested rationale
Deploy: host + commit + service health
Quality: status + score + key warnings from drover-server quality --json
Backups: paths for any live DB/PVC mutations
Remaining work: issue ids or explicit "none"
```

## Checker

`scripts/check_paperclip_completion.py` validates an exported evidence bundle.
It is intentionally offline: callers can export data from Paperclip/GitHub with
whatever credentials they have, then run the checker without exposing tokens.

Example:

```bash
python3 scripts/check_paperclip_completion.py evidence.json
```

Minimal evidence shape:

```json
{
  "items": [
    {
      "paperclip_id": "AGE-50",
      "paperclip_status": "done",
      "github_issue": 158,
      "github_state": "closed",
      "artifacts": ["https://github.com/arniesaha/drover/pull/169"],
      "validation": ["uv run pytest"],
      "quality_required": true,
      "quality_snapshot": {
        "status": "warn",
        "score": 0.65,
        "generated_at": "2026-06-20T15:56:48Z"
      },
      "deployed_commit": "5a32839",
      "service_health": "com.drover.server running",
      "backups": [
        "/Users/arnabmac/.drover/backups/drover.duckdb-pre-change.bak"
      ],
      "remaining_work": ["#151"]
    }
  ]
}
```

The checker exits non-zero when required evidence is missing or when a `done`
Paperclip issue is still linked to an open GitHub issue without a documented
follow-up rationale.
