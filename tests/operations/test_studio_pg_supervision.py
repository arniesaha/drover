"""Failure-path checks with all service operations mocked. No live changes."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "supervision",
    Path(__file__).resolve().parents[2] / "scripts/operations/studio_pg_supervision.py",
)
pg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pg)


class FailureSafety(unittest.TestCase):
    def test_failed_preflight_never_stops_services(self):
        with (
            patch.object(pg, "ACTIVE") as active,
            patch.object(pg, "preflight", side_effect=RuntimeError("not ready")),
            patch.object(pg, "stop_hub") as stop,
            patch.object(pg, "run") as run,
        ):
            active.exists.return_value = False
            with self.assertRaises(RuntimeError):
                pg.switch()
            stop.assert_not_called()
            run.assert_not_called()

    def test_failed_backup_preserves_hub_and_postgres(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hub = root / "hub.plist"
            hub.write_bytes(b"original")
            agent = root / "pg.plist"
            agent.write_bytes(b"agent")
            expected = root / "expected.json"
            expected.write_text('{"config_hashes": {}}')
            with (
                patch.multiple(
                    pg,
                    ROOT=root,
                    HUB=hub,
                    PG=agent,
                    ACTIVE=root / "active.json",
                    EXPECTED=expected,
                ),
                patch.object(pg, "preflight"),
                patch.object(pg, "live_pid", return_value=123),
                patch.object(pg, "backup", side_effect=RuntimeError("dump failed")),
                patch.object(pg, "stop_hub") as stop,
                patch.object(pg, "run") as run,
            ):
                with self.assertRaises(RuntimeError):
                    pg.switch()
                stop.assert_not_called()
                run.assert_not_called()
                self.assertEqual(hub.read_bytes(), b"original")
                self.assertEqual(
                    json.loads((root / "active.json").read_text())["stage"],
                    "backing-up",
                )

    def test_failed_checksum_never_stops_hub_on_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = root / "pg-switch-test"
            out.mkdir()
            original = out / "server.original.plist"
            original.write_bytes(b"tampered")
            active = root / "active.json"
            active.write_text(
                json.dumps({"directory": str(out), "original_hub_sha256": "bad"})
            )
            with (
                patch.multiple(pg, ROOT=root, ACTIVE=active),
                patch.object(pg, "stop_hub") as stop,
                patch.object(pg, "run") as run,
            ):
                with self.assertRaises(RuntimeError):
                    pg.rollback(True)
                stop.assert_not_called()
                run.assert_not_called()

    def test_backup_only_attempt_rollback_does_not_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = root / "pg-switch-test"
            out.mkdir()
            original = out / "server.original.plist"
            original.write_bytes(b"original")
            active = root / "active.json"
            active.write_text(
                json.dumps(
                    {
                        "directory": str(out),
                        "original_hub_sha256": pg.digest(original),
                        "stage": "backing-up",
                        "config_hashes": {},
                    }
                )
            )
            with (
                patch.multiple(pg, ROOT=root, ACTIVE=active),
                patch.object(pg.os.path, "samefile", return_value=True),
                patch.object(pg, "stop_hub") as stop,
                patch.object(pg, "run") as run,
            ):
                pg.rollback(True)
                stop.assert_not_called()
                run.assert_not_called()
                self.assertEqual(json.loads(active.read_text())["stage"], "rolled-back")


if __name__ == "__main__":
    unittest.main()
