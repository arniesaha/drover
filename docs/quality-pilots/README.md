# Quality pilots: feature → proof map (issue #1024, Lane C)

Three task classes, one pilot each: a bug fix (P1), a behaviour-preserving
refactor (P2) and a user-visible feature (P3). This page is the contract and
case map. What was actually run, by whom and at which source, is in
[final evidence at `87990e95`](final-evidence-87990e95.md).

**Status at `87990e95911d3897fcc0d2a195f0650d67fbf277`:**

- P1: one real outcome accepted by the owner through the installed gate.
- P2 and P3: the owner reports passing proof at this source; owner review and
  integration of this documentation are still open.
- The cohort is **not** declared accepted here, and issue #1024 stays open.

Case definitions:
[`tests/fixtures/quality-pilots/scenarios.json`](../../tests/fixtures/quality-pilots/scenarios.json).
They are data. No runner, score or service is added.

## Evidence tiers

Each verdict states the tier it reached. A lower tier never stands in for a
higher one, and agreement between models or reviewers is not evidence.

| Tier | Meaning | Does not prove |
| --- | --- | --- |
| T0 implementation | A diff exists at a named SHA | That it works |
| T1 scoped test | `pytest` against fakes or an isolated real PostgreSQL store | Real provider, transport or installed process |
| T2 CI | The same SHA green in `.github/workflows/ci.yml` | Installed behaviour |
| T3 real entry point | The installed gate, or the real script process, driven for an exact case with results read back from the system of record | Anything outside the exact case run |

The three pilots reach different tiers on purpose: P1 is a real installed
outcome, P2 is an internal-equivalence proof with no rollout tier, and P3 is a
real script process from the source checkout. They are not interchangeable.

## P1 · Bug fix: acceptance cannot be forged

Contract of record:
[GitHub acceptance pilot](../operations/github-acceptance-pilot.md). The gate
hardens **only exact opted-in GitHub issues**; `local_file` goals and
unprofiled issues are deliberately unchanged.

### P1a · GitHub gate

| | |
| --- | --- |
| Observable outcome | For an issue with an installed profile, a close happens only when trusted provider reads agree with that profile. Caller `behavioral_passed` / `checks_rerun` values grant nothing; an evidence label alone closes nothing. |
| Profile | The issue's entry in `work_queue.goals.github_acceptance_profiles`: `pull_number`, `criteria`, `instructions`, `behavioral_check`, `app_id`, `review_mode`, `owner_id` (human mode only), `amendment_record`. Its revision is SHA-256 over the complete profile. The issue body carries exactly one `<!-- drover:acceptance -->` section equal to `criteria`. |
| Two verdict modes | `human_approval` (the default): the configured human owner's `APPROVED` review at the current head carrying `drover-acceptance:<revision>`; evidence key `check-run:<id>:review:<id>`. `independent_check` (explicit): the configured App check is the verdict source and no review is fetched or synthesized; evidence key `check-run:<id>`. The installed profile selects the mode; a caller cannot. |
| Both modes resolve | Current PR head; the behavioural check with the configured name and App, success, at that head, `external_id` equal to the profile revision, still the latest run, non-empty output within 64 KiB; required branch-protection checks green; everything reread before the close intent. |
| Request | `POST /work/update`, `action=verified`. |
| Expected side effect | Positive: `200 {"outcome":"verified"}`, one close, audit row holding resolved source, check facts, evidence reference and result byte count and digest. Negative: issue stays open, no `verified` outcome. Repeat after an outcome: the recorded outcome, no second close. Repeat after an intent with no outcome: `deferred`. |
| Negative cases | `P1-N1`…`P1-N12`; `P1-N12` held out. |

`independent_check` does not turn an App or agent result into a human review
and does not waive repository branch protection. Arnab, as the sole human
decision-maker, reviews and installs either complete profile.

### Settled sessions

Acceptance needs a settled work session and a separate settled verifier
session. Two forms qualify:

- **Completed lifetime.** A queue-created or retained session with status
  `completed` and the named turn's correlated successful exit. This is the
  ordinary path and is unchanged.
