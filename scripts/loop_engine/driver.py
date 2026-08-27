"""The loop: one iteration at a time, with the caps in code rather than prose.

Shape, per §6.1 of the Phase 0 design:

1. launch a fresh session on a host;
2. send the rendered goal brief as the opening turn;
3. wait until the turn is done;
4. run the goal's verification command and record the result;
5. append an iteration record to the scratch ledger;
6. terminate the session, and render the next brief from accumulated state.

Fresh session per iteration is the continuation property. A clean context each
turn is what stops long runs degrading; continuity is carried by the brief
(claims) and the working tree (artifacts), which is decision D1.

§6.6 is enforced here and not by asking the agent nicely: a hard iteration cap,
a hard spend cap, and no completion condition that the loop can talk itself
into. The loop stops when the check passes, when a cap is hit, or when a human
stops it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .api import HarnessApi, HarnessApiError
from .brief import render
from .goals import Goal
from .ledger import Iteration, ScratchLedger, utcnow
from .tree import head_commit, is_reachable
from .usage import Usage, from_events

log = logging.getLogger("loop_engine.driver")

#: States that mean the agent has stopped working and is waiting on us.
_SETTLED_AWAITING = {"input", "approval", "permission"}
#: Session statuses that mean there is nothing left to wait for.
_FINISHED_STATUS = {"exited", "terminated", "failed", "completed"}


@dataclass(frozen=True)
class Caps:
    """The two limits the design insists the driver owns.

    A cap that lives in the prompt is a suggestion. A cap read from the ledger
    survives the driver restarting, which is the case that matters: a loop
    resumed after a crash must not get a fresh budget.
    """

    max_iterations: int = 10
    #: Tokens, not dollars. Drover prices nothing, so a USD cap could only be
    #: estimated; this one is read off the session's own events. It is a bound
    #: rather than an accounting and errs upward, because a cap that
    #: overcounts stops early and one that undercounts never stops.
    max_tokens: int = 2_000_000


@dataclass
class IterationOutcome:
    ordinal: int
    met: bool
    exit_code: Optional[int]
    session_id: Optional[str]
    stopped_reason: Optional[str] = None
    fatal: bool = False
    #: True when the session never started, or started and was lost. The check
    #: may still pass -- and on the first live run it did, because an orphaned
    #: session from an earlier run was still editing the tree. Verification is
    #: ground truth for *whether* the goal is met; it says nothing about which
    #: iteration met it, and a loop that conflates the two records a pass it
    #: cannot account for.
    unobserved: bool = False


class LoopDriver:
    def __init__(
        self,
        *,
        api: HarnessApi,
        ledger: ScratchLedger,
        goal: Goal,
        host_id: str,
        harness: str,
        caps: Caps = Caps(),
        poll_interval_s: float = 2.0,
        turn_timeout_s: float = 1800.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api = api
        self._ledger = ledger
        self._goal = goal
        self._host_id = host_id
        self._harness = harness
        self._caps = caps
        self._poll = poll_interval_s
        self._turn_timeout = turn_timeout_s
        self._sleep = sleep
        #: The session currently in flight, so a signal handler can reap it.
        #: Without this a stopped driver leaves an agent running against the
        #: same tree the next iteration verifies.
        self._active_session_id: Optional[str] = None

    # -- one turn ---------------------------------------------------------- #

    def _wait_for_turn(self, session_id: str) -> str:
        """Block until the agent stops working. Returns why it stopped.

        Polls the session rather than the event stream: Phase 0 needs to know
        *that* a turn ended, not to reconstruct it, and a poll cannot miss an
        event it was not attached for. The stream is the right answer once the
        loop wants per-event cost attribution.
        """
        deadline = time.monotonic() + self._turn_timeout
        while time.monotonic() < deadline:
            session = self._api.session(session_id)
            status = str(session.get("status") or "").lower()
            awaiting = str(session.get("awaiting") or "").lower()
            if status in _FINISHED_STATUS:
                return f"session {status}"
            if awaiting in _SETTLED_AWAITING:
                return f"awaiting {awaiting}"
            self._sleep(self._poll)
        return "turn timed out"

    def _usage(self, session_id: str) -> Usage:
        """What this session reported spending, as a bound.

        Read from the session's own events rather than from a rollup, because
        the rollup does not exist yet (drover#17) and is blocked on the event
        model (drover#270).
        """
        try:
            return from_events(self._api.messages(session_id), self._harness)
        except HarnessApiError:
            return Usage()

    def _claimed_outcome(self, session_id: str) -> str:
        """The last thing the agent said, kept as a *claim* and nothing more.

        Recorded next to the exit code deliberately. The pair is the evidence
        for the failure this phase exists to detect: an agent confidently
        inventing progress and one genuinely making it produce identical
        ledgers until something checks them.
        """
        try:
            messages = self._api.messages(session_id)
        except HarnessApiError:
            return ""
        for message in reversed(messages):
            if str(message.get("role") or "").lower() in ("assistant", "agent"):
                return str(message.get("text") or message.get("content") or "")[:4000]
        return ""

    def _annotated_history(self) -> list[dict[str, Any]]:
        """History, with git asked whether each claim's artifact is still here.

        Decision D1 keeps claims and artifacts in different places, so they can
        disagree. The brief says when they do; this is where the question gets
        asked, because only the driver knows which tree the goal is about.
        """
        rows = self._ledger.history(self._goal.goal_id)
        for row in rows:
            head = (row.get("diff_ref") or "").strip()
            base = (row.get("base_ref") or "").strip()
            if head and head != base:
                row["artifact_present"] = is_reachable(self._goal.cwd, head)
        return rows

    def run_iteration(self, ordinal: int) -> IterationOutcome:
        history = self._annotated_history()
        brief = render(
            goal_summary=self._goal.summary,
            goal_kind=self._goal.kind,
            check=self._goal.check,
            history=history,
            max_iterations=self._caps.max_iterations,
        )
        started = utcnow()
        # Read before the session runs: what it leaves behind is only
        # identifiable against where it started.
        base_ref = head_commit(self._goal.cwd)
        session_id: Optional[str] = None
        claimed = ""
        stopped = None
        fatal = False
        unobserved = False
        usage = Usage()
        try:
            session = self._api.create_session(
                self._host_id,
                harness=self._harness,
                cwd=str(self._goal.cwd),
                prompt=brief,
            )
            session_id = str(session.get("session_id") or session.get("id") or "")
            self._active_session_id = session_id or None
            stopped = self._wait_for_turn(session_id)
            claimed = self._claimed_outcome(session_id)
            usage = self._usage(session_id)
        except HarnessApiError as exc:
            # A harness that will not start is an iteration that happened and
            # produced nothing, which is worth a row: a run whose sessions all
            # failed to launch must not look like a run that found nothing.
            stopped = f"harness error: {exc}"
            fatal = getattr(exc, "fatal", False)
            unobserved = True
            log.warning("iteration %d could not run: %s", ordinal, exc)
        finally:
            if session_id:
                self._api.terminate(session_id)
            self._active_session_id = None

        verification = self._goal.verify()
        tail = (verification.stdout + verification.stderr).strip()[-4000:]
        self._ledger.append(
            Iteration(
                goal_id=self._goal.goal_id,
                ordinal=ordinal,
                started_at=started,
                ended_at=utcnow(),
                host_id=self._host_id,
                session_id=session_id or None,
                harness=self._harness,
                brief_rendered=brief,
                verification_cmd=" ".join(verification.command),
                verification_exit=verification.exit_code,
                verification_tail=tail,
                claimed_outcome=claimed or None,
                diff_ref=head_commit(self._goal.cwd),
                base_ref=base_ref,
                prompt_tokens=usage.prompt_tokens if usage.observed else None,
                completion_tokens=(usage.completion_tokens if usage.observed else None),
                note=stopped,
            )
        )
        return IterationOutcome(
            ordinal=ordinal,
            met=verification.met,
            exit_code=verification.exit_code,
            session_id=session_id or None,
            stopped_reason=stopped,
            fatal=fatal,
            unobserved=unobserved,
        )

    # -- the loop ---------------------------------------------------------- #

    def run(self) -> list[IterationOutcome]:
        outcomes: list[IterationOutcome] = []
        while True:
            ordinal = self._ledger.next_ordinal(self._goal.goal_id)
            if ordinal > self._caps.max_iterations:
                log.info("iteration cap reached (%d)", self._caps.max_iterations)
                break
            spent = self._ledger.tokens_used(self._goal.goal_id)
            if spent >= self._caps.max_tokens:
                log.info("token cap reached (%d of %d)", spent, self._caps.max_tokens)
                break

            outcome = self.run_iteration(ordinal)
            outcomes.append(outcome)
            log.info(
                "iteration %d: check exit %s (%s)",
                outcome.ordinal,
                outcome.exit_code,
                outcome.stopped_reason,
            )

            if outcome.fatal:
                log.error(
                    "stopping: %s. This will fail the same way next iteration.",
                    outcome.stopped_reason,
                )
                break

            if outcome.unobserved and outcome.met:
                # The check passes and this iteration cannot take credit for
                # it. Stopping here would record a success with no session, no
                # claim and no diff behind it; continuing would keep spending
                # against a goal that is already met. Neither is right, so say
                # so and stop rather than pick one quietly.
                log.error(
                    "iteration %d: check passes but its session never ran. "
                    "Something changed the tree that this loop did not observe.",
                    outcome.ordinal,
                )
                break

            if outcome.met and self._goal.kind == "project":
                # An invariant goal has no done-condition to stop on, so only a
                # project goal may end this way. Modelling one as the other is
                # how a loop either finishes falsely or never finishes at all.
                log.info("goal met at iteration %d", outcome.ordinal)
                break
        return outcomes
