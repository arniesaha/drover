# Quality pilots: feature → proof map (Lane C, issue #1024)

**Status: preparation only. No pilot has been executed and none has passed.**

Written against baseline `7133442d`. Lane A (work-queue acceptance fix) was
reported merged while this was being prepared, and Lane B (preflight CLI) was
not; neither accepted source was inspected. This map therefore names no new
A/B function, flag, command, exit code or message. Every `UNBOUND` slot is
filled only from owner-supplied accepted source at the
[binding gate](#binding-gate).

Case definitions:
[`tests/fixtures/quality-pilots/scenarios.json`](../../tests/fixtures/quality-pilots/scenarios.json).
They are data. No runner, score or service is added. The one executable piece
is the P2 pin, because it has value today.

## Evidence tiers

Each verdict states the tier it reached. A lower tier never stands in for a
higher one, and agreement between models or reviewers is not evidence.

| Tier | Meaning | Does not prove |
| --- | --- | --- |
| T0 implementation | A diff exists at a named SHA | That it works |
| T1 mocked test | Scoped `pytest` with fakes (for example the in-memory provider and harness in `tests/test_work_queue_execution.py`) | Real transport, store or process boundary |
| T2 CI | The same SHA green in `.github/workflows/ci.yml` | Installed behaviour |
| T3 installed behavioural acceptance | The installed artifact driven through its real entry point in an isolated root, with side effects read back from durable state | Production, GitHub-backed or iOS behaviour |

## P1 · Bug fix (Lane A): acceptance cannot be forged

| | |
| --- | --- |
| Observable outcome | A work item closes only on a real, separately produced verification result. Asserted success flags, or an evidence label alone, leave it open and record no `verified` outcome. |
| Baseline defect (`7133442d`) | `WorkService.update(action="verified")` in `src/drover/server/work_queue/service.py` trusts caller-supplied `behavioral_passed` / `checks_rerun` and accepts any `evidence_key` matching the opaque-code pattern. `BoundWorkControl.update` in `src/drover/server/work_queue/worker_mcp.py` hard-codes both flags to `True`. Nothing retrieves what the key names. |
| Approved criterion / profile | The `acceptance` block of `src/drover/server/work_queue/policy-v1.json` (separate verification run, behavioural evidence required, relevant checks rerun; criteria owner `nix`). Run profile: `UNBOUND`. |
| Source / build / context | Accepted A source at the owner-supplied SHA; isolated control store and data root; a `local_file` canary goal only. |
| Commands / paths | T1 at baseline: `uv run --locked --extra dev pytest -q tests/test_work_queue_execution.py tests/test_work_queue_worker_mcp.py tests/test_work_queue_local_candidates.py`. A's own tests and the T3 entry point: `UNBOUND`. |
| Expected side effect | Positive: one `outcome=verified` row in `decision_events`, one close, retry returns the same outcome. Negative: no close, no `verified` outcome row, item state unchanged. |
| Negative cases | `P1-N1`…`P1-N6`; `P1-N5` held out. |
| Evidence capture | Command, exit status, SHA, and before/after `decision_events` and item state per case. |
| Owner verdict | Nix, per case, with tier. |

## P2 · Behaviour-preserving refactor: verification run-key derivation

Chosen boundary: the verification request/run-key derivation, written out four
times in `service.py` at baseline (`launch`, `_verification_request`,
`advance_deferred_verification`, `update`). It is the smallest known
duplicated boundary inside the acceptance path, it is pure (`identity()` is
SHA-256 over a JSON array), and it is what makes a criteria change invalidate
a verification. The refactor is "one derivation, four callers". If accepted A
already consolidated it, the pilot picks the next duplicated pure boundary and
says so; it does not invent a product change.

| | |
| --- | --- |
| Observable outcome | Identical inputs give byte-identical request and verify run keys before and after. |
| Approved criterion | Equivalence, not review opinion: pinned digests match exactly and existing suites stay green with no test expectation edited. |
| Characterization pin | [`p2-run-key-vectors.json`](../../tests/fixtures/quality-pilots/p2-run-key-vectors.json): six vectors captured from the real source at `7133442d`, covering changed body, null body, non-ASCII body, other item, other parent. |
| Source / build / context | Pre- and post-refactor SHA of one lineage; no store or network. |
| Commands / paths | `uv run --locked python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py` at each SHA (0 match, 1 mismatch, 2 pinned entry point missing), then the P1 baseline suites. |
| Expected side effect | None. `git diff --stat` touches only the derivation sites. |
| Negative cases | `P2-N1`…`P2-N4`; `P2-N2` held out. |
| Evidence capture | Checker output at both SHAs, held-out differential output, test output, diff stat. |
| Owner verdict | Nix. T3 does not apply to a pure internal refactor; the verdict says so. |

What the pin does and does not cover: it pins `identity()` and
`WorkService._verification_request` and checks they agree. The other three
call sites are covered only by the existing suites (T1). During preparation
the checker returned 6/6 on baseline and exit 1 against an in-memory mutant
that dropped the criteria body (`P2-N1`). That shows the pin discriminates; it
is not a pilot result.

## P3 · User-visible feature (Lane B): preflight CLI gives actionable context diagnostics

| | |
| --- | --- |
| Observable outcome | Before work starts, the operator learns from the shell whether required context is usable and, if not, which item failed, why, and what to do next. |
| Approved criterion / profile | "Actionable" is checked per diagnostic: it names the failing item, states the cause, states a concrete next step, and failure is distinguishable by exit status. Profile: `UNBOUND`. |
| Source / build / context | Accepted B source at the owner-supplied SHA. T3 uses the installed entry point as a real process, not an in-process `CliRunner` call. One throwaway root per case. |
| Commands / paths | Command, flags, exit codes, context items and message text: all `UNBOUND`. Existing surfaces named "preflight" (`drover lake preflight`, cutover `release-preflight`, `credentials issue-preflight`) are different features. |
| Expected side effect | None: tree listing and control-store state identical before and after. No secret in output. |
| Negative cases | `P3-N1`…`P3-N6`; `P3-N5` held out. |
| Evidence capture | argv, stdout, stderr, exit status, before/after tree listing; output checked for secret material before it is kept. |
| Owner verdict | Nix, per diagnostic, with tier. |

## Held-out cases

Each class has one case marked `held_out`. Because this repository is visible
to candidates, only its property and expectation are committed; the owner
chooses the concrete inputs at execution and runs it. Expectations are fixed
here, before A/B were seen, and are not lowered at binding. A case that cannot
be expressed against the accepted API is reported as a gap, not dropped.

## Boundaries

- Automatic work → separate verification → native acknowledgement is proven
  only for configured `local_file` canary goals
  ([`verified-event-path-boundaries.md`](../operations/verified-event-path-boundaries.md)).
  The pilot stays inside that. It closes no live GitHub issue, touches no
  production state and manufactures no owner acknowledgement.
- iOS physical-device acceptance and public-release gates
  ([`public-release-readiness.md`](../design/public-release-readiness.md)) are
  separate. Nothing here counts toward them.
- No generic confidence score. A verdict is per case: pass, fail or not run.

## Binding gate

Execution waits for the owner to supply: reviewed, integrated A and B source
with the exact current SHA; a valid profile; and an isolated root.

Then, in the same Lane C session:

1. Fill every `UNBOUND` slot from the accepted source, not from this document.
2. Re-run the P2 checker at the pre-refactor SHA before any refactor starts.
3. Run each case with real criteria and real evidence retrieval, including the
   `retry` and `interruption` cases.
4. Keep evidence under `~/scratch/quality-pilots/<sha>/<pilot>/`, never `/tmp`.
5. Report the denominator (cases defined / bound / run / passed / failed /
   not run) and the limits of what was exercised.
