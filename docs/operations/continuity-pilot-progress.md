# Pilot continuity: substantive progress and prepared human holds

This is an opt-in rule at the existing `/continuity` decision boundary, not a
new scheduler, acceptance service, or generalized native-owner migration.
Legacy callers keep their current behavior. An authenticated owner may submit
`policy: {"require_advancement": true}` on a bounded pilot request.

## Facts and decisions

`advancement` is a bounded list of `{ref, at}` entries. A ref is an immutable
artifact or receipt identity, and at is its original provider/evidence time,
not the time of a poll, comment, or receipt retransmission. A repeated ref is
normalized once at the earliest supplied time. The clock starts at issue
creation if no advancement is present. Status comments, check-only changes,
worker activity, and external heartbeats do not reset this progress clock or
mint a new recovery wake identity. In this mode the substantive stall budget
replaces activity-based worker/CI deadlines; the existing nudge then stall
protocol and post-nudge grace window remain.

`recovery` retains bounded `scope`, `checkpoint`, `result`, and `next_action`
strings in the existing durable wake payload. A retry of the same decision
returns the existing wake and its original checkpoint, not a second effect.
A genuinely new advancement ref establishes a new checkpoint identity.

An explicit `blocked:human` label ALWAYS stops actions. With the pilot rule,
an absent or partial `human_handoff` packet reports
`none / blocked_human_unprepared`. A packet containing all of `artifact`,
`tested_capability`, `requested_action`, and `resume_when` reports
`none / blocked_human`. This diagnoses incomplete preparation; it never
removes a human label, grants authority, approves a candidate, or converts an
internal task into a human decision. Fields are bounded nonempty text, not
independently verified proof. The owner must finish all authorized preparation
and independently verify capability and decision provenance before reporting
a review-ready hold.

## Client and trust boundary

The provider/client must project immutable commit/head/artifact receipts and
their original times into advancement, preserving the exact issue, owner and
session binding. A generic comment, CI timestamp, draft packet, or changed
observation time is not a substantive receipt. The owner judges whether an
artifact advances the objective; authenticated fact submission is not acceptance
verification. Keep genuine child human holds separate from a parent that still
has authorized executable work. Submitting a prepared packet does not resolve
the human decision or the independent producer requirement.

This rule is product-enforced when explicitly selected. The OpenClaw owner's
choice of authorized next action, notification wording/suppression, and the
NAS client's GitHub projection remain separate client policy. Merging or
installing this change alone does not prove that a live caller selected it,
that GitHub marker writes were exactly once, or that Telegram suppressed an
unchanged blocker. Those require their own actual path receipts. No live
producer/profile permission is changed by this implementation.

## Focused proof

From the repository root, after `uv sync --locked --extra dev`, run:

```sh
mkdir -p "$HOME/scratch"
proof_root=$(mktemp -d "$HOME/scratch/continuity-pilot-proof.XXXXXX")
sh scripts/quality/preflight.sh --out "$proof_root" \
  --python "$PWD/.venv/bin/python" --expect-worktree "$PWD" \
  --expect-base "$(git rev-parse HEAD)" --profile focused-tests --then \
  "$PWD/.venv/bin/python" -m pytest -q \
  tests/test_continuity_advancement.py tests/test_continuity_loop.py \
  tests/test_continuity_hub.py
```

Use a fresh owned output root. Tests cover the status-only regression,
unchanged snapshots, check churn, new and duplicate immutable receipts, genuine
human holds with missing/complete preparation, malformed packets, legacy
compatibility, and a real disposable control-store outbox lease/consume/ack
retry retaining scope/checkpoint/result with one durable effect. These are
bounded product-path regressions, not a live GitHub accepted outcome or an
OpenClaw owner-delivery acceptance claim.
