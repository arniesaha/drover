"""Control-owned harness bridge and credential-authenticated work HTTP tools."""

import hmac
import json

from drover.server.harness.models import TERMINAL_SESSION_STATUSES
from drover.server.harness.registry import HarnessRegistry
from drover.server.mcp.contract import ReadCaps, bound_response
from drover.server.web.auth import bearer_credential

from .github import GitHubProvider
from .service import SYSTEMIC_COMMENT, WorkActor, WorkService
from .store import WorkStore


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
                raise RuntimeError("harness inspection unavailable")
            for row in json.loads(body).get("sessions", []):
                if row.get("client_session_id") == run_key:
                    self.collector._sync_created_harness_session(
                        host.host_id, row, json.dumps(row)
                    )
                    return self.registry.session_by_client_id(run_key)
        return None

    def active(self, links):
        active = []
        for link in links:
            session = (
                self.registry.get_session(link["session_id"])
                if link["session_id"]
                else None
            )
            if session is None or session.status not in TERMINAL_SESSION_STATUSES:
                active.append(link)
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

        return drover_provider_quota(duckdb_path=self.collector.duckdb_path)["accounts"]

    def launch(self, host, harness, run_key, ref, role, parent, goal_id):
        scope = next(g for g in self.config.goals if g.goal_id == goal_id)
        parent_session = self.registry.get_session(parent) if parent else None
        cwd = (
            parent_session.cwd
            if parent_session and parent_session.host_id == host
            else scope.execution_cwds.get(host)
        )
        if not cwd:
            raise ValueError("configured execution checkout required")
        payload = {
            "cwd": cwd,
            "harness": harness,
            "mode": "structured",
            "client_session_id": run_key,
            "parent_session_id": parent,
            "repo_owner": ref.repo.split("/")[0],
            "repo_name": ref.repo.split("/")[1],
            "prompt": f"Read {ref.url}. Run role: {role}. Follow the authorized goal brief ref {scope.brief_ref or scope.goal_id} and versioned policy. Report behavioral evidence through the work tools.",
        }
        status, body = self.collector.proxy_create_harness_session(host, payload)
        if not 200 <= status < 300:
            raise RuntimeError("harness launch uncertain; inspect persisted intent")
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
    if service is None or config.shadow_mode or not any(g.execution_enabled for g in config.goals):
        return None
    from drover.server.work_queue.reconciler import QueueExecutionWorker

    worker = QueueExecutionWorker(service)
    worker.start(shutdown_event)
    collector.work_queue_execution_worker = worker
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
    except PermissionError:
        return 403, {"error": "work operation forbidden"}
    except (ValueError, TypeError, KeyError):
        return 400, {"error": "invalid work request"}
    except Exception:
        # No exception/provider/session text is reflected to public callers.
        return 409, {
            "error": "work operation paused; inspect provider or execution intent"
        }


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
            allowed = {"id", "stage", "next", "pr", "acceptance", "auto_merge", "automation_gate", "publish", "depends_on"}
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
