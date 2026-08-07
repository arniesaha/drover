# Loop Engine — concepts, decisions, and Phase 0

Status: design of record (brainstormed 2026-08-06). Covers the conceptual
frame for long-horizon agent work in Drover, the architectural decisions taken
so far, and the design of Phase 0 — an instrumented manual loop that runs
before any schema is committed.

This is the first spec in the Loop Engine track. It deliberately does **not**
specify the production data model; several primitives are named as provisional
and are expected to change based on Phase 0 evidence.

---

## 1. Why this exists

Drover today drives sessions. A human decides what a session is for, starts it,
watches it, and decides when it is done. Every long-horizon objective — "keep
this project free of bugs", "hold the test suite under five minutes" — lives in
the operator's head, and dies when they stop paying attention.

The gap is not model capability. Current practice is fairly clear that on
long-horizon work, harness quality dominates model quality: agents stop early,
decompose badly, and lose coherence across context windows, and it is the
surrounding loop that has to design around this. Drover has the substrate for
that loop — durable transcripts, multi-host execution, a job ledger, cost and
token telemetry — and does not yet have the loop.

## 2. Four layers, kept separate

Most designs in this space fail by collapsing two of these into one. Naming
them separately is the main conceptual commitment of this document.

| Layer | Question it answers | Lifetime | Drover today |
| --- | --- | --- | --- |
| **Goal** | What are we trying to be true? | weeks–forever | absent |
| **Graph** | What work, in what order, blocked by what? | hours–days | partial (`tasks`, span forest) |
| **Loop** | How does one iteration run, verify, and decide to continue? | minutes–hours | absent |
| **Ledger** | What actually happened, durably, so we can resume? | forever | present (`pipeline_jobs`, `agent_events`) |

**Loop engineering** is layer 3: the iterative cycle that produces autonomy.
Its canonical primitive is the continuation loop — intercept the agent's attempt
to exit, re-inject the goal into a clean context window, and let iteration N+1
recover state written by iteration N. Fresh context per turn is a feature, not a
compromise: it is what stops long runs from degrading.

**Graph engineering** is layer 2: modelling work as nodes and edges rather than
a linear conversation. Agent graphs need *cycles* — retry, self-correct,
re-verify — which is why classic DAG schedulers do not fit directly.

**Ontology** is not a fifth layer. It is the type system layers 1–3 share: what
kinds of things exist (goal, container, task, finding, decision, skill), which
edges between them are legal, and — the part that pays off — what a claim
*means*, so that "this bug is fixed" is checkable rather than prose.
`context_containers` is already an ontology fragment: a closed `container_type`
vocabulary with `confidence` and `evidence` attached to every classification.
That evidence-and-confidence discipline is the thing to carry upward, because a
long-horizon loop mostly makes assertions about its own progress, and
unverified self-assertion is the primary way these loops rot.

## 3. What Drover already has

Assessed against the four layers, the ledger is in good shape and the rest is
missing.

Present and reusable:

- `pipeline_jobs` / `pipeline_job_attempts` / `pipeline_artifacts` — durable job
  state with `status` (pending / leased / succeeded / retry_wait /
  terminal_failed / dead_lettered / cancelled / superseded), lease ownership and
  expiry, attempt counting with `max_attempts`, backoff via `next_run_at`, and
  causal provenance through `caused_by_receipt_id`.
- `context_containers` — resumable, confidence-aware context keyed by
  `context_id` rather than repo, already carrying `next_action`, `open_loop`,
  `summary_md`, `session_ids`, `task_ids`.
- `decisions`, `session_summaries`, `project_briefs`, `curated_context_records`
  with `curated_context_provenance`.
- `spans` with `cost_usd`, prompt/completion/cache token counts, and `llm_model`
  — per-iteration cost attribution is a query, not new plumbing.
- A harness control plane that can already launch and drive sessions
  (§6).

Missing: a standing goal above `context_containers`; an iteration record; a
finding with a lifecycle and a dedup identity; explicit edges (relationships are
currently implicit in array columns and repo-column joins); and a budget model.

## 4. Decisions

### D1 — Artifacts live in git; claims live in the context store

Iteration state splits by kind. Code, tests, and patches are artifacts and live
in git. Findings, verdicts, iteration outcomes, and cost are *claims* and live
in the context store, with each claim carrying a git ref as evidence.

Rejected alternatives: filesystem-only state (agent-native and simple, but not
queryable — no cross-goal analytics, and nothing structured for a dashboard to
render); store-only state (fully queryable, but Drover would own state the agent
cannot see without a round-trip, and artifacts genuinely belong in git).

The cost of D1 is a sync contract between the two, which Phase 0 must exercise.
The benefit is that "this bug is fixed" becomes simultaneously verifiable
(git and test evidence) and queryable (a row that can be charted).

### D2 — No external durable-execution engine

