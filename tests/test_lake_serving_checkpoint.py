"""Rolling serving-proof checkpoints: bounded checks without weaker ones.

The serving check refuses more than 10,000 exports since the pinned proof;
prod exports 2.5-11 a minute. The hub's exporter checkpoints the same check
so serving only re-checks exports after it, with no config edit or restart.
"""

import json
from types import SimpleNamespace

import pyarrow as pa
import pytest
from test_lake_child_statement_costs import _receipt, proven_lake  # noqa: F401

from drover.server.lake import query_process, serving_proof
from drover.server.lake.export_worker import receipt_hash
from drover.server.lake.lifecycle import CHECKPOINT_EVERY_EXPORTS, ExporterLifecycle
from drover.server.lake.runtime import LakeError

HELD = SimpleNamespace(check=lambda: None)


def _bulk_export(con, first_snapshot, count, prefix):
    receipts = [_receipt(f"{prefix}-{index:05d}") for index in range(count)]
    snapshots = pa.Table.from_pylist(
        [
            {
                "snapshot_id": first_snapshot + index,
                "author": "drover-export",
                "commit_message": receipt["batch_id"],
                "commit_extra_info": receipt_hash(receipt),
            }
            for index, receipt in enumerate(receipts)
        ]
    )
    rows = pa.Table.from_pylist(receipts)
    con.register("bulk_receipts", rows)
    con.register("bulk_snapshots", snapshots)
    try:
        con.execute(
            "INSERT INTO lake.export_batch_receipts SELECT * FROM bulk_receipts"
        )
        con.execute("INSERT INTO lake.snapshot_log SELECT * FROM bulk_snapshots")
    finally:
        con.unregister("bulk_receipts")
        con.unregister("bulk_snapshots")
    return first_snapshot + count


@pytest.fixture
def advance(proven_lake, monkeypatch):  # noqa: F811
    """advance_checkpoint with its admitted child replaced by an in-process check."""
    spec, con, digest, _ = proven_lake

    def child(child_spec, sql, *, serving):
        assert serving == {"verification_sha256": digest, "checkpoint": True}
        return {"checkpoint": serving_proof.check_proof(child_spec, con, digest)}

    monkeypatch.setattr(query_process, "query", child)
    return lambda fence=HELD: serving_proof.advance_checkpoint(
        spec, digest, fence=fence
    )


def _checkpoint_file(spec):
    return spec.data_root / "verification" / serving_proof.CHECKPOINT_NAME


def test_more_than_10000_exports_since_the_proof_fail_closed(proven_lake):  # noqa: F811
    spec, con, digest, _ = proven_lake
    _bulk_export(con, 62, 10_000, "late")

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_rolling_checkpoints_carry_serving_past_the_threshold(
    proven_lake, advance  # noqa: F811
):
    spec, con, digest, _ = proven_lake
    snapshot = 62
    for round_ in range(3):
        assert advance() is not None
        snapshot = _bulk_export(con, snapshot, 4_000, f"round{round_}")

    # 12,060 exports since the proof, 4,000 since the last checkpoint.
    checked = serving_proof.check_proof(spec, con, digest)
    assert checked["exports"] == 12_060
    assert checked["snapshot"] == snapshot - 1
    assert checked["from_snapshot"] == 8_061


def test_checkpoint_still_rejects_a_tampered_receipt_after_it(
    proven_lake, advance  # noqa: F811
):
    spec, con, digest, export = proven_lake
    advance()
    export(100, "batch-tampered", digest="0" * 64)

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_checkpoint_still_rejects_a_non_export_snapshot_after_it(
    proven_lake, advance  # noqa: F811
):
    spec, con, digest, export = proven_lake
    advance()
    export(100, "batch-manual", author="operator")

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_tampered_chain_is_never_checkpointed(proven_lake, advance):  # noqa: F811
    spec, con, digest, export = proven_lake
    first = advance()
    before = _checkpoint_file(spec).read_bytes()
    export(100, "batch-tampered", digest="0" * 64)
    export(101, "batch-after")

    with pytest.raises(LakeError, match="lake_verification_required"):
        advance()

    assert _checkpoint_file(spec).read_bytes() == before
    assert json.loads(before)["snapshot"] == first["snapshot"] == 61


