"""Control-owned harness bridge and credential-authenticated work HTTP tools."""

import hmac
import json
import logging
import threading
from urllib.parse import quote

from drover.server.harness.models import TERMINAL_SESSION_STATUSES
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.work_coordinator import (
    FactoryObserverRequestError,
    parse_work_coordinator_continuity,
    sign_work_coordinator_continuity,
    work_coordinator_turn_id,
)
from drover.server.harness.worker_state import worker_brief
from drover.server.mcp.contract import ReadCaps, bound_response
from drover.server.web.auth import bearer_credential

from .github import ProviderUnavailable
from .providers import provider_for_scope
from .service import SYSTEMIC_COMMENT, WorkActor, WorkDenied, WorkService
from .store import WorkStore

log = logging.getLogger(__name__)


class HarnessBridge:
    def __init__(self, collector, config):
        self.collector = collector
        self.config = config
        self.registry = HarnessRegistry(collector.duckdb_path)

    def resolve(self, run_key):
        session = self.registry.session_by_client_id(run_key)
        if session is not None:
            # The hub registry can lag a terminal daemon transition across a
            # relay restart. Confirm a known nonterminal work session with
            # its own live host before letting it suppress a terminal relaunch.
            # An unavailable host remains fail-closed as the registry value.
            host_id = getattr(session, "host_id", None)
            if host_id and session.status not in TERMINAL_SESSION_STATUSES:
                for host in self.registry.list_hosts():
                    if host.host_id != host_id or not host.liveness().usable:
                        continue
                    status, body = self.collector._harness_request(
                        host,
                        "/sessions?client_session_id=" + run_key,
                        method="GET",
                        timeout_s=10,
                    )
                    if status != 200:
                        log.warning(
                            "harness inspection unavailable host_id=%s status=%s",
                            host.host_id,
                            status,
                        )
                        break
                    for row in json.loads(body).get("sessions", []):
                        if (
                            row.get("client_session_id") == run_key
                            and row.get("status") in TERMINAL_SESSION_STATUSES
                        ):
                            payload = self._recovery_payload(run_key, row)
                            self.collector._sync_created_harness_session(
                                host.host_id, payload, json.dumps(row)
                            )
                            return self.registry.session_by_client_id(run_key)
                    break
            self._require_bound_work_continuity(session, run_key)
            return session
        # A response may have been lost before the control registry was synced.
        # Read each reachable daemon, never replay POST to discover the outcome.
        inspection_unavailable = False
        for host in self.registry.list_hosts():
            if not host.liveness().usable:
                continue
            status, body = self.collector._harness_request(
                host,
                "/sessions?client_session_id=" + run_key,
                method="GET",
                timeout_s=10,
            )
            if status != 200:
                # One detached relay must not hide a matching session on a
                # different usable host, but it also means this intent cannot
                # safely be declared orphaned if no other host resolves it.
                log.warning(
                    "harness inspection unavailable host_id=%s status=%s",
                    host.host_id,
                    status,
                )
                inspection_unavailable = True
                continue
            for row in json.loads(body).get("sessions", []):
                if row.get("client_session_id") == run_key:
                    # A recovered daemon row does not include the create
                    # request. Reconstruct the hub-signed continuity binding
                    # from the committed work intent before syncing it, or
                    # refuse to link a coordinator run without its ledger.
                    payload = self._recovery_payload(run_key, row)
                    self.collector._sync_created_harness_session(
                        host.host_id, payload, json.dumps(row)
                    )
                    return self.registry.session_by_client_id(run_key)
        if inspection_unavailable:
            raise WorkDenied("harness inspection unavailable")
        return None

    def existing_session(self, session_id, turn_id, repo):
        """Read one pre-existing settled session without changing its key.

        Existing sessions predate the coordinator and therefore cannot safely
        be made to satisfy ``resolve(run_key)`` by rewriting their client ID.
        The explicit opt-in path reads their persisted repository identity and
        exact successful terminal turn instead.
        """
        if not all(isinstance(value, str) and value for value in (session_id, turn_id)):
            return None
        if not isinstance(repo, str) or repo.count("/") != 1:
            return None
        owner, name = repo.split("/", 1)
        session = self.registry.get_session(session_id)
        if session is None or session.repo_owner != owner or session.repo_name != name:
            return None
        if session.status == "completed":
            return (
                session
                if self.registry.has_successfully_completed_turn(session_id, turn_id)
                else None
            )
        if (
            session.status == "running"
            and session.mode == "structured"
            and self.registry.has_successfully_completed_resumable_turn(
                session_id, turn_id, harness=session.harness
            )
            and self._resumable_session_is_idle(session)
        ):
            return session
        return None

    def _resumable_session_is_idle(self, session):
        """Read the owning daemon's narrow live-turn fact for a parked session.

        A structured session remains ``running`` between turns.  Its durable
        result records what happened, but only the owning daemon can confirm
        that it has released that exact session's turn.  An unavailable or
        malformed response is deliberately not equivalent to idle.
        """
        host = next(
            (
                candidate
                for candidate in self.registry.list_hosts()
                if candidate.host_id == session.host_id and candidate.liveness().usable
            ),
            None,
        )
        if host is None:
            return False
        try:
            status, body = self.collector._harness_request(
                host,
                "/sessions/" + quote(session.session_id, safe=""),
                method="GET",
                timeout_s=10,
            )
            if status != 200:
                return False
            row = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return (
            isinstance(row, dict)
            and row.get("session_id") == session.session_id
            and row.get("harness") == session.harness
            and row.get("status") == "running"
            and row.get("mode") == "structured"
            and row.get("awaiting") == "input"
            and row.get("turn_active") is False
        )

    def _require_bound_work_continuity(self, session, run_key):
        """Reject a coordinator session that lacks its immutable ledger.

        A coordinator create is only usable after the hub has persisted the
        signed continuity run.  A daemon can still be live when its create
        response reached the hub but initialization then failed (for example,
        because a serialized integration reservation was already held).  The
        registry row alone cannot reconstruct a local-candidate authority or
        expected effect, so it must never turn that orphan into a reusable
        launch result.
        """
        if getattr(session, "handoff_mode", None) != "work_coordinator":
            return
        from drover.server.harness.continuity import FactoryObserverContinuity

        try:
            status = FactoryObserverContinuity(self.collector.duckdb_path).status(
                "work-" + run_key
            )
        except Exception as exc:  # unknown ledger is never safe to recover
            raise WorkDenied(
                "bound work continuity is unavailable; inspection required"
            ) from exc
        if status.get("session_id") != session.session_id:
            raise WorkDenied(
                "bound work continuity is unavailable; inspection required"
            )

    def _recovery_payload(self, run_key, row):
        payload = {
            "client_session_id": run_key,
            "harness": row.get("harness"),
        }
        service = getattr(self.collector, "work_control", None)
        store = getattr(service, "store", None)
        if store is None:
            if any(
                getattr(scope, "continuity_owner_id", None)
                for scope in getattr(getattr(self, "config", None), "goals", [])
            ):
                raise WorkDenied("cannot recover work continuity launch intent")
            return payload
        links = store.links(run_key=run_key)
        if len(links) != 1:
            raise WorkDenied("cannot recover unique work launch intent")
        link = links[0]
        stored_binding = link.get("work_coordinator_binding_json")
        if stored_binding is not None:
            from drover.server.harness.work_coordinator import (
                FactoryObserverRequestError,
                parse_work_coordinator_continuity,
            )

            try:
                raw_binding = json.loads(stored_binding)
                if not isinstance(raw_binding, dict):
                    raise ValueError("stored binding is not an object")
                binding, _ = parse_work_coordinator_continuity(
                    {"work_coordinator_continuity": raw_binding},
                    hub_secret=self.collector.work_coordinator_continuity_secret,
                )
                if binding is None or binding.run_id != "work-" + run_key:
                    raise ValueError("stored binding run does not match")
            except (
                FactoryObserverRequestError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise WorkDenied(
                    "stored work continuity binding is invalid; inspection required"
                ) from exc
            payload["work_coordinator_continuity"] = raw_binding
            return payload
        scope = next(
            (
                scope
                for scope in self.config.goals
                if scope.goal_id == link.get("goal_id")
            ),
            None,
        )
        if scope is None:
            raise WorkDenied("cannot recover work continuity goal")
        authority_labels = ()
        expected_effect_artifact = None
        if (
            getattr(scope, "integration_serialization_label", None) is not None
            or getattr(scope, "candidate_source", None) == "local_file"
        ):
            _, _, _, item = service.item(link.get("goal_id"), link.get("item_number"))
            authority_labels = item.get("operator_authority_labels", [])
            expected_effect_artifact = item.get("expected_effect_artifact")
        if getattr(scope, "candidate_source", None) == "local_file":
            # The original local-candidate authority and expected effect are
            # hub-signed at launch.  A missing create response has no durable
            # signed binding to recover, so mutable goal config must never
            # reconstruct either an implementation binding or a downgraded
            # former deployment binding.
            raise WorkDenied(
                "local candidate continuity recovery requires existing signed session"
            )
        binding = self._work_coordinator_binding(
            run_key,
            link.get("run_role"),
            link.get("item_url"),
            link.get("goal_id"),
            authority_labels=authority_labels,
            expected_effect_artifact=expected_effect_artifact,
            local_candidate_number=link.get("item_number"),
        )
        if binding is not None:
            payload["work_coordinator_continuity"] = binding
        return payload

    @staticmethod
    def _local_candidate_authority_scope(scope, local_candidate_number):
        if getattr(scope, "candidate_source", None) != "local_file":
            return "implementation"
        if not isinstance(local_candidate_number, int) or local_candidate_number <= 0:
            raise WorkDenied("local candidate authority binding is invalid")
        return getattr(scope, "local_candidate_authority_scopes", {}).get(
            str(local_candidate_number), "implementation"
        )

    @staticmethod
    def _local_candidate_m2_ack_pending(scope, role, local_candidate_number):
        """Return the reviewed M2 receipt handoff for one work launch only."""
        if (
            getattr(scope, "candidate_source", None) != "local_file"
            or role != "work"
            or not isinstance(local_candidate_number, int)
            or local_candidate_number <= 0
        ):
            return False
        return (
            getattr(scope, "local_candidate_m2_ack_pending", {}).get(
                str(local_candidate_number)
            )
            == "m2"
        )

    @staticmethod
    def _local_candidate_committed_provenance(scope, role, local_candidate_number):
        """Return the reviewed M3 commit exception for one work launch only."""
        if (
            getattr(scope, "candidate_source", None) != "local_file"
            or role != "work"
            or not isinstance(local_candidate_number, int)
            or local_candidate_number <= 0
        ):
            return False
        return (
            getattr(scope, "local_candidate_committed_provenance", {}).get(
                str(local_candidate_number)
            )
            == "m3"
        )

    @staticmethod
    def _github_event_delivery_revision(scope, role, number):
        """Return the reviewed revision for one signed GitHub continuation."""
        if (
            getattr(scope, "candidate_source", None) != "github"
            or role not in {"work", "verify"}
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
        ):
            return None
        revision = getattr(scope, "github_event_delivery_profiles", {}).get(str(number))
        profile = getattr(scope, "github_acceptance_profiles", {}).get(str(number))
        return (
            revision if profile is not None and revision == profile.revision else None
        )

    def _work_coordinator_binding(
        self,
        run_key,
        role,
        item_url,
        goal_id,
        *,
        authority_labels=(),
        expected_effect_artifact=None,
        local_candidate_number=None,
    ):
        scope = next(
            (scope for scope in self.config.goals if scope.goal_id == goal_id), None
        )
        if scope is None or not getattr(scope, "continuity_owner_id", None):
            return None
        if (
            role not in {"work", "verify"}
            or not isinstance(item_url, str)
            or not item_url
        ):
            raise WorkDenied("cannot reconstruct work continuity binding")
        authority_scope = self._local_candidate_authority_scope(
            scope, local_candidate_number
        )
        binding = {
            "run_id": "work-" + run_key,
            "owner_id": scope.continuity_owner_id,
            "authority_scope": authority_scope,
            "objective": f"Work coordinator {role} for {item_url}",
        }
        committed_provenance = self._local_candidate_committed_provenance(
            scope, role, local_candidate_number
        )
        m2_ack_pending = self._local_candidate_m2_ack_pending(
            scope, role, local_candidate_number
        )
        github_event_delivery_revision = self._github_event_delivery_revision(
            scope, role, local_candidate_number
        )
        if (
            committed_provenance or m2_ack_pending
        ) and authority_scope != "implementation":
            raise WorkDenied(
                "candidate provenance or M2 handoff cannot select non-implementation authority"
            )
        serialization_key = getattr(scope, "integration_serialization_key", None)
        serialization_label = getattr(scope, "integration_serialization_label", None)
        if (
            (committed_provenance or m2_ack_pending)
            and serialization_label is not None
            and serialization_label in set(authority_labels or ())
        ):
            raise WorkDenied(
                "candidate provenance or M2 handoff cannot select integration serialization"
            )
        if (
            isinstance(goal_id, str)
            and goal_id
            and not isinstance(local_candidate_number, bool)
            and isinstance(local_candidate_number, int)
            and local_candidate_number > 0
        ):
            binding.update(goal_id=goal_id, item_number=local_candidate_number)
        if expected_effect_artifact is not None:
            if (
                not isinstance(expected_effect_artifact, str)
                or not expected_effect_artifact.strip()
                or len(expected_effect_artifact) > 2000
            ):
                raise WorkDenied("local candidate expected effect is invalid")
            binding["expected_effect_artifact"] = expected_effect_artifact
        if committed_provenance:
            if expected_effect_artifact is None:
                raise WorkDenied("committed provenance requires expected effect")
            binding["committed_provenance"] = True
        if github_event_delivery_revision is not None:
            binding["github_event_delivery_profile_revision"] = (
                github_event_delivery_revision
            )
        if m2_ack_pending:
            if expected_effect_artifact is None:
                raise WorkDenied("M2 handoff requires expected effect")
            binding["m2_ack_pending"] = True
        if (
            authority_scope != "implementation"
            and serialization_label is not None
            and serialization_label in set(authority_labels or ())
        ):
            raise WorkDenied(
                "deployment candidate cannot select integration serialization"
            )
        if (
            role == "work"
            and serialization_key is not None
            and (
                serialization_label is None
                or serialization_label in set(authority_labels or ())
            )
        ):
            binding.update(
                delivery_role="integration_serialized",
                repository=scope.repo,
                serialization_key=serialization_key,
            )
        binding["binding_signature"] = sign_work_coordinator_continuity(
            binding, self.collector.work_coordinator_continuity_secret
        )
        return binding

    def active(self, links, *, release_finalised=True):
        """Links that still hold a session.

        With ``release_finalised`` (capacity), a worker that declared done or was
        reported by the idle sweep is dropped: it is idle at awaiting:input with
        nothing left to run. Pass False for duplicate-execution checks, where the
        resumable session still counts. Fails closed: if the finalised lookup is
        unavailable every live worker keeps its slot.
        """
        active = []
        for link in links:
            # An intent is not a worker. It remains visible to the duplicate
            # guard (release_finalised=False), but never consumes launch capacity.
            if release_finalised and link["session_id"] is None:
                continue
            session = (
                self.registry.get_session(link["session_id"])
                if link["session_id"]
                else None
            )
            if session is None or session.status not in TERMINAL_SESSION_STATUSES:
                active.append(link)
        live = [l["session_id"] for l in active if l["session_id"]]
        if release_finalised and live:
            from drover.server.harness.continuity import finalised_session_ids

            try:
                released = finalised_session_ids(
                    self.collector.duckdb_path,
                    live,
                    idle_awaiting_input_seconds=(
                        self.config.execution.idle_awaiting_input_minutes * 60
                    ),
                )
            except Exception:  # noqa: BLE001 - never free a slot on a failed read
                log.exception("finalised-worker lookup failed; holding all slots")
                released = set()
            active = [l for l in active if l["session_id"] not in released]
        return active

    def eligible(self, goal_id, parent_session_id=None):
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        parent = (
            self.registry.get_session(parent_session_id) if parent_session_id else None
        )
        hosts = {parent.host_id} if parent and parent.cwd else set(scope.execution_cwds)
        # Native-owner goals bind their durable lifecycle to one configured
        # harness. Their work turn must use that same worktree-capable harness:
        # never let quota preference select a shared-CWD adapter.
        native_owner = isinstance(scope.continuity_owner_id, str) and (
            scope.continuity_owner_id.startswith("drover:owner/")
        )
        required_harness = scope.continuity_owner_harness if native_owner else None
        if native_owner and not isinstance(required_harness, str):
            return set()
        if native_owner and not required_harness.strip():
            return set()

        def eligible_row(row):
            if not isinstance(row, dict) or not row.get("enabled"):
                return False
            capabilities = row.get("capabilities")
            if not isinstance(capabilities, dict):
                return False
            if "structured" not in capabilities.get("launch_modes", []):
                return False
            if required_harness is None:
                return True
            return (
                row.get("name") == required_harness
                and capabilities.get("worktree") is True
            )

        return {
            (host.host_id, row["name"])
            for host in self.registry.list_hosts()
            if host.liveness().usable and host.host_id in hosts
            for row in host.capabilities.get("harnesses", [])
            if eligible_row(row)
        }

    def quota(self):
        from drover.server.providers.quota_read import read_provider_quota

        # Queue admission is consequential, so it must not route from a stale
        # lake projection.  The shared provider reader refreshes through the hub's
        # relay-aware collector transport; use that same path here rather than
        # its unconfigured direct-fetch fallback.
        return read_provider_quota(
            duckdb_path=self.collector.duckdb_path,
            fresh=True,
            fetch=self.collector.fetch_harness_provider_usage,
        )["accounts"]

    def coordinator_failure_category(self, run_key):
        """Read one terminal coordinator failure without interpreting prose."""
        session = self.resolve(run_key)
        if session is None or session.status not in TERMINAL_SESSION_STATUSES:
            return None
        if session.handoff_mode != "work_coordinator":
            return None
        return self.registry.coordinator_turn_failure_category(session.session_id)

    def terminal_work_done(self, run_key):
        """Return true only after the bound worker's exact turn is safely idle.

        Codex is a reusable structured session: a successful turn emits an
        ``exited`` status with its turn id while the session remains running
        and awaits its next input. Requiring the whole session to terminate
        stranded a deferred verification intent forever. A live session is
        therefore acceptable only when its most recent turn is durably
        finished and it is awaiting input; active, failed, and unknown turns
        remain ineligible.
        """
        session = self.resolve(run_key)
        if session is None or session.handoff_mode != "work_coordinator":
            return False
        expected_turn_id = work_coordinator_turn_id("work-" + run_key)
        if session.status == "completed":
            turn_finished = self.registry.has_successfully_completed_turn(
                session.session_id, expected_turn_id
            )
        elif session.status == "running" and session.effective_awaiting == "input":
            turn_finished = self.registry.has_successfully_completed_turn(
                session.session_id, expected_turn_id
            )
        else:
            turn_finished = False
        if not turn_finished:
            return False
        from drover.server.harness.continuity import FactoryObserverContinuity

        try:
            status = FactoryObserverContinuity(self.collector.duckdb_path).status(
                "work-" + run_key
            )
        except Exception:  # unknown terminal projection must not schedule a verifier
            return False
        return (
            status.get("session_id") == session.session_id
            and status.get("worker_state") == "done"
        )

    def work_coordinator_committed_provenance(self, run_key):
        """Return the immutable provenance mode from the bound hub run."""
        session = self.resolve(run_key)
        if session is None or session.handoff_mode != "work_coordinator":
            return None
        return self.registry.work_coordinator_committed_provenance(
            session.session_id, "work-" + run_key
        )

    def github_event_delivery(self, run_key, profile_revision):
        """Read one signed, exact-profile continuation binding from its link."""
        session = self.resolve(run_key)
        service = getattr(self.collector, "work_control", None)
        if (
            session is None
            or session.handoff_mode != "work_coordinator"
            or service is None
            or not isinstance(profile_revision, str)
        ):
            return False
        rows = service.store.links(run_key=run_key)
        if len(rows) != 1:
            return False
        raw = rows[0].get("work_coordinator_binding_json")
        if not isinstance(raw, str):
            return False
        try:
            binding = json.loads(raw)
            parsed, _ = parse_work_coordinator_continuity(
                {"work_coordinator_continuity": binding},
                hub_secret=self.collector.work_coordinator_continuity_secret,
            )
        except (
            FactoryObserverRequestError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False
        return bool(
            parsed is not None
            and parsed.run_id == "work-" + run_key
            and parsed.github_event_delivery_profile_revision == profile_revision
        )

    def github_native_owner_wake(
        self, run_key, *, source_event_id, subject, summary
    ) -> str | None:
        """Durably hand one exact GitHub verdict to its signed native owner.

        The caller has already checked the installed profile and provider
        verdict.  This boundary only proves that the retained work session owns
        the matching Factory observer run before it appends the idempotent wake.
        It neither reads issue text nor treats the coordinator outcome as an
        acknowledgement.
        """
        session = self.resolve(run_key)
        if session is None or session.handoff_mode != "work_coordinator":
            return None
        from drover.server.harness.continuity import FactoryObserverContinuity

        try:
            continuity = FactoryObserverContinuity(self.collector.duckdb_path)
            status = continuity.status("work-" + run_key)
            if status.get("session_id") != session.session_id:
                return None
            wake_id = continuity.report(
                "work-" + run_key,
                source="github-provider-verdict",
                source_event_id=source_event_id,
                subject=subject,
                sequence=0,
                kind="github_acceptance_verified",
                summary=summary,
            )
        except Exception:  # unavailable ledger must retain the coordinator retry
            return None
        runner = getattr(self.collector, "native_owner_runner", None)
        if runner is not None:
            # This is an event kick, not another scheduler.  The runner's
            # lease/claim fences make an idle sweep racing this call harmless.
            runner.process_once()
        return wake_id

    def expected_effect_materialized(self, run_key, artifact):
        """Ask the bound daemon whether its exact candidate effect exists.

        ``None`` means the readback was unavailable or malformed and is
        intentionally not recoverable.  The owner uses the same endpoint
        before dispatching a candidate action.
        """
        session = self.resolve(run_key)
        if session is None or session.handoff_mode != "work_coordinator":
            return None
        # The provider manifest can change after launch. Recovery must compare
        # its current value with the original hub-verified binding rather than
        # reconstructing authority from that mutable provider data.
        bound_artifact = self.registry.work_coordinator_expected_effect_artifact(
            session.session_id, "work-" + run_key
        )
        if bound_artifact != artifact:
            return None
        status, body = self.collector.proxy_harness_session_action(
            session.session_id, "expected-effect", {"artifact": artifact}
        )
        if not 200 <= status < 300:
            return None
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        verification = payload.get("verification")
        materialized = payload.get("materialized")
        if verification == "materialized" and materialized is True:
            return True
        if verification == "absent" and materialized is False:
            return False
        return None

    def prepare_work_coordinator_binding(
        self, run_key, role, ref, goal_id, *, authority=None
    ):
        """Build the one signed envelope persisted before daemon create."""
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        if not scope.continuity_owner_id:
            return None
        authority = authority or {}
        return self._work_coordinator_binding(
            run_key,
            role,
            ref.url,
            goal_id,
            authority_labels=authority.get("labels", []),
            expected_effect_artifact=authority.get("expected_effect_artifact"),
            local_candidate_number=authority.get("local_candidate_number"),
        )

    def reserve_work_coordinator_launch(self, binding):
        """Reserve a signed integration binding before the daemon POST.

        Standard work has no serialization reservation.  The signed envelope
        is parsed again here so a persisted launch intent cannot use mutable
        provider/configuration state to reserve authority on a later retry.
        """
        try:
            parsed, _ = parse_work_coordinator_continuity(
                {"work_coordinator_continuity": binding},
                hub_secret=self.collector.work_coordinator_continuity_secret,
            )
        except (FactoryObserverRequestError, TypeError, ValueError) as exc:
            raise WorkDenied(
                "stored work continuity binding is invalid; inspection required"
            ) from exc
        if parsed is None or parsed.delivery_role != "integration_serialized":
            return False
        from drover.server.harness.continuity import (
            FactoryObserverContinuity,
            IntegrationReservationHeld,
        )

        try:
            return FactoryObserverContinuity(
                self.collector.duckdb_path
            ).reserve_integration_launch(
                run_id=parsed.run_id,
                owner_id=parsed.owner_id,
                authority_scope=parsed.authority_scope,
                delivery_role=parsed.delivery_role,
                repository=parsed.repository,
                serialization_key=parsed.serialization_key,
            )
        except IntegrationReservationHeld as exc:
            raise WorkDenied("integration reservation is held") from exc
        except Exception as exc:
            raise WorkDenied(
                "integration reservation is unavailable; inspection required"
            ) from exc

    def launch(
        self,
        host,
        harness,
        run_key,
        ref,
        role,
        parent,
        goal_id,
        *,
        authority=None,
        continuity_binding=None,
    ):
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        parent_session = self.registry.get_session(parent) if parent else None
        cwd = (
            parent_session.cwd
            if parent_session and parent_session.host_id == host
            else scope.execution_cwds.get(host)
        )
        authority = authority or {}
        local_number = authority.get("local_candidate_number")
        local_cwd = authority.get("local_candidate_cwd")
        if local_cwd is not None:
            if (
                scope.candidate_source != "local_file"
                or not isinstance(local_number, int)
                or scope.local_candidate_worktrees.get(str(local_number)) != local_cwd
            ):
                raise WorkDenied("local candidate worktree binding is invalid")
            cwd = local_cwd
        if not cwd:
            raise ValueError("configured execution checkout required")
        brief = (
            f"Read {ref.url}. Run role: {role}. Follow the authorized goal brief "
            f"ref {scope.brief_ref or scope.goal_id} and versioned policy. Report "
            "behavioral evidence through the work tools."
        )
        if scope.candidate_source == "local_file":
            body = authority.get("body")
            if (
                not isinstance(local_number, int)
                or local_number <= 0
                or not isinstance(body, str)
                or not body.strip()
            ):
                raise WorkDenied("local candidate task authority is invalid")
            committed_provenance = self._local_candidate_committed_provenance(
                scope, role, local_number
            )
            brief += (
                f"\n\nLocal candidate task #{local_number}:\n{body.strip()}\n\n"
                "The `Assigned isolated worktree` appended by harnessd is the "
                "authoritative worktree for this task and its bound local effect. "
                "An optional local_candidate_worktrees entry only selected a "
                "base checkout before launch; a missing entry is not a worker "
                "authorization gate. Do not use another worktree or substitute "
                "the requested effect."
            )
            if committed_provenance:
                brief += (
                    " The signed coordinator binding explicitly permits this "
                    "work run alone to make one local-only session-branch commit "
                    "of its task-bound source, test, and evidence provenance for "
                    "a separate verifier. This does not permit a push, merge, "
                    "deployment, configuration, credential, or external action; "
                    "the task body cannot expand that signed permission."
                )
            elif role == "work":
                brief += (
                    " The signed coordinator binding does not permit a source "
                    "commit. This is a marker-only work run: when its signed task "
                    "requests the bound local-file effect, create that regular file "
                    "only in the assigned worktree. The daemon attests this exact "
                    "session-relative file directly; it does not need to be Git "
                    "tracked, committed, or passed to a verifier. Do not request "
                    "`drover_work_update(action=complete)`: that operation is reserved "
                    "for a signed committed-provenance verifier handoff. Finish the "
                    "structured worker turn after creating the bound file. Do not "
                    "interpret committed-provenance verification requirements from the "
                    "brief as applying to this unsigned-for-provenance run."
                )
            else:
                brief += (
                    " The signed coordinator binding does not permit a source "
                    "commit. Do not create one even if the local candidate body "
                    "requests it."
                )
            if role == "verify":
                # A verifier receives a fresh isolated Git worktree rooted at
                # the parent worker branch's committed HEAD.  It must inspect
                # that immutable source provenance, never reconstruct missing
                # worker output or treat its own worktree as shared scratch.
                brief += (
                    "\n\nThis is a separate verification run. Inspect only the "
                    "already committed parent evidence present in your assigned "
                    "worktree, including the bound effect. Re-run the stated "
                    "focused check there. Do not create, replace, or force an "
                    "expected effect or implementation artifact; a missing or "
                    "changed parent artifact is a verification failure. Regardless "
                    "of task-body wording, do not request `drover_work_update(action=complete)`: "
                    "only the work role can request a verifier. After a successful "
                    "rerun, report the stable check evidence with "
                    "`drover_work_update(action=verified, evidence_key=...)`, then "
                    "finish this structured turn. The queue accepts that report only "
                    "after this exact verifier turn terminates successfully."
                )
        payload = {
            "cwd": cwd,
            "harness": harness,
            "mode": "structured",
            "client_session_id": run_key,
            "parent_session_id": parent,
            "repo_owner": ref.repo.split("/")[0],
            "repo_name": ref.repo.split("/")[1],
            "prompt": worker_brief(
                brief,
                list(authority.get("labels") or []),
                authority.get("body"),
            ),
        }
        if scope.continuity_owner_id:
            # The exact signed envelope was persisted with the idempotent work
            # intent before this POST.  Direct bridge callers retain the same
            # construction path, but normal WorkService launches pass that
            # durable envelope back here unchanged.
            payload["work_coordinator_continuity"] = (
                continuity_binding
                if continuity_binding is not None
                else self.prepare_work_coordinator_binding(
                    run_key, role, ref, goal_id, authority=authority
                )
            )
        status, body = self.collector.proxy_create_harness_session(host, payload)
        if not 200 <= status < 300:
            raise WorkDenied("harness launch uncertain; inspect persisted intent")
        return json.loads(body)["session_id"]


def start_work_service(collector, config):
    if not config.enabled:
        return None
    from drover.server.control_store import is_postgres_control_store

    if not is_postgres_control_store(collector.duckdb_path):
        if config.shadow_mode:
            return None
        raise ValueError("live work control requires Postgres")
    providers = {
        g.goal_id: provider_for_scope(
            config.workspace_id,
            g,
            public_text_validator=lambda text: text == SYSTEMIC_COMMENT,
        )
        for g in config.goals
    }
    service = WorkService(
        config,
        WorkStore(collector.duckdb_path, config.workspace_id),
        providers,
        HarnessBridge(collector, config),
    )
    collector.work_control = service
    return service


def start_work_item_event_worker(collector, config, shutdown_event):
    """Start the notifier only after its Postgres-backed service exists."""
    service = getattr(collector, "work_control", None)
    if service is None:
        return None
    from drover.server.work_queue.work_item_events import WorkItemEventWorker

    worker = WorkItemEventWorker(service.store, config.work_item_events)
    worker.start(shutdown_event=shutdown_event)
    collector.work_item_event_worker = worker
    return worker


def start_github_webhook_worker(collector, config, shutdown_event):
    """Start post-ack GitHub processing only after a work service is available."""
    service = getattr(collector, "work_control", None)
    if service is None or not config.github_webhook.secret_env:
        return None
    from drover.server.work_queue.github_events import GitHubWebhookWorker

    worker = GitHubWebhookWorker(
        service.store,
        config.github_webhook,
        service.github_webhooks,
        service.providers,
        service=service,
    )
    worker.start(shutdown_event=shutdown_event)
    collector.github_webhook_worker = worker
    return worker


def start_work_item_rule_worker(collector, config, shutdown_event):
    service = getattr(collector, "work_control", None)
    if service is None or not config.rules.enabled:
        return None
    from drover.server.work_queue.rules import WorkItemRuleWorker

    worker = WorkItemRuleWorker(
        service.store,
        {scope.repo: service.providers[scope.goal_id] for scope in config.goals},
        config.rules,
    )
    worker.start(shutdown_event)
    collector.work_item_rule_worker = worker
    return worker


def start_queue_execution_worker(collector, config, shutdown_event):
    service = getattr(collector, "work_control", None)
    if (
        service is None
        or config.shadow_mode
        or not any(g.execution_enabled for g in config.goals)
    ):
        return None
    from drover.server.work_queue.reconciler import QueueExecutionWorker

    worker = QueueExecutionWorker(service)
    worker.start(shutdown_event)
    collector.work_queue_execution_worker = worker
    # The device-authenticated queue report is also the execution status model.
    collector.work_queue = worker
    return worker


class IdleUndeclaredWorker:
    """Polls for workers that went idle without a declared state (#785)."""

    def __init__(
        self, path, deadline_seconds, *, interval=30.0, clock=None, owner_runner=None
    ):
        from drover.server.harness.continuity import FactoryObserverContinuity

        self.store = FactoryObserverContinuity(path, clock=clock)
        self.deadline_seconds = deadline_seconds
        self.interval = interval
        self.owner_runner = owner_runner
        self._stop = threading.Event()

    def process_once(self) -> int:
        swept = len(self.store.sweep_idle_undeclared(self.deadline_seconds))
        if self.owner_runner is not None:
            self.owner_runner.process_once()
        return swept

    def start(self, shutdown_event):
        def run():
            while not self._stop.is_set() and not shutdown_event.is_set():
                try:
                    self.process_once()
                except Exception:  # noqa: BLE001 - the next pass retries
                    log.exception("idle-undeclared continuity sweep failed")
                self._stop.wait(self.interval)

        threading.Thread(target=run, name="idle-undeclared", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def start_idle_undeclared_worker(
    collector, config, shutdown_event, *, credential_store=None
):
    """Only goals that opted into an owner get idle-worker wakes."""
    if getattr(collector, "work_control", None) is None or not any(
        g.continuity_owner_id for g in config.goals
    ):
        return None
    native_scopes = [
        scope
        for scope in config.goals
        if scope.continuity_owner_id == "drover:owner/" + scope.goal_id
    ]
    owner_runner = None
    if native_scopes:
        from drover.server.work_queue.native_owner import NativeOwnerRunner

        if credential_store is None:
            raise RuntimeError("native continuity owners require a credential store")
        owner_runner = NativeOwnerRunner(
            collector, config, credential_store=credential_store
        )
        # GitHub's signed CI continuation can wake this existing runner after
        # a provider-verified outcome.  The periodic idle sweep remains only
        # bounded recovery when that immediate kick is unavailable.
        collector.native_owner_runner = owner_runner
    worker = IdleUndeclaredWorker(
        collector.duckdb_path,
        config.idle_undeclared_seconds,
        owner_runner=owner_runner,
    )
    worker.start(shutdown_event)
    collector.idle_undeclared_worker = worker
    return worker


def authenticated_actor(auth, headers, config):
    if not auth.enabled:
        raise PermissionError("work tools require credentials")
    credential = bearer_credential(auth, headers)
    if credential is not None:
        if credential.scope not in {"device", "host"}:
            raise PermissionError("work credential scope required")
        principal = "credential:" + credential.id
        return WorkActor(
            principal,
            config.principal_roles.get(principal, "worker"),
            credential.scope == "device",
            credential.host_id,
            credential.scope == "device",
        )
    authorization = headers.get("Authorization", "") or ""
    if (
        auth.legacy_token_enabled
        and auth.api_token
        and hmac.compare_digest(authorization, "Bearer " + auth.api_token)
    ):
        principal = "control:operator"
        return WorkActor(
            principal, config.principal_roles.get(principal, "operator"), True
        )
    raise PermissionError("authenticated bearer required")


def dispatch(service, auth, headers, operation, args):
    if service is None:
        return 503, {"error": "work control unavailable"}
    try:
        actor = authenticated_actor(auth, headers, service.config)
        result = json.loads(
            json.dumps(service.call(operation, args, actor), default=str)
        )
        if operation in {"list", "get"}:
            result = bound_response(result, ReadCaps())
        return 200, result
    except PermissionError as exc:
        log.warning("work %s forbidden %s: %s", operation, _target(args), exc)
        return 403, {"error": "work operation forbidden", "reason": _reason(exc)}
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("work %s invalid: %s: %s", operation, type(exc).__name__, exc)
        return 400, {"error": "invalid work request"}
    except ProviderUnavailable as exc:
        log.warning(
            "work %s provider unavailable %s: %s", operation, _target(args), exc
        )
        return 409, {
            "error": "work operation denied",
            "reason": "item provider unavailable",
        }
    except WorkDenied as exc:
        log.warning("work %s denied %s: %s", operation, _target(args), exc)
        return 409, {"error": "work operation denied", "reason": _reason(exc)}
    except Exception as exc:
        # Only the exception class is reflected; provider/session text may carry
        # secrets, so the detail stays in the server log.
        log.exception("work %s failed %s", operation, _target(args))
        return 409, {
            "error": "work operation paused; inspect provider or execution intent",
            "reason": "unexpected " + type(exc).__name__,
        }


def _reason(exc):
    # PermissionError/WorkDenied messages are fixed strings written in this package.
    return str(exc)[:200]


def _target(args):
    if not isinstance(args, dict):
        return ""
    return "goal=%s item=%s" % (
        str(args.get("goal_id"))[:64],
        str(args.get("number"))[:12],
    )


def dispatch_hub_items(service, auth, headers, operation, args):
    """Credential boundary for the hub-owned continuity projection."""
    if service is None:
        return 503, {"error": "work control unavailable"}
    try:
        actor = authenticated_actor(auth, headers, service.config)
        if not actor.human:
            raise PermissionError("device credential required")
        credential = bearer_credential(auth, headers)
        if credential is None:
            raise PermissionError("credential required")
        if operation == "list":
            if set(args) - {"state"}:
                raise ValueError("invalid work item query")
            result = {"items": service.store.hub_items(state=args.get("state"))}
        elif operation == "get":
            if set(args) != {"id"}:
                raise ValueError("invalid work item query")
            result = service.store.hub_item(args["id"])
            if result is None:
                return 404, {"error": "work item not found"}
        elif operation == "patch":
            allowed = {
                "id",
                "stage",
                "next",
                "pr",
                "acceptance",
                "auto_merge",
                "automation_gate",
                "publish",
                "depends_on",
            }
            if set(args) - allowed:
                raise ValueError("invalid work item fields")
            result = service.store.patch_hub_item(
                args.pop("id"),
                stage=args.get("stage"),
                next_action=args.get("next"),
                pr=args.get("pr"),
                acceptance=args.get("acceptance"),
                auto_merge=args.get("auto_merge"),
                automation_gate=args.get("automation_gate"),
                publish=args.get("publish"),
                depends_on=args.get("depends_on"),
            )
        elif operation == "export":
            if args:
                raise ValueError("invalid work item query")
            result = service.store.hub_export()
        elif operation == "subscriptions_list":
            if args:
                raise ValueError("invalid subscription query")
            result = {
                "subscriptions": service.store.work_item_subscriptions(credential.id)
            }
        elif operation == "subscriptions_create":
            if set(args) - {"url", "event_kinds", "repo"} or "url" not in args:
                raise ValueError("invalid subscription fields")
            from drover.server.work_queue.work_item_events import (
                validate_subscription_url,
            )

            url = validate_subscription_url(
                args["url"], service.config.work_item_events.webhook_allow_hosts
            )
            result = service.store.create_work_item_subscription(
                credential.id,
                url=url,
                event_kinds=args.get("event_kinds", []),
                repo=args.get("repo"),
            )
        elif operation == "subscriptions_delete":
            if set(args) != {"id"}:
                raise ValueError("invalid subscription query")
            if not service.store.delete_work_item_subscription(
                credential.id, args["id"]
            ):
                return 404, {"error": "subscription not found"}
            result = {"deleted": True}
        elif operation == "events":
            if set(args) - {"since", "limit"}:
                raise ValueError("invalid event query")
            since = int(args.get("since", 0))
            limit = int(args.get("limit", 100))
            if since < 0 or not 1 <= limit <= 100:
                raise ValueError("invalid event cursor")
            result = service.store.work_item_events(since=since, limit=limit)
        else:
            raise ValueError("unknown work item operation")
        return 200, json.loads(json.dumps(result, default=str))
    except PermissionError:
        return 403, {"error": "work item operation forbidden"}
    except KeyError:
        return 404, {"error": "work item not found"}
    except (ValueError, TypeError):
        return 400, {"error": "invalid work item request"}