The industry pattern is a two-layer stack: a durable execution engine for macro
orchestration plus a graph library for micro reasoning. Drover does not need the
first, because `pipeline_jobs` already provides leases, attempt limits, backoff,
dead-lettering, and causal provenance. A goal loop is a new job kind whose
activity is "run an agent session", not a new orchestration system.

Adopting one would also cut against the local-first pillar: a required server
process is exactly the kind of dependency the product exists to avoid.

Revisit only if Phase 0 shows the ledger cannot express iteration state
honestly.

### D3 — The control-plane store decision is deferred to Phase 0 evidence

`server/db.py` documents that every DuckDB connection must open read-write with
an identical configuration, and that `duckdb.connect()` is serialized
process-wide per path. The practical consequence is **one writer process, ever**.
Meanwhile `pipeline_jobs` performs lease acquisition and status transitions,
which is OLTP on an OLAP engine.

The two candidate end states are (a) keep control-plane state in DuckDB behind
the single server writer, or (b) split — SQLite or Redis for control plane,
DuckDB and parquet remaining the analytics lake.

Deciding now would be guessing. Phase 0 runs on existing tables and a scratch
table, measures actual contention and iteration write volume, and the decision
is made against that evidence. The provisional model in §7 is shaped so the move
to (b) would not require redesigning it.