def test_checkpoint_is_not_written_without_the_writer_fence(
    proven_lake, advance  # noqa: F811
):
    spec, *_ = proven_lake

    def lost():
        raise LakeError("lake_fence_lost")

    with pytest.raises(LakeError, match="lake_fence_lost"):
        advance(SimpleNamespace(check=lost))

    assert not _checkpoint_file(spec).exists()
    assert not _checkpoint_file(spec).with_suffix(".pending").exists()


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE lake.snapshot_log SET commit_extra_info='0' WHERE snapshot_id=61",
        "DELETE FROM lake.snapshot_log WHERE snapshot_id=61",
    ],
)
def test_checkpointed_snapshot_that_changed_fails_closed(
    proven_lake, advance, change  # noqa: F811
):
    spec, con, digest, _ = proven_lake
    advance()
    con.execute(change)

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)


def test_checkpoint_for_another_proof_is_ignored(proven_lake, advance):  # noqa: F811
    # A stale checkpoint (from before a re-verification, or another lake) must
    # not skip anything: the check falls back to the whole chain.
    spec, con, digest, export = proven_lake
    record = advance()
    export(100, "batch-tampered", digest="0" * 64)
    # Everything matches snapshot 100 except the proof it is bound to.
    forged = {
        **record,
        "snapshot": 100,
        "commit": ["drover-export", "batch-tampered", "0" * 64],
    }
    _checkpoint_file(spec).write_text(json.dumps({**forged, "proof_sha256": "f" * 64}))

    with pytest.raises(LakeError, match="lake_verification_required"):
        serving_proof.check_proof(spec, con, digest)

    # Control: bound to this proof, the same record is honoured. A checkpoint
    # is trusted as the writer recorded it; see advance_checkpoint.
    _checkpoint_file(spec).write_text(json.dumps(forged))
    assert serving_proof.check_proof(spec, con, digest)["from_snapshot"] == 100


def test_malformed_checkpoint_falls_back_to_the_whole_chain(
    proven_lake,  # noqa: F811
):
    spec, con, digest, _ = proven_lake
    _checkpoint_file(spec).write_text("{not json")

    checked = serving_proof.check_proof(spec, con, digest)

    assert checked["from_snapshot"] == 1 and checked["exports"] == 60


def test_chain_does_not_depend_on_where_checkpoints_fell(
    proven_lake, advance  # noqa: F811
):
    spec, con, digest, export = proven_lake
    advance()
    for index in range(40):
        export(62 + index, f"tail-{index:02d}")
    rolled = advance()
    _checkpoint_file(spec).unlink()

    whole = serving_proof.check_proof(spec, con, digest)

    assert rolled["chain_sha256"] == whole["chain_sha256"]
    assert rolled["exports"] == whole["exports"] == 100


def test_no_new_exports_leaves_the_checkpoint_alone(proven_lake, advance):  # noqa: F811
    spec, *_ = proven_lake
    advance()
    before = _checkpoint_file(spec).read_bytes()

    assert advance() is None
    assert _checkpoint_file(spec).read_bytes() == before


def test_exporter_checkpoints_every_500_exports_and_survives_failures(
    monkeypatch,
):
    calls = []

    def fake(spec, digest, *, fence):
        calls.append((digest, fence))
        if len(calls) == 1:
            raise RuntimeError("catalog unreachable")
        return {"snapshot": 9, "exports": 8}

    monkeypatch.setattr(serving_proof, "advance_checkpoint", fake)
    config = SimpleNamespace(
        analytics=SimpleNamespace(
            verification_sha256="a" * 64,
            catalog_dsn_env="X",
            exporter_dsn_env="",
            data_root="/lake",
            extension_dir="/ext",
            engine_sha256="b" * 64,
        )
    )
    lifecycle = ExporterLifecycle(config)
    exporter = SimpleNamespace(fence=HELD)

    lifecycle._checkpoint(exporter)  # failure is logged, not raised
    lifecycle._checkpoint(exporter)

    assert calls == [("a" * 64, HELD), ("a" * 64, HELD)]
    assert lifecycle._exports_since_checkpoint == 0
    assert CHECKPOINT_EVERY_EXPORTS == 500