- **Strict resumable turn.** Only for a retained pair named by an installed
  `github_existing_session_bindings` entry. A structured session may remain
  `running` between turns when all of these hold: the named input is the
  latest durable input; its terminal result is final in that window (Codex:
  correlated `exited: 0`; Claude Code: the complete success result with zero
  queued turns); no later input, error or status follows; and the owning
  daemon reports the same session `awaiting: input` with `turn_active: false`.
  `awaiting: input` or `turn_complete` alone never qualifies, and a queue link
  without such a binding still needs `completed`.

### Binding history and idempotency limits

`POST /work/bind_existing` carries only the goal and issue; installed
configuration selects the pair. It launches nothing and a repeat returns the
same links. Known limits, all fail-closed:

- A superseded pair stays as append-only history and cannot reach a verdict
  once the installed binding changes.
- A new pair may replace an old one only while every existing link holds
  nothing but its single binding event. Any progress, verification intent or
  outcome, or uncertain close requires inspection instead.
- A later turn of an already-bound session is referenced through the binding
  event; the link's session column stays empty and the one-owner-per-session
  rule is not relaxed. This needs the same issue, role, profile revision and
  source head.
- The verifier link's identity derives from the work link and the source. A
  change to the verifier turn alone cannot be rebound; work and verifier turns
  must change together.
- Neither bound session may receive further input between the final bind and
  `verified`.
- A daemon that does not report `turn_active` cannot satisfy the resumable
  form.

### P1b · Compatibility regression (must not change)

| | |
| --- | --- |
| Scope | `local_file` goals and unprofiled GitHub issues. |
| Expectation | Identical to baseline: caller flags plus an opaque `evidence_key` are still what the service checks there. This is a stated limit of the fix, not a pass. |
| Cases | `P1-C1`…`P1-C6`. |
| Boundary | The signed deferred work → verify → acknowledge chain stays `local_file`-only. The GitHub gate is a close gate; it creates no acknowledgement and proves no merge or deploy. |

## P2 · Behaviour-preserving refactor: verification identity

Boundary: the verification request/run-key derivation, formerly written out in
four places in `src/drover/server/work_queue/service.py` (`launch`,
`_verification_request`, `advance_deferred_verification`, `update`). It is
what makes a criteria change invalidate a verification. Landed in PR #1030
(`d6be93c7`) as one pure `verification_identity(...)` with four callers; each
caller still resolves its own criteria in the same place and order.

| | |
| --- | --- |
| Observable outcome | Identical inputs give byte-identical request and verify run keys before and after. |
| Criterion | Equivalence, not review opinion: pinned digests match exactly and existing suites stay green with no expectation edited. |
| Pin | [`p2-run-key-vectors.json`](../../tests/fixtures/quality-pilots/p2-run-key-vectors.json): six vectors captured from pre-refactor source at `7133442d`. |
| Behavioural regression | [`tests/test_quality_p2_derivation.py`](../../tests/test_quality_p2_derivation.py) drives the real service on unprofiled, profiled and deferred `local_file` paths against an independently written digest, and seeds wrong derivations that must change a named behaviour. |
| Negative cases | `P2-N1`…`P2-N4`; `P2-N2` held out. |
| Tier | T1 with a real isolated PostgreSQL store, plus T2. T3 does not apply to a pure internal refactor. |

Limits: the six pins cover items with no acceptance profile; the profiled path
and the other call sites rest on the suites. `launch` also stores every run
key with its own role-generic expression, a fifth site left untouched; the
helper's verify key must equal it and acceptance refuses if it does not.

## P3 · User-visible feature: preflight gives actionable diagnostics

Contract of record: [developer preflight](../operations/dev-preflight.md).