Tracked as item B3 of [#40](https://github.com/arniesaha/drover/issues/40).

### D4 — Phase 0 is driven externally, through the existing harness API

The Phase 0 driver lives outside `src/drover/server/` and speaks to Drover over
its public harness API. It does not add a `pipeline_jobs` job kind, and it does
not run inside the server process.

Rejected alternatives: a pure filesystem continuation script that Drover only
observes (cheap, but it produces evidence about continuation loops in general
rather than about Drover as a substrate, which is what Phase 0 exists to learn);
an in-server job kind (most faithful to the end state, but it pre-commits the
store decision D3 defers, and puts experimental code in the release-critical
server process).

External-and-throwaway is what keeps D3 genuinely deferred: a scratch table can
be dropped, a job kind cannot.

### D5 — A bounded, checkable goal comes before an open-ended one

Phase 0 runs two goals in sequence (§6.4). The first has a machine-checkable
done-condition so the instrumentation can be validated against ground truth. The
second is open-ended bug discovery, which is the real target capability.

Running the open-ended goal first would mean debugging the loop and the observer
simultaneously with no way to tell which is wrong — an agent confidently
inventing findings and an agent genuinely finding them produce identical ledgers
until something checks them.

## 5. Primitives the design must eventually settle

Named here so Phase 0 knows what it is looking for. None are committed.

**Done-conditions and steady state.** The single highest-leverage move in
long-run practice is writing the exit condition before the agent starts. Drover
needs two goal shapes: *project* goals that complete, and *invariant* goals that
are never done and instead express a level to hold. "Keep this project bug-free"
is an invariant; modelling it as a project goal guarantees either a false
completion or an infinite loop.

**Finding identity and dedup.** A discovery loop without a dedup key
rediscovers the same defects forever and reports excellent productivity.
Findings need a lifecycle — open → confirmed → fixed → verified → regressed —
and a stable identity across iterations. The inverse failure matters too:
practice suggests not terminating on a clean pass, since sessions that validate
prior fixes also surface new defects.

**Verification cost.** The loop's verification step is the test suite. Drover's
is 1143 tests in 9m08s, so that is the floor on per-iteration cost for any
server-touching change. This makes suite parallelisation a Loop Engine concern,
not general tidiness (tracked in [#40](https://github.com/arniesaha/drover/issues/40)).

**The economy.** A loop without a budget model is a token fire. This needs an
account model (Claude subscription, Codex subscription, API key), quota
headroom, per-iteration cost attribution, and an exhaustion policy: pause,
degrade to a cheaper model, or hand off mid-goal to another subscription.
`spans` already carries `cost_usd`, tokens, and `llm_model`, so the ledger side
exists; the entitlement model and handoff rule do not.
[#17](https://github.com/arniesaha/drover/issues/17) is the first piece of this
and should be treated as control-plane accounting rather than row rendering.

**Role separation.** Reported practice separates planners (explore, emit tasks),
workers (execute, blind to the big picture), and judges (decide when an
iteration is done and whether to restart), and finds that behaviour depends more
on how each role is prompted than on the harness or the model. Phase 0 runs a
single undifferentiated role deliberately, so that the case for splitting is
made by evidence.

## 6. Phase 0 — instrumented manual loop

**Purpose: derive the schema from friction rather than specifying it
speculatively.** Phase 0 ships no production schema and no server changes. Its
deliverable is evidence.

### 6.1 Shape

A standalone driver script, outside `src/drover/server/`, that per iteration:

1. Launches a fresh session on a chosen host via
   `POST /harness/hosts/{host_id}/sessions`.
2. Sends the rendered goal brief as the opening turn via
   `POST /harness/sessions/{session_id}/messages`.
3. Watches `/harness/sessions/{session_id}/stream` until the driver reports the
   turn complete.
4. Runs the goal's verification command and records the result.
5. Appends an iteration record to a scratch table.
6. Terminates the session and starts the next iteration with a brief rendered
   from accumulated state.

Fresh session per iteration is the continuation property: clean context each
turn, with continuity carried by the brief (claims) and the working tree
(artifacts), per D1.

### 6.2 Existing API surface used

No new server endpoints are required:

- `POST /harness/hosts/{host_id}/sessions` — launch
- `POST /harness/sessions/{session_id}/messages` — send a turn
- `GET /harness/sessions/{session_id}/stream` — event stream
- `POST /harness/sessions/{session_id}/terminate` — stop
- structured drivers for `claude`, `codex`, and `gemini`

### 6.3 Scratch state

One scratch table, explicitly disposable, plus whatever the goal writes to git.
Provisional columns: iteration ordinal, goal id, session id, host id, harness,
start and end timestamps, verification command, verification exit code, claimed
outcome, observed diff refs, token and cost totals, and a free-text note.

The point of the free-text note is to capture what the fixed columns could not,
which is the main input to §7.

### 6.4 The two goals, in order

**Goal A — calibration (bounded, checkable).** Clear the annotation debt from
[#40](https://github.com/arniesaha/drover/issues/40) until `mypy` exits clean.
Ground truth is an exit code, so a loop that claims progress it did not make is
caught immediately. Validates: does the iteration ledger match reality, is cost
attribution correct, does the loop stop when the done-condition is met, does the
brief carry enough state across a context boundary.

**Goal B — the real capability (open-ended discovery).** Bug discovery on Drover
itself, with no completion condition. Surfaces the hard primitives: finding
identity and dedup, confidence and evidence, and what "done for now" means for
an invariant goal.

Goal B only starts once Goal A's instrumentation is trusted.

### 6.5 Success criteria

Phase 0 succeeds if it produces:

1. A recommendation on D3 backed by measured contention and write volume.
2. A finding-identity scheme validated against real rediscovery in Goal B.
3. Measured per-iteration cost and wall-clock, split between agent time and
   verification time.
4. A written list of every place the fixed schema was wrong — the direct input
   to the production model.

Phase 0 explicitly does **not** need to fix a useful number of bugs. A run that
finds nothing but produces trustworthy instrumentation has succeeded.

### 6.6 Safety and budget

Phase 0 runs against a branch, never `main`; opens pull requests rather than
pushing; has a hard iteration cap and a hard spend cap, both enforced by the
driver rather than by prompting; and is stopped by a human, not by the loop
deciding it is finished.

## 7. Provisional data model

Recorded as a sketch to be contradicted by Phase 0, not as a target.

- `goals` — standing objective; a `goal_kind` distinguishing project from
  invariant, a done-condition or held-level expressed as a runnable check, an
  owning `context_id`, and budget references.
- `goal_iterations` — one row per loop turn: observation, action, verification
  verdict, cost, and links to the session that produced it.
- `findings` — the discovery unit: dedup identity, lifecycle state, confidence,
  and evidence refs.
- `goal_edges` — explicit relationships (`blocks`, `derived_from`, `verifies`,
  `supersedes`, `regressed_by`), replacing today's implicit array-column and
  repo-column joins.

Each of these carries evidence and confidence in the manner
`context_containers` already establishes.

## 8. Non-goals

- No production schema in Phase 0.
- No changes to the server process in Phase 0.
- No external durable-execution engine (D2).
- No control-plane store decision before Phase 0 evidence (D3).
- No multi-role planner/worker/judge split until single-role evidence justifies
  it (§5).
- No fleet dashboard here. The home page is a separate, later sub-project that
  renders what this track produces.

## 9. Open questions

- Which host runs Phase 0, and does the driver need to survive host restarts
  during a run?
- How much of the brief should be rendered claims versus letting the agent read
  the working tree directly? D1 sets the boundary but not the ratio.
- Does an invariant goal need an explicit quiescence notion — "nothing new found
  in N iterations" — or is that the human's judgement in Phase 0?
- Does cross-harness handoff mid-goal (Claude to Codex) belong in Phase 0 as an
  economy test, or does it add a variable too many?

## 10. Related work

- [#40](https://github.com/arniesaha/drover/issues/40) — pre-Loop-Engine
  cleanup: CI gates, module altitude, framework calls. Item B3 is D3 above; the
  test-suite runtime comment is the verification-cost input to §5.
- [#17](https://github.com/arniesaha/drover/issues/17) — session token rollup;
  reframed as the first piece of the economy primitive.
- Nexus #178 — self-learning loop and shared skill registry. Downstream of this
  track: skill effectiveness is measured from iteration outcomes.
- `docs/design/context-containers.md` — the ontology fragment §2 builds on.
