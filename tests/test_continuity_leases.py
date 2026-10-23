from datetime import datetime, timedelta, timezone

import pytest

from drover.server.continuity_leases import (
    DeadlinePolicy,
    LeaseConflict,
    OwnerLeaseStore,
    ReapCandidate,
    reap,
    select_reap_candidates,
)

NOW = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)
CLEAR = {"has_open_pr": False, "has_live_lease": False, "has_watcher": False}


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


class PostgresLeaseConnection(LeaseConnection):
    dialect = "postgres"

    def execute(self, sql, params=()):
        if sql in {"BEGIN", "COMMIT", "ROLLBACK"} or "pg_advisory_xact_lock" in sql:
            self.calls.append((sql, list(params)))
            return Cursor(None)
        cursor = super().execute(sql, params)
        if "RETURNING issue_key" in sql:
            return Cursor(
                {"issue_key": params[0] if sql.startswith("INSERT") else "drover#785"}
            )
        return cursor


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
    assert "FOR UPDATE" not in con.calls[0][0]
    assert store.renew("drover#785", owner_id="a", owner_epoch=1)["owner_epoch"] == 1
    with pytest.raises(LeaseConflict):
        store.renew("drover#785", owner_id="a", owner_epoch=2)
    store.release("drover#785", owner_id="a", owner_epoch=1)
    assert store.acquire("drover#785", owner_id="b")["owner_epoch"] == 2
    with pytest.raises(LeaseConflict):
        store.release("drover#785", owner_id="a", owner_epoch=1)


def test_owner_cannot_take_over_live_lease():
    con = LeaseConnection()
    store = OwnerLeaseStore(con, clock=lambda: NOW)
    store.acquire("drover#785", owner_id="a")
    with pytest.raises(LeaseConflict):
        store.acquire("drover#785", owner_id="b")


def test_postgres_lease_writes_are_transactional_advisory_locked_and_returning_fenced():
    con = PostgresLeaseConnection()
    OwnerLeaseStore(con, clock=lambda: NOW).acquire("drover#785", owner_id="a")
    statements = [sql for sql, _ in con.calls]
    assert statements[0] == "BEGIN"
    assert any("pg_advisory_xact_lock" in sql for sql in statements)
    assert any("FOR UPDATE" in sql for sql in statements)
    assert any("RETURNING issue_key" in sql for sql in statements)
    assert statements[-1] == "COMMIT"


def test_reap_selects_only_terminal_or_old_idle_with_explicit_clear_protections():
    rows = [
        {"session_id": "done", "worker_state": "done", "accepted_pr": True, **CLEAR},
        {
            "session_id": "unaccepted",
            "worker_state": "done",
            "accepted_pr": False,
            **CLEAR,
        },
        {"session_id": "ended", "worker_state": "ended", **CLEAR},
        {
            "session_id": "idle",
            "worker_state": "idle_undeclared",
            "last_activity": NOW - timedelta(hours=25),
            **CLEAR,
        },
        {
            "session_id": "recent",
            "worker_state": "idle_undeclared",
            "last_activity": NOW - timedelta(hours=23),
            **CLEAR,
        },
        {
            "session_id": "leased",
            "worker_state": "ended",
            "has_live_lease": True,
            "has_open_pr": False,
            "has_watcher": False,
        },
        {
            "session_id": "open",
            "worker_state": "ended",
            "has_open_pr": True,
            "has_live_lease": False,
            "has_watcher": False,
        },
        {"session_id": "unknown", "worker_state": "ended"},
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
    candidates = select_reap_candidates(
        [{"session_id": "x", "worker_state": "ended", **CLEAR}], now=NOW
    )
    results = reap(con, candidates, actor="sweeper", now=NOW)
    assert results == [
        {"session_id": "x", "reason": "ended", "dry_run": True, "applied": False}
    ]
    assert len(con.calls) == 1
    assert "continuity_reap_audit" in con.calls[0][0]


def test_apply_reap_records_intent_then_confirms_acknowledged_termination():
    class AuditConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    con, terminated = AuditConnection(), []
    candidates = select_reap_candidates(
        [{"session_id": "x", "worker_state": "ended", **CLEAR}], now=NOW
    )
    result = reap(
        con,
        candidates,
        actor="sweeper",
        dry_run=False,
        terminator=lambda sid: terminated.append(sid) or True,
        protection_check=lambda _: CLEAR,
        now=NOW,
    )
    assert terminated == ["x"]
    assert result[0]["applied"] is True
    assert any("archived_at" in sql for sql, _ in con.calls)
    assert [p[2] for sql, p in con.calls if "continuity_reap_operations" in sql] == [
        "intent",
        "confirmed",
    ]
    assert not any("DELETE" in sql for sql, _ in con.calls)


def test_reap_fails_closed_for_missing_or_changed_protection_evidence():
    assert (
        select_reap_candidates([{"session_id": "x", "worker_state": "ended"}], now=NOW)
        == []
    )

    class AuditConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    con, terminated = AuditConnection(), []
    result = reap(
        con,
        [ReapCandidate("x", "ended")],
        actor="sweeper",
        dry_run=False,
        terminator=lambda sid: terminated.append(sid) or True,
        protection_check=lambda _: {"has_open_pr": False, "has_live_lease": False},
        now=NOW,
    )
    assert terminated == []
    assert result[0]["applied"] is False
    assert "failed" in [
        p[2] for sql, p in con.calls if "continuity_reap_operations" in sql
    ]


def test_reap_records_failed_outcome_when_terminator_does_not_acknowledge():
    class AuditConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    con = AuditConnection()
    candidate = select_reap_candidates(
        [{"session_id": "x", "worker_state": "ended", **CLEAR}], now=NOW
    )
    result = reap(
        con,
        candidate,
        actor="sweeper",
        dry_run=False,
        terminator=lambda _: None,
        protection_check=lambda _: CLEAR,
        now=NOW,
    )
    assert result[0]["applied"] is False
    assert not any("archived_at" in sql for sql, _ in con.calls)
    assert [p[2] for sql, p in con.calls if "continuity_reap_operations" in sql] == [
        "intent",
        "failed",
    ]
