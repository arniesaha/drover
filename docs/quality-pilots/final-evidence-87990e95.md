# Final evidence at `87990e95` (issue #1024, Lane C)

Source: `87990e95911d3897fcc0d2a195f0650d67fbf277` (protected merge of PR
#1039). Contract and case map: [README](README.md).

**How to read this page.** The results below are the integration owner's
receipts, recorded here by the documentation author, who did not rerun them.
Raw logs, session data and private runtime details stay with the owner and are
not committed. Public GitHub identifiers are given so the provider-side facts
can be read back independently.

**What this page does not say.** The cohort is not declared accepted. P1 has
one accepted real outcome; P2 and P3 have passing owner proof and await owner
review and integration of this documentation. Issue #1024 remains open.

## Summary

| Pilot | Class | Tier reached | Source | Result |
| --- | --- | --- | --- | --- |
| P1 | bug fix | T3, installed coordinator gate, one real case | task PR #1029 head `5202c235`; gate at `87990e95` | accepted by owner: one verified outcome, one close |
| P2 | refactor | T1 with isolated real PostgreSQL, T2 | PR #1030 `d6be93c7`; rerun at `87990e95` | owner reports equivalence held |
| P3 | feature | T3, real script process from the checkout | `87990e95` | owner reports all run cases passed |

These tiers differ and are not comparable: a refactor's suites, a feature's
script process and a real installed outcome prove different things.

## P1 · Real installed outcome

Pilot issue: [#1031](https://github.com/arniesaha/drover/issues/1031).

| Fact | Value |
| --- | --- |
| Source task | PR #1029, head `5202c2359483a58b2916c58acac5f11ee2df20b8` |
| Gate source | PR #1039, reviewed head `8f925c69`, merged as `87990e95`; server CI run `46090787017` success; iOS run `46090787003` success on attempt 2 |
| Profile | revision `228c9ccced9eac01eea57b8971d83195ab3f25f18e1870b30283f6a0128aed7c`, `review_mode = independent_check`, App `15368` |
| Behavioural check | check run `140725060148`, `cohort1024-independent-quota-outcome`, completed/success at the task head, `external_id` equal to the profile revision |
| Check content | 36 tests, 0 failures, 0 errors, 0 skipped; strict boundary pass; owner-held forbidden import rejected; direct unknown-quota `route()` denial with reason `quota or capability unavailable` |
| Retained output | 4398 bytes, SHA-256 `0dcb81a55a3137360263b945ac345e02de95cb456e8fbe09c285958f3dc5f2f0`; the owner reports this equals the digest in the durable decision record |
| Required checks | `Build and test iOS app` (`140317535336`) and `build-and-test` (`140317535272`), both success, App `15368`, at the task head |
| Producer | protected-main workflow at `5abbeda3` (PR #1036), run `46063992370`: a credential-free proof job, then a separate publish job |
| Sessions | latest work and verifier turns each passed the strict resumable-turn reader; bind called twice returned the identical pair and launched nothing |
| Outcome | one `verified` intent and outcome; one close of #1031 (issue event `37524913278`) |
| Retry | two further exact `verified` requests each returned `verified`; audit unchanged; still exactly one close |
| History | all three earlier bind-only links remain |

Accepted-milestone records:
[#1031 comment](https://github.com/arniesaha/drover/issues/1031#issuecomment-7232323523)
and
[#1024 comment](https://github.com/arniesaha/drover/issues/1024#issuecomment-7232323736).

Things to know about this outcome:

- **No human approval.** The profile used `independent_check`. No GitHub
  human approval was created, required or represented, and the earlier
  human-approval criterion is not claimed as met. Arnab authorized a
  sole-human policy for this cohort; the integration owner independently
  reviewed and installed the profile under that authority. No personal review
  or installation by Arnab is recorded or implied.
- **First response not retained.** The first HTTP response to `verified` was
  not kept. The owner reconciled the durable record and the provider state as
  successful before sending the two retries. This is not the designed
  "intent without outcome returns `deferred`" case.
- **Negatives are not live.** Forged, stale, required-CI and producer controls
  were exercised in the scoped real-PostgreSQL service suites at this source,
  not by fault injection against the live gate. The earlier expectation of
  live injection and a live interruption is not claimed as satisfied.
- **Held-out `P1-N12` not separately reported.** No owner-chosen
  different-source receipt exists. Committed tests at this source refuse
  related cases (`test_wrong_repository_source_cannot_close`,
  `test_mismatched_behavioral_evidence_cannot_close`,
  `test_stale_verdict_and_unfinished_verifier` in
  `tests/test_work_queue_github_acceptance.py`); that is source-bound coverage,
  not the held-out case.
- **Independent read-only review.** Before acceptance, a separate verifier
  session inspected the check output, all four command logs and their hashes,
  the JUnit counts, the recomputed profile revision, the branch-protection
  record and the producer source. That review is an input to the owner's
  judgment, not a human gate.
- **Scope.** One issue, one PR, one profile. It shows the gate closed once on
  matching provider facts; it is not a general statement about live GitHub
  acceptance.

## P2 · Refactor equivalence

| Check | Owner-reported result at `87990e95` |
| --- | --- |
| `tests/test_work_queue_execution.py`, `tests/test_work_queue_local_candidates.py`, `tests/test_work_queue_github_acceptance.py` and `tests/test_quality_p2_derivation.py`, through the isolated recipe in root `AGENTS.md` | 168 passed, 0 skipped; own cluster stopped |
| Six committed pins | 6/6 |
| Held-out `P2-N2`: three fresh inputs (Unicode, multiline, empty, null) | digests equal those of pre-refactor `85ace5d5` |
| Profiled path: routine-note invariance, substantive-change invalidation, stale-profile refusal before any new source read | same as baseline, three before and three after |
| Seeded wrong derivations in the new suite | rejected by behaviour |
| Format, links, strict boundary | clean |

The refactor's author is also this page's author and is not its reviewer; the
four-caller source review and every result above are the owner's. Scope is
internal equivalence. It makes no claim about live GitHub behaviour and has no
rollout tier.

## P3 · Feature as a real process

The owner ran `scripts/quality/preflight.sh` as a process at `87990e95` with
the locked interpreter of that source. It was not a `CliRunner` or mocked
call.

| Case | Owner-reported result |
| --- | --- |
| Valid context (`P3-P1`) | exit 0, receipt written, owned marker created by `--then` |
| Negative process cases (`P3-N1`, `P3-N2`), reported in these categories: malformed, stale, missing, expired, wrong-source, no-git, unwritable | each exit 3, naming the check and cause with a non-empty concrete action, same text on stderr; no marker |
| Failed PostgreSQL query, reported separately | exit 3 with an actionable diagnostic |
| Sequential failures (`P3-N3`): base, output and interpreter broken together | first failure only on each run, no effects; after the last repair the marker is created |
| Retry (`P3-P2`) | a new receipt per valid invocation; the guarded marker is not duplicated |
| Secret (`P3-N4`) | fake secret-shaped DSN not printed |
| Held-out (`P3-N5`): wrong source | fails for the intended mismatch |
| Interruption (`P3-N6`): real SIGINT while `--then` runs | exit 130, `harness_failure`, `interrupted`, no traceback, receipt retained, no effect; two reruns exit 0 with exactly one owned marker |

Retained as it happened: the owner's first attempt used an interpreter from
another checkout and failed with `import:drover`, as the contract intends.
Only the interpreter was corrected; no criterion or host setting changed.

Limits: this is the script process, not installed-server behaviour. The
preflight makes no exactly-once claim for arbitrary `--then` commands; the
caller reconciles partial effects.

### Earlier P3 run, kept as history

The first process run was at `70a332c1`. Of 14 failing-check runs, all 14
stopped before `--then` with exit 3, but only 7 gave a concrete next step.
Missing-git, a non-Python `--python`, an unwritable `--out`, all three receipt
refusals and a failed PostgreSQL query had an empty action. An interrupted run
ended with tracebacks and no result line. `P3-N1` and `P3-N2` therefore
**failed at `70a332c1`**. The diagnostics and interruption handling were
repaired afterwards (the owner references `3ac07728`); those repairs are what
the `87990e95` run exercises. The earlier failures are not converted into
passes.

## Exclusions

- Not statistical: three cases, one per class.
- Observation window: execution to this checkpoint only. No longitudinal
  claim; no timing, token or intervention metrics.
- Excluded gates: physical-device iOS, APNs, Reduce Motion, public release.
- No production fault was injected and no account policy or quota was changed
  for these runs.

## Remaining

- Owner review of this documentation, normal CI and protected merge.
- Owner verdicts for P2 and P3, and the cohort decision on #1024.
