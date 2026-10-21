"""Event-driven continuity actions are conservative and idempotent."""

from types import SimpleNamespace

from test_hub_work_items import _register
from test_work_queue_execution import service


class GitHub:
    def __init__(self, head="a" * 40):
        self.head, self.opened, self.merged, self.updated = head, [], [], []

    def branch_head(self, branch):
        return self.head

    def open_draft_pr(self, branch, title):
        self.opened.append((branch, title))
        return "https://github.com/example/project/pull/7"

    def pull_is_current(self, pr):
        return True

    def mark_pr_ready(self, pr):
        return None

    def squash_merge(self, pr, head_sha):
        self.merged.append((pr, head_sha))

    def update_pr_branch(self, pr, head_sha):
        self.updated.append((pr, head_sha))


def _worker(service, github, *, dry_run=False):
    from drover.server.work_queue.rules import WorkItemRuleWorker

    return WorkItemRuleWorker(
        service.store,
        {"example/project": github},
        SimpleNamespace(dry_run=dry_run, batch_size=50),
    )


def _item(service, **policy):
    _register(service)
    item = service.store.hub_items()[0]
    return service.store.patch_hub_item(item["id"], **policy) if policy else item


def test_publish_requires_remote_exact_head_and_duplicate_event_runs_once(service):
    item = _item(service, publish=True)
    github = GitHub()
    worker = _worker(service, github)
    worker.evaluate(item, "worker_done", "a" * 40)
    worker.evaluate(item, "worker_done", "a" * 40)
    assert len(github.opened) == 1


def test_process_once_consumes_event_cursor_and_wakes_blocked_worker(service):
    item = _item(service)
    service.store.append_rule_event(item["id"], kind="blocked", detail="worker")
    worker = _worker(service, GitHub())
    assert worker.process_once() >= 1
    assert worker.store.rule_cursor() > 0
    assert "orchestrator_wake" in [row["kind"] for row in service.store.hub_item(item["id"])["timeline"]]


def test_publish_missing_or_mismatched_branch_wakes_without_pr(service):
    item = _item(service, publish=True)
    for head, reason in ((None, "branch_not_pushed"), ("b" * 40, "head_mismatch")):
        github = GitHub(head)
        _worker(service, github).evaluate(item, "worker_done", "a" * 40)
        kinds = [row["kind"] for row in service.store.hub_item(item["id"])["timeline"]]
        assert "orchestrator_wake" in kinds
        assert not github.opened


def test_stale_or_gated_green_never_merges_and_dry_run_records_only(service):
    item = _item(
        service,
        pr="https://github.com/example/project/pull/7",
        auto_merge=True,
        automation_gate="ui_review",
    )
    github = GitHub()
    worker = _worker(service, github)
    worker.evaluate(item, "ci_green_exact_head", "a" * 40)
    assert not github.merged
    service.store.patch_hub_item(item["id"], automation_gate="none")
    service.store.append_github_fact(item["id"], kind="pr_synchronized", detail="{}", head_sha="a" * 40)
    item = service.store.hub_item(item["id"])
    _worker(service, github, dry_run=True).evaluate(item, "ci_green_exact_head", "a" * 40)
    assert not github.merged


def test_merged_marks_done_and_unblocks_dependents(service):
    blocker = _item(service)
    _register(service, session_id="dependent")
    dependent = service.store.hub_items()[0]
    service.store.patch_hub_item(dependent["id"], stage="blocked", depends_on=[blocker["id"]])
    _worker(service, GitHub()).evaluate(blocker, "pr_merged", "a" * 40)
    assert service.store.hub_item(blocker["id"])["stage"] == "done"
    assert service.store.hub_item(dependent["id"])["stage"] == "queued"


def test_behind_pr_updates_once_only_when_ci_is_not_failed(service):
    item = _item(service, pr="https://github.com/example/project/pull/7", publish=True)
    service.store.append_github_fact(item["id"], kind="pr_synchronized", detail="{}", head_sha="a" * 40)
    item = service.store.hub_item(item["id"])
    github = GitHub()
    worker = _worker(service, github)
    worker.evaluate(item, "pr_behind", "a" * 40)
    worker.evaluate(item, "pr_behind", "a" * 40)
    assert github.updated == [(item["pr"], "a" * 40)]
