"""Control-owned harness bridge and credential-authenticated work HTTP tools."""

import hmac
import json
import logging
import threading

from drover.server.harness.models import TERMINAL_SESSION_STATUSES
from drover.server.harness.registry import HarnessRegistry
from drover.server.harness.work_coordinator import sign_work_coordinator_continuity
from drover.server.harness.worker_state import worker_brief
from drover.server.mcp.contract import ReadCaps, bound_response
from drover.server.web.auth import bearer_credential

from .github import GitHubProvider, ProviderUnavailable
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
            return session
        # A response may have been lost before the control registry was synced.
        # Read each reachable daemon, never replay POST to discover the outcome.
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
                raise WorkDenied("harness inspection unavailable")
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
        return None

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
        binding = self._work_coordinator_binding(
            run_key,
            link.get("run_role"),
            link.get("item_url"),
            link.get("goal_id"),
        )
        if binding is not None:
            payload["work_coordinator_continuity"] = binding
        return payload

    def _work_coordinator_binding(self, run_key, role, item_url, goal_id):
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
        binding = {
            "run_id": "work-" + run_key,
            "owner_id": scope.continuity_owner_id,
            "authority_scope": "implementation",
            "objective": f"Work coordinator {role} for {item_url}",
        }
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
                released = finalised_session_ids(self.collector.duckdb_path, live)
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
        return {
            (host.host_id, row["name"])
            for host in self.registry.list_hosts()
            if host.liveness().usable and host.host_id in hosts
            for row in host.capabilities.get("harnesses", [])
            if isinstance(row, dict)
            and row.get("enabled")
            and "structured" in row.get("capabilities", {}).get("launch_modes", [])
        }

    def quota(self):
        from drover.server.mcp.tools import drover_provider_quota

        # Queue admission is consequential, so it must not route from a stale
        # lake projection.  The public quota tool can refresh through the hub's
        # relay-aware collector transport; use that same path here rather than
        # its unconfigured direct-fetch fallback.
        return drover_provider_quota(
            duckdb_path=self.collector.duckdb_path,
            fresh=True,
            fetch=self.collector.fetch_harness_provider_usage,
        )["accounts"]

    def launch(
        self, host, harness, run_key, ref, role, parent, goal_id, *, authority=None
    ):
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        parent_session = self.registry.get_session(parent) if parent else None
        cwd = (
            parent_session.cwd
            if parent_session and parent_session.host_id == host
            else scope.execution_cwds.get(host)
        )
        if not cwd:
            raise ValueError("configured execution checkout required")
        brief = (
            f"Read {ref.url}. Run role: {role}. Follow the authorized goal brief "
            f"ref {scope.brief_ref or scope.goal_id} and versioned policy. Report "
            "behavioral evidence through the work tools."
        )
        authority = authority or {}
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
            # The committed run key is the producer identity and binds the
            # exact launched session to the coordinator work item. The hub
            # verifies and initializes the durable ledger before reporting
            # launch success to the work coordinator.
            # The daemon accepts coordinator mode only when this exact binding
            # was signed by the hub's continuity secret. The host receives the
            # binding but never the HMAC secret or the hub bearer token.
            payload["work_coordinator_continuity"] = self._work_coordinator_binding(
                run_key, role, ref.url, goal_id
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
        g.goal_id: GitHubProvider(
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
        service.store, config.github_webhook, service.github_webhooks, service.providers
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


def start_idle_undeclared_worker(collector, config, shutdown_event):
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

        owner_runner = NativeOwnerRunner(collector, config)
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
