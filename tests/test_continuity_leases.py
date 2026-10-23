from datetime import datetime, timedelta, timezone

import pytest

from drover.server.continuity_leases import (
    DeadlinePolicy,
    LeaseConflict,
    OwnerLeaseStore,
    reap,
    select_reap_candidates,
)

NOW = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)


class Cursor:
    description = [("issue_key",), ("owner_id",), ("owner_epoch",), ("lease_until",)]

    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class LeaseConnection:
    def __init__(self):
        self.row = None
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, list(params)))
        if sql.startswith("SELECT"):
            return Cursor(self.row)
        if sql.startswith("INSERT"):
            self.row = dict(
                zip(("issue_key", "owner_id", "owner_epoch", "lease_until"), params[:4])
            )
        elif sql.startswith("UPDATE") and self.row is not None:
            if "owner_id = ?, owner_epoch" in sql:
                self.row.update(
                    owner_id=params[0], owner_epoch=params[1], lease_until=params[2]
                )
            elif "lease_until = ?" in sql:
                self.row["lease_until"] = params[0]
        return Cursor(None)


def test_deadline_policy_is_per_state_and_uses_state_timestamp():
    policy = DeadlinePolicy({"working": 4, "idle_undeclared": 0})
    assert policy.due_at("working", NOW) == NOW + timedelta(seconds=4)
    assert not policy.overdue("working", NOW, NOW + timedelta(seconds=3))
    assert policy.overdue("working", NOW, NOW + timedelta(seconds=4))
    with pytest.raises(KeyError):
        policy.budget("unknown")


def test_owner_lease_acquire_renew_release_are_epoch_fenced():
    con = LeaseConnection()
    store = OwnerLeaseStore(con, clock=lambda: NOW)
    first = store.acquire("drover#785", owner_id="a", lease_seconds=30)
    assert first["owner_epoch"] == 1
    assert store.renew("drover#785", owner_id="a", owner_epoch=1)["owner_epoch"] == 1
    with pytest.raises(LeaseConflict):
        store.renew("drover#785", owner_id="a", owner_epoch=2)
    store.release("drover#785", owner_id="a", owner_epoch=1)
    second = store.acquire("drover#785", owner_id="b")
    assert second["owner_epoch"] == 2
    with pytest.raises(LeaseConflict):
        store.release("drover#785", owner_id="a", owner_epoch=1)


def test_owner_cannot_take_over_live_lease():
    con = LeaseConnection()
    store = OwnerLeaseStore(con, clock=lambda: NOW)
    store.acquire("drover#785", owner_id="a")
    with pytest.raises(LeaseConflict):
        store.acquire("drover#785", owner_id="b")


def test_reap_selects_only_accepted_done_ended_or_old_idle_without_protection():
    rows = [
        {"session_id": "done", "worker_state": "done", "accepted_pr": True},
        {"session_id": "unaccepted", "worker_state": "done", "accepted_pr": False},
        {"session_id": "ended", "worker_state": "ended", "has_open_pr": False},
        {
            "session_id": "idle",
            "worker_state": "idle_undeclared",
            "last_activity": NOW - timedelta(hours=25),
        },
        {
            "session_id": "recent",
            "worker_state": "idle_undeclared",
            "last_activity": NOW - timedelta(hours=23),
        },
        {"session_id": "leased", "worker_state": "ended", "has_live_lease": True},
        {"session_id": "open", "worker_state": "ended", "has_open_pr": True},
    ]
    candidates = select_reap_candidates(rows, now=NOW, enabled_watchers={"ended"})
    assert [(c.session_id, c.reason) for c in candidates] == [
        ("done", "accepted_done"),
        ("idle", "idle_undeclared_24h"),
    ]


def test_reap_defaults_dry_run_and_audits_each_candidate():
    class AuditConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    con = AuditConnection()
    results = reap(
        con,
        select_reap_candidates([{"session_id": "x", "worker_state": "ended"}], now=NOW),
        actor="sweeper",
        now=NOW,
    )
    assert results == [
        {"session_id": "x", "reason": "ended", "dry_run": True, "applied": False}
    ]
    assert len(con.calls) == 1
    assert "continuity_reap_audit" in con.calls[0][0]


def test_apply_reap_terminates_then_archives_and_audits_without_deleting():
    class AuditConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    con, terminated = AuditConnection(), []
    result = reap(
        con,
        select_reap_candidates([{"session_id": "x", "worker_state": "ended"}], now=NOW),
        actor="sweeper",
        dry_run=False,
        terminator=terminated.append,
        now=NOW,
    )
    assert terminated == ["x"]
    assert result[0]["applied"] is True
    assert any("archived_at" in sql for sql, _ in con.calls)
    assert not any("DELETE" in sql for sql, _ in con.calls)
