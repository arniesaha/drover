# Quality pilots: feature → proof map (Lane C, issue #1024)

**Status on this branch: P1 not run. P2 refactor candidate implemented and
checked as described below, not yet owner-accepted. P3 is recorded
separately and is not replayed here. Lane C is not accepted.**

Preparation originated at `7133442d` and is integrated with Lane A
(`b36305f6`) and the reviewed Lane B preflight increment. The canonical
cold-start recipe is in root `AGENTS.md`; the preflight contract is in
[`dev-preflight.md`](../operations/dev-preflight.md). Runtime profile and
execution slots stay `UNBOUND` until the [binding gate](#binding-gate).
Source integration and baseline pins are not executed pilot outcomes.

Case definitions:
[`tests/fixtures/quality-pilots/scenarios.json`](../../tests/fixtures/quality-pilots/scenarios.json).
They are data. No runner, score or service is added. The one executable piece
is the P2 pin.

## Evidence tiers

Each verdict states the tier it reached. A lower tier never stands in for a
higher one, and agreement between models or reviewers is not evidence.

| Tier | Meaning | Does not prove |
| --- | --- | --- |
| T0 implementation | A diff exists at a named SHA | That it works |
| T1 mocked test | Scoped `pytest` with fakes (stub provider, harness, HTTP session) | Real provider, transport, store or process boundary |
| T2 CI | The same SHA green in `.github/workflows/ci.yml` | Installed behaviour |
| T3 installed behavioural acceptance | The installed artifact driven through its real entry point, with results read back from the real system of record | Anything outside the exact case run |

## P1 · Bug fix (Lane A): acceptance cannot be forged

Lane A hardens **only exact opted-in GitHub issues**. `local_file` goals and
GitHub issues without a profile are deliberately unchanged. P1 therefore has
two parts with opposite expectations, and they must not be mixed.

Contract of record: [GitHub acceptance pilot](../operations/github-acceptance-pilot.md),
introduced at `b36305f6`.

### P1a · GitHub gate (the fix)

| | |
| --- | --- |
| Observable outcome | For an issue with a reviewed profile, a close happens only when trusted provider reads agree with that profile. Caller `behavioral_passed` / `checks_rerun` values grant nothing; an evidence label alone closes nothing. |
| Approved criterion / profile | The issue's entry in `work_queue.goals.github_acceptance_profiles`, installed by an independent owner: `pull_number`, `criteria`, `instructions`, `behavioral_check`, `app_id`, `owner_id`, `amendment_record`. Its revision is SHA-256 over the whole profile. The issue body must carry exactly one `<!-- drover:acceptance -->` section equal to `criteria`. Actual profile values: `UNBOUND`. |
| Source / build / context | Integrated SHA, installed runtime, repository, issue, PR, producer App: `UNBOUND`. |
| Request | `POST /work/update`, `action=verified`, `evidence_key=check-run:<id>:review:<id>`, after the separate verification session is terminal. |
| What the gate resolves | Current PR head; behavioural check with the configured name and App, success, at that head, `external_id` equal to the profile revision, still the latest run, non-empty output within 64 KiB; `APPROVED` review by the configured human owner at that head carrying `drover-acceptance:<revision>`; required branch-protection checks green; everything reread before the close intent. |
| Expected side effect | Positive: `200 {"outcome":"verified"}`, one close, audit row holding resolved source, check facts, evidence reference and result byte count/digest. Negative: issue stays open, no `verified` outcome. Lost close response then repeat: `deferred`, no second close. |
| Negative cases | `P1-N1`…`P1-N12` (forged flags and label, unresolvable evidence, stale head, changed profile or criteria, wrong or superseded check, missing or oversized output, wrong review, required CI, unfinished sessions, change at the final boundary, uncertain close); `P1-N12` held out. |
| T1 | `tests/test_work_queue_github_acceptance.py` at `b36305f6`, run with Lane A's own recipe in its `AGENTS.md`. Fixtures prove the code contract only. |

**Acceptance gates (T3, owner-performed; not satisfied by any test):**

1. The owner independently reviews and installs the profile.
2. The owner retrieves the retained result from
   `GET /repos/<repo>/check-runs/<check-id>` and reads `output.summary` and
   `output.text`.
3. The owner judges whether those observations establish the claim and
   whether the producer was isolated. A success conclusion is not that
   judgment, and the gate does not automate it.
4. Source head, profile revision, check-run, App and review identities match
   the audit row.
5. Stale and forged negatives, retry and interruption are observed on the
   installed gate where they can be produced without harm.
6. One real, subsequent, authorized PR goes through the installed gate with a
   single close.

This worker closes no live issue and mutates no service. It can prepare and,
after binding, run isolated cases and read back owner-supplied evidence.

### P1b · Compatibility regression (must not change)

| | |
| --- | --- |
| Scope | `local_file` goals and unprofiled GitHub issues. |
| Expectation | Identical to baseline. On these paths caller flags plus an opaque `evidence_key` are still what the service checks. The pilot does **not** expect them to reject opaque evidence; that would contradict Lane A's preserved compatibility. This is a stated limit of the fix, not a pass. |
| Cases | `P1-C1`…`P1-C6`, including that a `local_file` goal cannot declare an acceptance profile. |
| Commands | `tests/test_work_queue_execution.py`, `tests/test_work_queue_worker_mcp.py`, `tests/test_work_queue_local_candidates.py` (T1). |
| Boundary | The signed deferred work → verify → acknowledge chain stays `local_file`-only. The GitHub gate is a close gate; it creates no acknowledgement and proves no merge or deploy. |

## P2 · Behaviour-preserving refactor: verification run-key derivation

Chosen boundary: the verification request/run-key derivation, written out four
times in `service.py` (`launch`, `_verification_request`,
`advance_deferred_verification`, `update`), at baseline and still at
`b36305f6`. It is the smallest known duplicated boundary in the acceptance
path and is what makes a criteria change invalidate a verification. The
refactor is "one derivation, four callers"; see the candidate below.

| | |
| --- | --- |
| Observable outcome | Identical inputs give byte-identical request and verify run keys before and after. |
| Approved criterion | Equivalence, not review opinion: pinned digests match exactly and existing suites stay green with no test expectation edited. |
| Characterization pin | [`p2-run-key-vectors.json`](../../tests/fixtures/quality-pilots/p2-run-key-vectors.json): six vectors captured from the real source at `7133442d` (changed, null and non-ASCII body; other item; other parent). |
| Command | See [Running the checker](#running-the-checker). Exit 0 match, 1 mismatch, 2 pinned entry point missing or reshaped. |
| Expected side effect | None. The diff touches only the derivation sites. |
| Negative cases | `P2-N1`…`P2-N4`; `P2-N2` held out. |
| Owner verdict | Nix. T3 does not apply to a pure internal refactor; the verdict says so. |

Limits of the pin:

- It pins `identity()` and `WorkService._verification_request` for items with
  no acceptance profile (criteria = item body). The profiled GitHub path and
  the other three call sites are covered only by existing suites (T1).
- During preparation it returned 6/6 on baseline, exit 1 against an in-memory
  mutant that drops the criteria body, and 6/6 against `b36305f6`'s
  `service.py` loaded in memory over baseline sibling modules. The last is an
  indication, not a run of the integrated tree. None of this is a pilot result.

### P2 refactor candidate (not yet owner-accepted)

Implemented on top of `9ab079fd` as candidate `990d7e34`: one pure
`verification_identity(workspace_id, goal_id, number, parent_run_key, criteria)`
in `service.py`, called by the four sites. Each caller still resolves its own
criteria in the same place and order, so no authority check or provider read
moved; `_verification_request` keeps its signature.

What was actually run at `990d7e34`:

- Six pins: 6/6. A derivation that drops the criteria fails them (1/6, exit 1).
- The AGENTS isolated-PostgreSQL recipe with the `focused-tests-pg` preflight
  (own role, data directory under the run's scratch root, port 5432
  forbidden, clean tree): 17 passed, 0 skipped, 0 failed; the disposable
  cluster was stopped by the recipe's own trap. The 17 are the 9 tests in
  [`tests/test_quality_p2_derivation.py`](../../tests/test_quality_p2_derivation.py)
  and 8 existing cases:
  `test_work_queue_execution.py::test_separate_success_and_changed_criteria`,
  `test_work_queue_github_acceptance.py::test_provider_evidence_and_owner_review_close_once_despite_routine_notes`
  and `::test_stale_verdict_and_unfinished_verifier` (parametrized),
  `test_work_queue_local_candidates.py::test_bound_verifier_reports_then_queue_accepts_only_its_terminal_turn`.
- The new tests drive the real service on the unprofiled, profiled-GitHub and
  deferred `local_file` paths and compare stored keys and decisions with an
  independently written digest. Four seeded wrong derivations, across five
  control tests, each change a named behaviour (for example, dropping criteria lets a changed-criteria
  item be accepted).

History, kept because it happened: the first run, at checkpoint `a5fa2c47`,
was 15 passed, 2 failed. Both failures were wrong predictions inside the new
negative control about *which* behaviour a wrong derivation breaks; the
refactor and every real-derivation expectation were unchanged by the fix.

Limits:

- `launch` also stores every run key (work or verify) with its own
  role-generic expression. That is a fifth, deliberately untouched site; the
  helper's verify run key must equal it, and acceptance refuses if it does not.
- The three existing suites were not run in full at this candidate, only the
  cases named above. No CI run. `P2-N2` (owner-held fresh inputs) and the
  owner's profile/all-caller baselines are the owner's to compare.

## P3 · User-visible feature (Lane B): preflight CLI gives actionable context diagnostics

| | |
| --- | --- |
| Observable outcome | Before work starts, the operator learns from the shell whether the required context is usable and, if not, the first thing that failed, why, and what to do next. |
| Approved contract | Fail-fast: stop at the first failing prerequisite before any expensive work. "Actionable" is checked per diagnostic: it names the failing item, states the cause, states a concrete next step, and exits non-zero. |
| Source / build / context | Accepted B source at the owner-supplied SHA. T3 uses the installed entry point as a real process, not an in-process `CliRunner` call. One throwaway root per case. |
| Commands / paths | Command, flags, exit codes, prerequisite order, what counts as expensive work, message text: all `UNBOUND`. Existing surfaces named "preflight" (`drover lake preflight`, cutover `release-preflight`, `credentials issue-preflight`) are different features. |
| Expected side effect | None: tree and store identical before and after. No secret in output. |
| Negative cases | `P3-N1`…`P3-N6`; `P3-N5` held out. |
| Owner verdict | Nix, per diagnostic, with tier. |

`P3-N3` (two broken prerequisites) follows the fail-fast contract: the first
run reports the first failure only, with zero expensive work; after only that
one is remedied, the rerun surfaces the second; after both, exit zero.
Aggregate reporting of all failures in one run is not required and not
expected. The bar per diagnostic is unchanged.

## Held-out cases

Each class has one case marked `held_out`. Because this repository is visible
to candidates, only its property and expectation are committed; the owner
chooses the concrete inputs at execution. Expectations are not lowered at
binding. A case that cannot be expressed against the accepted source is
reported as a gap, not dropped.

## Running the checker

Supported context: Python ≥ 3.11 (`requires-python`), `uv` with the committed
lock. It needs only the base project, no extras. Proven during preparation
with CPython 3.14.7 and uv 0.13.6:

```sh
uv run --locked --python <python3> python tests/fixtures/quality-pilots/check_p2_run_key_vectors.py
```

Format checks on the checker use the project's pinned tools and need the `dev`
extra:

```sh
uv run --locked --python <python3> --extra dev black --check tests/fixtures/quality-pilots/check_p2_run_key_vectors.py
uv run --locked --python <python3> --extra dev isort --check-only tests/fixtures/quality-pilots/check_p2_run_key_vectors.py
uv run --locked --python <python3> python scripts/check_markdown_links.py docs/quality-pilots/README.md
```

The T1 suites named above need `--extra dev`; Lane A's GitHub acceptance suite
additionally follows the Postgres recipe in its `AGENTS.md`.

## Evidence storage

Two different things:

- **Worker-authorized paths.** A Lane C worker writes only
  `docs/quality-pilots/` and `tests/fixtures/quality-pilots/`. During
  preparation a worker write outside the worktree, to a scratch directory, was
  denied by the harness; no scratch write path is granted or assumed here.
- **Owner-managed evidence.** Run evidence (command, exit status, SHA,
  retrieved provider results, before/after state) is kept by the owner in a
  location the owner chooses, outside the repository and never in `/tmp`. A
  worker without such a grant returns its observations in its report for the
  owner to retain.

## Boundaries

- Automatic work → separate verification → native acknowledgement is proven
  only for configured `local_file` canary goals
  ([`verified-event-path-boundaries.md`](../operations/verified-event-path-boundaries.md)).
- The pilot manufactures no owner acknowledgement, performs no demo close of a
  live GitHub issue, and touches no production state.
- iOS physical-device acceptance and public-release gates
  ([`public-release-readiness.md`](../design/public-release-readiness.md)) are
  separate. Nothing here counts toward them.
- No generic confidence score. A verdict is per case: pass, fail or not run.

## Binding gate

Execution waits for the owner to supply: reviewed, integrated A and B source
with the exact current SHA; the installed profile and its revision; and an
isolated root.

Then, in the same Lane C session:

1. Fill every `UNBOUND` slot from the accepted source or installed runtime,
   not from this document.
2. Re-run the P2 checker at the pre-refactor SHA before any refactor starts.
3. Run the isolated cases, including `retry` and `interruption`, and read back
   the real criteria and provider evidence for P1a.
4. Report the denominator (cases defined / bound / run / passed / failed /
   not run), which P1a gates the owner performed, and the limits of what was
   exercised.
