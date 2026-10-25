"""Real PostgreSQL owner races; uses only the repository's disposable facility.

The pg_control_path -> postgres_dsn fixtures explicitly skip when neither
DROVER_TEST_POSTGRES_DSN nor local initdb exists (or psycopg is missing).
No DuckDB fallback and no production control-store DSN are used here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock, Thread, get_ident

import pytest

from drover.server.control_store import (
    close_control_store,
    configure_control_store,
    control_store_config,
)
from drover.server.db import control_plane_connection
from drover.server.harness import continuity
from drover.server.harness import registry as harness_registry
from drover.server.harness.continuity import (
    ContinuityConflict,
    FactoryObserverContinuity,
    continuity_request,
)
from drover.server.harness.openclaw_owner import ENDPOINT, OpenClawOwnerAdapter
from drover.server.harness.registry import HarnessRegistry
from drover.server.ledger import JobLedger

RUN = "run_PG497"


@pytest.fixture(scope="session", autouse=True)
def _pg_driver_dependencies():
    pytest.importorskip(
        "psycopg",
        reason="PostgreSQL continuity proof requires psycopg (drover[postgres])",
    )
    pytest.importorskip(
        "psycopg_pool",
        reason="PostgreSQL continuity proof requires psycopg_pool (drover[postgres])",
    )


class Clock:
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def pg_bridge(pg_control_path):
    with control_plane_connection(pg_control_path) as con:
        assert con.dialect == "postgres"
        print(
            "Actual PostgreSQL server:", con.execute("SELECT version()").fetchone()[0]
        )
    registry = HarnessRegistry(pg_control_path)
    registry.create_session(
        session_id="pg-observer",
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{RUN}@4",
    )
    clock = Clock()
    store = FactoryObserverContinuity(pg_control_path, clock=clock)
    store.initialize(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
    )
    return store, clock


def race(functions):
    barrier = Barrier(len(functions))

    def execute(function):
        barrier.wait(timeout=5)
        try:
            return function()
        except ContinuityConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(execute, functions))


def report(store):
    return store.report(
        RUN,
        source="openclaw",
        source_event_id="worker:pg:completed:1",
        subject="worker:pg",
        sequence=1,
        kind="worker_brief_completed",
        summary="Commit ready for owner review",
    )


def handoff(store, owner_id):
    store.initialize(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
        authorized_owner_id=owner_id,
    )


def test_http_initialize_requires_host_issued_handoff_on_postgres(pg_bridge, tmp_path):
    """The real PostgreSQL path cannot let the first device seize a run."""
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings
    from drover.server.web.credentials import CredentialStore

    store, _ = pg_bridge
    registry = HarnessRegistry(store.path)
    credentials = CredentialStore(tmp_path / "credentials.json")
    _, host_token = credentials.issue(scope="host", label="studio", host_id="studio")
    owner_credential, owner_token = credentials.issue(scope="device", label="owner")
    _, unrelated_token = credentials.issue(scope="device", label="unrelated")
    owner_principal = "credential:" + owner_credential.id
    collector = MetricsCollector(
        duckdb_path=store.path,
        incoming_dir=tmp_path / "incoming",
        summarizer_report={},
        ttl_seconds=60,
    )
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=collector,
        auth=AuthSettings(
            enabled=True,
            api_token="",
            credentials=credentials,
            legacy_token_enabled=False,
        ),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}/harness/factory-observer/continuity"

    def request(body, token):
        req = urllib.request.Request(
            base,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def initialize(session_id, token, handoff_token=None):
        body = {
            "operation": "initialize",
            "session_id": session_id,
            "objective": "Prove #497 on PostgreSQL",
            "checkpoint": "Pending",
        }
        if handoff_token is not None:
            body["handoff_token"] = handoff_token
        return request(body, token)

    def issue(session_id):
        return request(
            {
                "operation": "issue_handoff",
                "session_id": session_id,
                "objective": "Prove #497 on PostgreSQL",
                "checkpoint": "Pending",
                "authorized_owner_id": owner_principal,
            },
            host_token,
        )

    try:
        assert initialize("pg-observer", unrelated_token)[0] == 403
        assert store.status(RUN)["owner_handoff"]["authorized_owner_id"] is None
        status, issued = issue("pg-observer")
        assert status == 200
        assert (
            initialize("pg-observer", unrelated_token, issued["handoff_token"])[0]
            == 403
        )
        assert store.status(RUN)["owner_handoff"]["authorized_owner_id"] is None
        assert initialize("pg-observer", owner_token, issued["handoff_token"])[0] == 200
        assert initialize("pg-observer", owner_token)[0] == 200

        race_run = "run_PG498"
        race_session = "pg-observer-race"
        registry.create_session(
            session_id=race_session,
            host_id="studio",
            harness="codex",
            command="codex",
            status="running",
            handoff_mode="factory_observer",
            source_session_id=f"factory/{race_run}@4",
        )
        status, issued = issue(race_session)
        assert status == 200
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda token: initialize(
                        race_session, token, issued["handoff_token"]
                    )[0],
                    [unrelated_token, owner_token],
                )
            )
        assert sorted(outcomes) == [200, 403]
        assert store.status(race_run)["owner_handoff"]["authorized_owner_id"] == (
            owner_principal
        )
    finally:
        server.shutdown()
        server.server_close()


def test_two_connections_and_two_owners_race_acquisition(pg_bridge):
    store, _ = pg_bridge
    handoff(store, "owner-a")
    barrier = Barrier(2)

    def backend(_):
        with control_plane_connection(store.path) as con:
            pid = con.execute("SELECT pg_backend_pid()").fetchone()[0]
            barrier.wait(timeout=5)  # Hold both independent PG connections.
            return pid

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(backend, range(2)))) == 2
    outcomes = race(
        [
            lambda: store.lease(RUN, owner_id="owner-a"),
            lambda: store.lease(RUN, owner_id="owner-b"),
        ]
    )
    winners = [result for result in outcomes if isinstance(result, dict)]
    assert len(winners) == 1
    assert sum(isinstance(result, ContinuityConflict) for result in outcomes) == 1
    status = store.status(RUN)
    assert winners[0]["owner_id"] == status["owner"]["id"] == "owner-a"
    assert status["owner"]["epoch"] == winners[0]["owner_epoch"] == 1


def test_owner_and_intruder_race_renewal_with_epoch_cas(pg_bridge):
    store, clock = pg_bridge
    handoff(store, "owner-a")
    lease = store.lease(RUN, owner_id="owner-a", lease_seconds=10)
    epoch = lease["owner_epoch"]
    clock.advance(1)
    outcomes = race(
        [
            lambda: store.lease(
                RUN, owner_id="owner-a", owner_epoch=epoch, lease_seconds=30
            ),
            lambda: store.lease(
                RUN, owner_id="owner-b", owner_epoch=epoch, lease_seconds=60
            ),
        ]
    )
    assert isinstance(outcomes[0], dict)
    assert isinstance(outcomes[1], ContinuityConflict)
    status = store.status(RUN)
    assert status["owner"]["id"] == "owner-a"
    assert status["owner"]["epoch"] == epoch
    until = status["owner"]["lease_until"]
    with pytest.raises(ContinuityConflict, match="epoch changed"):
        store.lease(RUN, owner_id="owner-a", owner_epoch=epoch + 1)
    assert store.status(RUN)["owner"]["lease_until"] == until


def test_expired_readmission_race_and_stale_owner_ack(pg_bridge):
    store, clock = pg_bridge
    handoff(store, "old-owner")
    old = store.lease(RUN, owner_id="old-owner", lease_seconds=5)
    event_id = report(store)
    action = store.consume(RUN, owner_id="old-owner", owner_epoch=old["owner_epoch"])
    clock.advance(5)
    with pytest.raises(ContinuityConflict):
        store.acknowledge(
            RUN,
            owner_id="old-owner",
            owner_epoch=old["owner_epoch"],
            event_id=event_id,
            checkpoint="Stale continuation",
        )
    outcomes = race(
        [
            lambda: store.lease(
                RUN,
                owner_id="old-owner",
                owner_epoch=old["owner_epoch"],
                lease_seconds=5,
            ),
            lambda: store.lease(
                RUN,
                owner_id="new-owner",
                owner_epoch=old["owner_epoch"],
                lease_seconds=5,
            ),
        ]
    )
    [winner] = [result for result in outcomes if isinstance(result, dict)]
    assert winner["owner_epoch"] == old["owner_epoch"] + 1
    fence = {"owner_id": winner["owner_id"], "owner_epoch": winner["owner_epoch"]}
    assert store.consume(RUN, **fence) == action
    with pytest.raises(ContinuityConflict):
        store.acknowledge(
            RUN,
            owner_id="old-owner",
            owner_epoch=old["owner_epoch"],
            event_id=event_id,
            checkpoint="Stale continuation",
        )
    clock.advance(5)
    # The newer lease is also expired: an old delayed renewal still fails CAS.
    with pytest.raises(ContinuityConflict, match="epoch changed"):
        store.lease(RUN, owner_id="old-owner", owner_epoch=old["owner_epoch"])
    assert store.status(RUN)["owner"]["epoch"] == winner["owner_epoch"]
    recovered = store.lease(
        RUN, owner_id=winner["owner_id"], owner_epoch=winner["owner_epoch"]
    )
    assert recovered["owner_epoch"] == winner["owner_epoch"] + 1
    store.acknowledge(
        RUN,
        owner_id=recovered["owner_id"],
        owner_epoch=recovered["owner_epoch"],
        event_id=event_id,
        checkpoint="Current owner reconciled",
    )
    assert store.status(RUN)["checkpoint"] == "Current owner reconciled"


def test_duplicate_source_retries_race_before_and_after_ack(pg_bridge):
    store, _ = pg_bridge
    handoff(store, "owner")
    ids = race([lambda: report(store), lambda: report(store)])
    assert ids[0] == ids[1] and isinstance(ids[0], str)
    assert store.status(RUN)["inbox_counts"] == {"pending": 1}
    with pytest.raises(ContinuityConflict, match="divergent"):
        store.report(
            RUN,
            source="openclaw",
            source_event_id="worker:pg:completed:1",
            subject="worker:pg",
            sequence=1,
            kind="worker_awaiting_input",
            summary="Changed identity",
        )
    lease = store.lease(RUN, owner_id="owner")
    fence = {"owner_id": "owner", "owner_epoch": lease["owner_epoch"]}
    action = store.consume(RUN, **fence)
    assert action["type"] == "review_worker_result"
    store.acknowledge(RUN, **fence, event_id=ids[0], checkpoint="Owner reviewed commit")
    assert race([lambda: report(store), lambda: report(store)]) == ids
    assert store.consume(RUN, **fence) is None


def test_sequenced_drover_completion_reaches_leased_owner_on_postgres(pg_bridge):
    store, _ = pg_bridge
    registry = HarnessRegistry(store.path)
    store.initialize(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
        authorized_owner_id="postgres-owner",
    )
    lease = store.lease(RUN, owner_id="postgres-owner")
    fence = {"owner_id": "postgres-owner", "owner_epoch": lease["owner_epoch"]}

    registry.append_event(
        event_id="pg-drover-completion-1",
        session_id="pg-observer",
        event_type="status",
        payload={"turn_complete": True},
        seq=1,
    )

    [event] = store.status(RUN)["events"]
    assert event["report"]["source"] == "drover"
    assert event["report"]["source_event_id"] == "pg-drover-completion-1"
    assert event["report"]["subject"] == "pg-observer"
    assert store.consume(RUN, **fence)["type"] == "review_worker_result"
    store.acknowledge(
        RUN,
        **fence,
        event_id=event["event_id"],
        checkpoint="PostgreSQL owner reviewed Drover completion",
    )
    assert store.status(RUN)["inbox_counts"] == {"acknowledged": 1}


def test_batched_completion_projection_retains_retry_and_rejection_on_postgres(
    pg_bridge,
):
    store, clock = pg_bridge
    registry = HarnessRegistry(store.path)
    store.initialize(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
        authorized_owner_id="postgres-batch-owner",
    )
    records = [
        {
            "event_id": "pg-batch-completion-2",
            "session_id": "pg-observer",
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": 2,
            "created_at": clock.now + timedelta(seconds=2),
        },
        {
            "event_id": "pg-batch-completion-1",
            "session_id": "pg-observer",
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": 1,
            "created_at": clock.now + timedelta(seconds=1),
        },
        {
            "event_id": "pg-batch-malformed",
            "session_id": "pg-observer",
            "event_type": "status",
            "payload": {"turn_complete": True},
            "created_at": clock.now + timedelta(seconds=3),
        },
        {
            "event_id": "pg-batch-completion-1",
            "session_id": "pg-observer",
            "event_type": "status",
            "payload": {"turn_complete": True},
            "seq": 1,
            "created_at": clock.now + timedelta(seconds=1),
        },
    ]
    assert registry.ingest_structured_events(records) == 3
    status = store.status(RUN)
    assert [event["report"]["source_event_id"] for event in status["events"]] == [
        "pg-batch-completion-1",
        "pg-batch-completion-2",
    ]
    assert status["completion_receipts"] == {
        "counts": {"projected": 2, "rejected": 1},
        "errors": [
            {
                "harness_event_id": "pg-batch-malformed",
                "state": "rejected",
                "reason": "completion_requires_nonnegative_sequence",
            }
        ],
    }
    assert registry.ingest_structured_events(records) == 0


def test_pending_completion_recovers_after_authorized_handoff_on_postgres(pg_bridge):
    store, clock = pg_bridge
    registry = HarnessRegistry(store.path)
    assert (
        registry.ingest_structured_events(
            [
                {
                    "event_id": "pg-completion-before-owner-handoff",
                    "session_id": "pg-observer",
                    "event_type": "status",
                    "payload": {"payload": {"turn_complete": True}},
                    "seq": 1,
                    "created_at": clock.now,
                }
            ]
        )
        == 1
    )
    assert store.status(RUN)["completion_receipts"]["counts"] == {
        "pending_authorization": 1
    }
    handoff_token = store.issue_handoff(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
        authorized_owner_id="postgres-recovery-owner",
        issued_by="credential:postgres-host",
    )["handoff_token"]
    with pytest.raises(PermissionError, match="not authorized"):
        store.initialize(
            session_id="pg-observer",
            objective="Prove #497 on PostgreSQL",
            checkpoint="Pending",
            authorized_owner_id="postgres-forged-owner",
            handoff_token=handoff_token,
        )
    store.initialize(
        session_id="pg-observer",
        objective="Prove #497 on PostgreSQL",
        checkpoint="Pending",
        authorized_owner_id="postgres-recovery-owner",
        handoff_token=handoff_token,
    )
    recovered = store.status(RUN)
    assert recovered["completion_receipts"] == {
        "counts": {"projected": 1},
        "errors": [],
    }
    lease = store.lease(RUN, owner_id="postgres-recovery-owner")
    action = store.consume(
        RUN,
        owner_id="postgres-recovery-owner",
        owner_epoch=lease["owner_epoch"],
    )
    assert action["type"] == "review_worker_result"
    store.acknowledge(
        RUN,
        owner_id="postgres-recovery-owner",
        owner_epoch=lease["owner_epoch"],
        event_id=action["event_id"],
        checkpoint="PostgreSQL owner acknowledged recovered completion",
    )
    assert store.status(RUN)["inbox_counts"] == {"acknowledged": 1}


def test_first_run_completion_projection_serializes_with_handoff_on_postgres(
    pg_control_path, monkeypatch
):
    """A first-run completion cannot commit behind its owner-recovery scan."""
    run = "run_PG499"
    session_id = "pg-observer-first-run"
    event_id = "pg-first-run-completion"
    clock = Clock()
    registry = HarnessRegistry(pg_control_path)
    registry.create_session(
        session_id=session_id,
        host_id="studio",
        harness="codex",
        command="codex",
        status="running",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{run}@4",
    )
    store = FactoryObserverContinuity(pg_control_path, clock=clock)
    receipt_entered = Event()
    release_receipt = Event()
    initialization_finished = Event()
    ingestion_errors: list[BaseException] = []
    initialization_errors: list[BaseException] = []
    original_receipt = continuity._record_completion_receipt

    def hold_first_pending_receipt(*args, **kwargs):
        if (
            kwargs["harness_event_id"] == event_id
            and kwargs["run_id"] is None
            and not receipt_entered.is_set()
        ):
            receipt_entered.set()
            assert release_receipt.wait(timeout=5)
        return original_receipt(*args, **kwargs)

    monkeypatch.setattr(
        continuity, "_record_completion_receipt", hold_first_pending_receipt
    )
    record = {
        "event_id": event_id,
        "session_id": session_id,
        "event_type": "status",
        "payload": {"payload": {"turn_complete": True}},
        "seq": 1,
        "created_at": clock.now,
    }

    def ingest():
        try:
            assert registry.ingest_structured_events([record]) == 1
        except BaseException as exc:  # preserve assertion failures from the thread
            ingestion_errors.append(exc)

    def issue_and_redeem():
        try:
            handoff = store.issue_handoff(
                session_id=session_id,
                objective="Prove first-run completion serialization",
                checkpoint="Pending",
                authorized_owner_id="credential:pg-owner",
                issued_by="credential:pg-host",
            )
            store.initialize(
                session_id=session_id,
                objective="Prove first-run completion serialization",
                checkpoint="Pending",
                authorized_owner_id="credential:pg-owner",
                handoff_token=handoff["handoff_token"],
            )
        except BaseException as exc:  # preserve assertion failures from the thread
            initialization_errors.append(exc)
        finally:
            initialization_finished.set()

    ingestion = Thread(target=ingest)
    initialization = Thread(target=issue_and_redeem)
    try:
        ingestion.start()
        assert receipt_entered.wait(timeout=5)
        initialization.start()
        # The session row serializes first-run projection with handoff creation.
        # Event coordination proves blocking without an arbitrary sleep.
        assert not initialization_finished.wait(timeout=0.2)
        release_receipt.set()
        ingestion.join(timeout=5)
        initialization.join(timeout=5)
        assert not ingestion.is_alive()
        assert not initialization.is_alive()
    finally:
        release_receipt.set()
        ingestion.join(timeout=5)
        initialization.join(timeout=5)

    assert not ingestion_errors
    assert not initialization_errors
    # A retry cannot hide the same durable source fact after the restart/poll view.
    assert registry.ingest_structured_events([record]) == 0
    restarted = FactoryObserverContinuity(pg_control_path, clock=clock)
    status = restarted.status(run)
    assert status["completion_receipts"]["counts"] == {"projected": 1}
    assert status["inbox_counts"] == {"pending": 1}
    assert status["owner_handoff"] == {
        "authorized_owner_id": "credential:pg-owner",
        "state": "owner_lease_required",
    }


def test_http_report_cannot_claim_drover_completion_identity_on_postgres(
    pg_bridge, tmp_path
):
    """A different authenticated device cannot poison an internal completion."""
    from drover.server.metrics import MetricsCollector
    from drover.server.web.app import start_metrics_server
    from drover.server.web.auth import AuthSettings
    from drover.server.web.credentials import CredentialStore

    store, clock = pg_bridge
    event_id = "pg-internal-drover-completion"
    registry = HarnessRegistry(store.path)
    assert (
        registry.ingest_structured_events(
            [
                {
                    "event_id": event_id,
                    "session_id": "pg-observer",
                    "event_type": "status",
                    "payload": {"payload": {"turn_complete": True}},
                    "seq": 1,
                    "created_at": clock.now,
                }
            ]
        )
        == 1
    )
    credentials = CredentialStore(tmp_path / "credentials.json")
    _, host_token = credentials.issue(scope="host", label="studio", host_id="studio")
    owner, owner_token = credentials.issue(scope="device", label="owner")
    _, other_token = credentials.issue(scope="device", label="other")
    server = start_metrics_server(
        host="127.0.0.1",
        port=0,
        collector=MetricsCollector(
            duckdb_path=store.path,
            incoming_dir=tmp_path / "incoming",
            summarizer_report={},
            ttl_seconds=60,
        ),
        auth=AuthSettings(
            enabled=True,
            api_token="",
            credentials=credentials,
            legacy_token_enabled=False,
        ),
    )
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/harness/factory-observer/continuity"

    def request(body, token):
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert (
            request(
                {
                    "operation": "report",
                    "run_id": RUN,
                    "source": "drover",
                    "source_event_id": event_id,
                    "subject": "pg-observer",
                    "sequence": 1,
                    "kind": "ci_red",
                    "summary": "Unrelated report cannot replace completion",
                },
                other_token,
            )[0]
            == 403
        )
        assert store.status(RUN)["inbox_counts"] == {}
        status, issued = request(
            {
                "operation": "issue_handoff",
                "session_id": "pg-observer",
                "objective": "Prove #497 on PostgreSQL",
                "checkpoint": "Pending",
                "authorized_owner_id": "credential:" + owner.id,
            },
            host_token,
        )
        assert status == 200
        status, initialized = request(
            {
                "operation": "initialize",
                "session_id": "pg-observer",
                "objective": "Prove #497 on PostgreSQL",
                "checkpoint": "Pending",
                "handoff_token": issued["handoff_token"],
            },
            owner_token,
        )
        assert status == 200
        assert initialized["continuity"]["completion_receipts"]["counts"] == {
            "projected": 1
        }
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("scope", ["integration", "deployment"])
def test_python_adapter_first_handoff_preserves_scope_on_postgres(
    pg_control_path, scope
):
    """The trusted adapter can bind a fresh PostgreSQL run without a GET race."""
    run_id = f"run_PG_ADAPTER_{scope.upper()}"
    session_id = f"pg-adapter-{scope}"
    registry = HarnessRegistry(pg_control_path)
    registry.create_session(
        session_id=session_id,
        host_id="studio",
        harness="codex",
        command="codex",
        handoff_mode="factory_observer",
        source_session_id=f"factory/{run_id}@4",
    )
    store = FactoryObserverContinuity(pg_control_path)
    store.initialize(
        session_id=session_id,
        objective="Redeem adapter handoff",
        checkpoint="Pending",
        authority_scope=scope,
    )
    handoff = store.issue_handoff(
        session_id=session_id,
        objective="Redeem adapter handoff",
        checkpoint="Pending",
        authority_scope=scope,
        authorized_owner_id="credential:pg-owner",
        issued_by="credential:pg-host",
    )

    class Endpoint:
        def get(self, path, *, query):
            assert path == ENDPOINT
            return {"continuity": store.status(**query)}

        def post(self, path, *, body):
            assert path == ENDPOINT
            return continuity_request(
                store, {**body, "authorized_owner_id": "credential:pg-owner"}
            )

    reply = OpenClawOwnerAdapter(
        Endpoint(),
        run_id=run_id,
        owner_id="credential:pg-owner",
        authority_scope=scope,
        session_id=session_id,
        objective="Redeem adapter handoff",
        checkpoint="Pending",
        handoff_token=handoff["handoff_token"],
    ).invoke({"version": 1, "request": {"operation": "initialize"}})
    assert reply.continuity.authority_scope == scope
    assert reply.continuity.owner_handoff.authorized_owner_id == "credential:pg-owner"


@pytest.mark.parametrize("method", ["append_events_if_new", "ingest_structured_events"])
def test_bulk_completion_projection_locks_sessions_in_one_order_on_postgres(
    pg_control_path, monkeypatch, method
):
    """Opposite-order batches cannot deadlock while retaining their whole commit."""
    registry = HarnessRegistry(pg_control_path)
    store = FactoryObserverContinuity(pg_control_path)
    for session_id, run_id in (
        ("pg-lock-order-a", "run_PG_LOCK_A"),
        ("pg-lock-order-b", "run_PG_LOCK_B"),
    ):
        registry.create_session(
            session_id=session_id,
            host_id="studio",
            harness="codex",
            command="codex",
            handoff_mode="factory_observer",
            source_session_id=f"factory/{run_id}@4",
        )
        store.initialize(
            session_id=session_id,
            objective="Prove global completion lock order",
            checkpoint="Pending",
            authorized_owner_id="credential:pg-owner",
        )
    barrier = Barrier(2)
    seen_threads: set[int] = set()
    seen_lock = Lock()
    original_lock = continuity._lock_observer_session
    # This regression isolates observer-session row locking. The structured
    # recap advisory-lock ordering has a dedicated test immediately below.
    monkeypatch.setattr(
        harness_registry,
        "lock_live_recap_sessions",
        lambda *_args, **_kwargs: None,
    )

    def coordinated_lock(con, session_id):
        with seen_lock:
            first_for_thread = get_ident() not in seen_threads
            seen_threads.add(get_ident())
        if first_for_thread:
            barrier.wait(timeout=5)
        return original_lock(con, session_id)

    monkeypatch.setattr(continuity, "_lock_observer_session", coordinated_lock)
    errors: list[BaseException] = []

    def records(first, second):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        batch = [
            {
                "event_id": f"{first}-completion-{sequence}",
                "session_id": first,
                "event_type": "status",
                "payload": {"payload": {"turn_complete": True}},
                "seq": sequence,
                "created_at": now + timedelta(seconds=sequence),
            }
            for sequence in range(1, 101)
        ]
        batch.append(
            {
                "event_id": f"{first}-then-{second}",
                "session_id": second,
                "event_type": "status",
                "payload": {"payload": {"turn_complete": True}},
                "seq": 101,
                "created_at": now + timedelta(seconds=101),
            }
        )
        return batch

    def ingest(first, second):
        try:
            assert getattr(registry, method)(records(first, second)) == 101
        except BaseException as exc:  # preserve thread failures for the assertion
            errors.append(exc)

    threads = [
        Thread(target=ingest, args=("pg-lock-order-b", "pg-lock-order-a")),
        Thread(target=ingest, args=("pg-lock-order-a", "pg-lock-order-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert not errors
    for run_id in ("run_PG_LOCK_A", "run_PG_LOCK_B"):
        assert store.status(run_id)["completion_receipts"]["counts"] == {
            "projected": 101
        }


@pytest.mark.parametrize("method", ["append_events_if_new", "ingest_structured_events"])
def test_structured_bulk_completion_recaps_lock_sessions_in_one_order_on_postgres(
    pg_control_path, monkeypatch, method
):
    """Structured recap advisory locks are acquired lexically across all chunks."""
    registry = HarnessRegistry(pg_control_path)
    store = FactoryObserverContinuity(pg_control_path)
    for session_id, run_id in (
        ("pg-recap-lock-order-a", "run_PG_RECAP_LOCK_A"),
        ("pg-recap-lock-order-b", "run_PG_RECAP_LOCK_B"),
    ):
        registry.create_session(
            session_id=session_id,
            host_id="studio",
            harness="codex",
            command="codex",
            mode="structured",
            handoff_mode="factory_observer",
            source_session_id=f"factory/{run_id}@4",
        )
        store.initialize(
            session_id=session_id,
            objective="Prove global recap lock order",
            checkpoint="Pending",
            authorized_owner_id="credential:pg-owner",
        )

    first_lock_barrier = Barrier(2)
    opposite_first_lock_barrier = Barrier(2)
    seen_threads: set[int] = set()
    first_subjects: dict[int, str] = {}
    seen_lock = Lock()
    original_lock = JobLedger.lock_subject

    def coordinated_lock(con, job_kind, subject_key):
        with seen_lock:
            first_for_thread = get_ident() not in seen_threads
            seen_threads.add(get_ident())
            if first_for_thread:
                first_subjects[get_ident()] = subject_key
        if first_for_thread:
            # Both old input-order paths first acquire different subjects and
            # then proceed to a second conflicting lock. A repaired lexical
            # pre-lock instead has both threads contend for the same first
            # subject, so it must be allowed to serialize without a barrier.
            first_lock_barrier.wait(timeout=5)
        original_lock(con, job_kind, subject_key)
        if first_for_thread:
            with seen_lock:
                opposite_first_subjects = len(set(first_subjects.values())) == 2
            if opposite_first_subjects:
                opposite_first_lock_barrier.wait(timeout=5)

    monkeypatch.setattr(JobLedger, "lock_subject", staticmethod(coordinated_lock))

    def records(first, second):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        batch = [
            {
                "event_id": f"{first}-recap-completion-{sequence}",
                "session_id": first,
                "event_type": "status",
                "payload": {"payload": {"turn_complete": True}},
                "seq": sequence,
                "created_at": now + timedelta(seconds=sequence),
            }
            for sequence in range(1, 101)
        ]
        batch.append(
            {
                "event_id": f"{first}-recap-then-{second}",
                "session_id": second,
                "event_type": "status",
                "payload": {"payload": {"turn_complete": True}},
                "seq": 101,
                "created_at": now + timedelta(seconds=101),
            }
        )
        return batch

    def ingest(first, second):
        return getattr(registry, method)(records(first, second))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=20)
            for future in (
                pool.submit(ingest, "pg-recap-lock-order-b", "pg-recap-lock-order-a"),
                pool.submit(ingest, "pg-recap-lock-order-a", "pg-recap-lock-order-b"),
            )
        ]
    assert outcomes == [101, 101]
    for run_id in ("run_PG_RECAP_LOCK_A", "run_PG_RECAP_LOCK_B"):
        assert store.status(run_id)["completion_receipts"]["counts"] == {
            "projected": 101
        }


@pytest.mark.parametrize("method", ["append_events_if_new", "ingest_structured_events"])
def test_reconciliation_and_bulk_recaps_share_one_lock_order_on_postgres(
    pg_control_path, monkeypatch, method
):
    """Reconciliation cannot invert a structured bulk recap lock order."""
    registry = HarnessRegistry(pg_control_path)
    sessions = ("pg-reconcile-lock-a", "pg-reconcile-lock-b")
    for session_id in sessions:
        registry.create_session(
            session_id=session_id,
            host_id="studio",
            harness="codex",
            command="codex",
            mode="structured",
        )
        registry.append_event(
            event_id=f"{session_id}-stored-completion",
            session_id=session_id,
            event_type="status",
            payload={"payload": {"turn_complete": True}},
            seq=1,
        )

    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    with control_plane_connection(pg_control_path) as con:
        # Reconciliation reads B then A, deliberately opposite the lexical
        # order required by the concurrent A/B bulk delivery.
        con.execute(
            "UPDATE harness_sessions SET recap_reconcile_needed = TRUE, updated_at = ? "
            "WHERE session_id = ?",
            [now, "pg-reconcile-lock-b"],
        )
        con.execute(
            "UPDATE harness_sessions SET recap_reconcile_needed = TRUE, updated_at = ? "
            "WHERE session_id = ?",
            [now + timedelta(seconds=1), "pg-reconcile-lock-a"],
        )

    first_lock_barrier = Barrier(2)
    opposite_first_lock_barrier = Barrier(2)
    seen_threads: set[int] = set()
    first_subjects: dict[int, str] = {}
    seen_lock = Lock()
    original_lock = JobLedger.lock_subject

    def coordinated_lock(con, job_kind, subject_key):
        with seen_lock:
            first_for_thread = get_ident() not in seen_threads
            seen_threads.add(get_ident())
            if first_for_thread:
                first_subjects[get_ident()] = subject_key
        if first_for_thread:
            # Before the repair, reconciliation first locks B and the bulk
            # path first locks A. After both call the shared lexical helper,
            # they contend for A and must serialize instead.
            first_lock_barrier.wait(timeout=5)
        original_lock(con, job_kind, subject_key)
        if first_for_thread:
            with seen_lock:
                opposite_first_subjects = len(set(first_subjects.values())) == 2
            if opposite_first_subjects:
                opposite_first_lock_barrier.wait(timeout=5)

    monkeypatch.setattr(JobLedger, "lock_subject", staticmethod(coordinated_lock))

    def bulk_records():
        return [
            {
                "event_id": f"{session_id}-bulk-completion",
                "session_id": session_id,
                "event_type": "status",
                "payload": {"payload": {"turn_complete": True}},
                "seq": 2,
                "created_at": now + timedelta(seconds=2),
            }
            for session_id in sessions
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        reconciliation = pool.submit(registry.reconcile_orphan_completions, limit=2)
        bulk = pool.submit(getattr(registry, method), bulk_records())
        assert reconciliation.result(timeout=20) == 0
        assert bulk.result(timeout=20) == 2

    with control_plane_connection(pg_control_path) as con:
        markers = con.execute(
            "SELECT session_id, recap_reconcile_needed FROM harness_sessions "
            "WHERE session_id IN (?, ?) ORDER BY session_id",
            list(sessions),
        ).fetchall()
    assert markers == [
        ("pg-reconcile-lock-a", False),
        ("pg-reconcile-lock-b", False),
    ]


def test_unbound_run_denies_mutations_until_authorized_handoff_on_postgres(pg_bridge):
    store, _ = pg_bridge
    event_id = report(store)
    for mutate in (
        lambda: store.lease(RUN, owner_id="unrelated-owner"),
        lambda: store.consume(RUN, owner_id="unrelated-owner", owner_epoch=0),
        lambda: store.acknowledge(
            RUN,
            owner_id="unrelated-owner",
            owner_epoch=0,
            event_id=event_id,
            checkpoint="Unauthorized acknowledgement",
        ),
    ):
        with pytest.raises(ContinuityConflict, match="handoff"):
            mutate()
    handoff(store, "postgres-authorized-owner")
    lease = store.lease(RUN, owner_id="postgres-authorized-owner")
    assert (
        store.consume(
            RUN,
            owner_id="postgres-authorized-owner",
            owner_epoch=lease["owner_epoch"],
        )["type"]
        == "review_worker_result"
    )


def test_retry_pool_restart_persistence_and_bounded_exhaustion(pg_bridge):
    store, clock = pg_bridge
    handoff(store, "owner")
    lease = store.lease(RUN, owner_id="owner", lease_seconds=5)
    fence = {"owner_id": "owner", "owner_epoch": lease["owner_epoch"]}
    event_id = report(store)
    action = store.consume(RUN, **fence)
    config = control_store_config(store.path)
    close_control_store(store.path)  # Close/reconstruct clients, never restart PG.
    configure_control_store(store.path, config)
    restarted = FactoryObserverContinuity(store.path, clock=clock)
    assert restarted.status(RUN)["next_action"] == action
    assert restarted.status(RUN)["events"][0]["attempts"] == 1
    assert restarted.consume(RUN, **fence) is None
    clock.advance(5)
    assert restarted.status(RUN)["recovery"] == "reclaim_expired_owner"
    lease = restarted.lease(RUN, owner_id="owner")
    fence = {"owner_id": "owner", "owner_epoch": lease["owner_epoch"]}
    assert restarted.consume(RUN, **fence) == action
    clock.advance(10)
    assert restarted.consume(RUN, **fence) == action
    clock.advance(20)
    assert restarted.consume(RUN, **fence) is None
    assert restarted.status(RUN)["recovery"] == "manual_attention"
    assert restarted.status(RUN)["events"][0]["attempts"] == 3
    restarted.acknowledge(
        RUN, **fence, event_id=event_id, checkpoint="Recovered checkpoint"
    )
    assert report(restarted) == event_id
    close_control_store(store.path)
    configure_control_store(store.path, config)
    recovered = FactoryObserverContinuity(store.path, clock=clock)
    assert recovered.status(RUN)["checkpoint"] == "Recovered checkpoint"
    assert recovered.status(RUN)["inbox_counts"] == {"acknowledged": 1}
    assert recovered.consume(RUN, **fence) is None


def test_idle_undeclared_sweep_concurrent_sweepers_report_once(pg_control_path):
    now = datetime(2026, 10, 25, 5, tzinfo=timezone.utc)
    HarnessRegistry(pg_control_path).create_session(
        session_id="pg-idle",
        host_id="studio",
        harness="agy",
        command="agy",
        status="running",
        handoff_mode="work_coordinator",
        source_session_id="work/work-pg-idle@0",
    )
    store = FactoryObserverContinuity(pg_control_path, clock=lambda: now)
    run_id = store.initialize(
        session_id="pg-idle",
        objective="review",
        checkpoint="launched",
        authorized_owner_id="agent:coder:subagent:owner",
    )
    with store._transaction() as (con, _):
        con.execute(
            "UPDATE harness_sessions SET awaiting = 'input', last_activity = ? "
            "WHERE session_id = 'pg-idle'",
            [now - timedelta(minutes=10)],
        )
        for seq, (kind, text) in enumerate(
            (("user_input", "go"), ("assistant_output", "final review"))
        ):
            con.execute(
                """INSERT INTO harness_events
                   (event_id, session_id, event_type, payload_json, created_at, seq)
                   VALUES (?, 'pg-idle', ?, ?, ?, ?)""",
                [
                    f"pg-idle-{seq}",
                    kind,
                    json.dumps({"text": text}),
                    now - timedelta(minutes=10, seconds=-seq),
                    seq,
                ],
            )
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: store.sweep_idle_undeclared(300), range(4)))
    assert sum(len(ids) for ids in results) == 1
    [event] = store.status(run_id)["events"]
    assert event["report"]["source"] == continuity.IDLE_SOURCE
    assert "final review" in event["report"]["summary"]