| | |
| --- | --- |
| Observable outcome | Before work starts, the operator learns from the shell whether the required context is usable and, if not, the first thing that failed, why, and what to do next. |
| Contract | Fail-fast: stop at the first failing prerequisite before the `--then` command. Every failure names the check, states the cause and carries a non-empty concrete action. |
| Command | `sh scripts/quality/preflight.sh --out <dir> [--expect-base] [--python] [--verify-receipt] [--profile] [--then '{python}' …]` |
| Exit codes | 0 pass; 1 `--then` failed; 3 environment unavailable; 4 harness failure; 130 interrupted by SIGINT. |
| Check order | tools, worktree and base, readable inputs, output directory, project interpreter, PostgreSQL (PG profile), receipt verification. |
| Expected side effect | A failing preflight starts no `--then` command and writes no receipt. Each passing invocation writes its own receipt. No secret in output. |
| Negative cases | `P3-N1`…`P3-N6`; `P3-N5` held out. |
| Tier | T3 as a real script process from the source checkout. Not installed-server behaviour. |

`P3-N3` follows fail-fast: one failure per run, in order, each surfaced after
only the previous one is repaired. Aggregate reporting is not expected.

The preflight is not an exactly-once wrapper for arbitrary commands. A retried
or interrupted `--then` is the caller's to guard and reconcile; the receipt
certifies the environment only.

Other surfaces named "preflight" (`drover lake preflight`, cutover
`release-preflight`, `credentials issue-preflight`) are different features.

## Held-out cases

Each class has one case marked `held_out`. Only its property and expectation
are committed; the owner chooses the concrete inputs and runs it. Expectations
are not lowered at binding.

## Reproducing the checks

Supported context: Python ≥ 3.11 and `uv` with the committed lock.

```sh
# P2 pins (base project only): exit 0 match, 1 mismatch, 2 entry point reshaped
uv run --locked python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py

# Links in these pages
uv run --locked python scripts/check_markdown_links.py docs/quality-pilots/README.md \
  docs/quality-pilots/final-evidence-87990e95.md
```

The PostgreSQL-backed suites (P1 service controls and the P2 regression) run
through the isolated recipe in root `AGENTS.md`, "Fresh-session, scoped
verification". It creates its own disposable cluster under a new directory in
`~/scratch`, runs the `focused-tests-pg` preflight, and stops only that
cluster. The P3 recipe is in the preflight contract linked above.

## Boundaries and exclusions

- Three pilot cases are not a statistical reliability result and say nothing
  general about autonomy.
- The observation window is execution to the current checkpoint only. No
  longitudinal or regression-over-time claim is made, and no timing, token or
  intervention metric is reported.
- Physical-device iOS, APNs, Reduce Motion and public-release gates
  ([`public-release-readiness.md`](../design/public-release-readiness.md)) are
  excluded. Nothing here counts toward them.
- Native acknowledgement remains proven only for configured `local_file`
  canary goals
  ([`verified-event-path-boundaries.md`](../operations/verified-event-path-boundaries.md)).
- No generic confidence score. A verdict is per case: pass, fail or not run.

## Corrections to earlier versions of this page

Kept so the history is not mistaken for the current contract:

- **Human approval was once the only verdict path.** Earlier text required an
  `APPROVED` review by a configured human owner for every P1 acceptance and
  listed that as an acceptance gate. That requirement was superseded by the
  explicit `independent_check` mode. The accepted P1 outcome used that mode.
  **The human-approval criterion was not met and is not claimed as met**; no
  human approval was created or represented. `human_approval` remains the
  default for any profile that does not opt out.
- **Terminal lifetime was once required.** Earlier text required both sessions
  to be terminal. The strict resumable-turn form above is the corrected,
  narrower contract.
- **`UNBOUND` slots.** Earlier versions left P1 and P3 bindings unbound and
  stated that no pilot had run. Those slots are now bound in the fixture.
- **P3 history.** The first process run, at `70a332c1`, failed two cases
  because seven diagnostics had no next step, and an interrupted run produced
  a traceback with no result line. Those failures stand as recorded; later
  repairs do not turn them into passes.
- **P2 history.** The first run of the new negative control predicted the
  wrong broken behaviour for two seeded derivations (15 passed, 2 failed). The
  control was corrected; the refactor and real-derivation expectations were
  not changed.
