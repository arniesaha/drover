"""Failure-path checks with all service operations mocked. No live changes."""

import importlib.util
import json
import plistlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
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


class ResumeTests(unittest.TestCase):
    def test_generated_postgres_plist_sets_utf8_locale(self):
        plist = plistlib.loads(plistlib.dumps(pg.pg_dict()))
        self.assertEqual(plist["EnvironmentVariables"]["LC_ALL"], "en_US.UTF-8")
        self.assertEqual(plist["EnvironmentVariables"]["LANG"], "en_US.UTF-8")

    def resume(
        self, stage, pg_loaded=True, hub_loaded=False, wrapped=False, tamper=False
    ):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            out = root / "pg-switch-test"
            out.mkdir()
            original = {
                "Label": "com.drover.server",
                "ProgramArguments": ["server", "--flag"],
                "EnvironmentVariables": {"TEST": "preserved"},
                "KeepAlive": True,
            }
            original_file = out / "server.original.plist"
            original_file.write_bytes(plistlib.dumps(original))
            hub = root / "hub.plist"
            patched = {
                **original,
                "ProgramArguments": [str(pg.WAIT), *original["ProgramArguments"]],
            }
            hub.write_bytes(plistlib.dumps(patched if wrapped else original))
            agent = root / "pg.plist"
            agent.write_bytes(plistlib.dumps(pg.pg_dict()))
            archive = out / "db.dump"
            archive.write_bytes(b"test dump")
            (out / "sha256.json").write_text(
                json.dumps({archive.name: pg.digest(archive)})
            )
            state = {
                "directory": str(out),
                "stage": stage,
                "original_pid": 123,
                "original_hub_sha256": pg.digest(original_file),
                "postgres_sha256": pg.digest(agent),
                "config_hashes": {},
                "downtime_start_epoch": 1,
            }
            active = root / "active.json"
            active.write_text(json.dumps(state))
            if tamper:
                archive.write_bytes(b"tampered")
            stack.enter_context(
                patch.multiple(pg, ROOT=root, HUB=hub, PG=agent, ACTIVE=active)
            )
            stack.enter_context(patch.object(pg, "validate"))
            stack.enter_context(patch.object(pg.os.path, "samefile", return_value=True))
            preflight = stack.enter_context(patch.object(pg, "preflight"))
            backup = stack.enter_context(patch.object(pg, "backup"))
            stop = stack.enter_context(patch.object(pg, "stop_hub"))
            stack.enter_context(patch.object(pg, "alive", return_value=False))
            stack.enter_context(patch.object(pg, "tcp_open", return_value=False))
            stack.enter_context(patch.object(pg, "pg_ready", return_value=True))
            stack.enter_context(patch.object(pg, "http_ready", return_value=True))
            stack.enter_context(patch.object(pg, "soak_twice"))
            jobs = {
                "com.drover.postgres": {"PID": 456} if pg_loaded else None,
                "com.drover.server": {"PID": 789} if hub_loaded else None,
            }
            stack.enter_context(
                patch.object(pg, "loaded", side_effect=lambda label: jobs[label])
            )
            stack.enter_context(
                patch.object(
                    pg,
                    "live_pid",
                    side_effect=lambda: 456 if jobs["com.drover.postgres"] else None,
                )
            )

            def command(args, **kwargs):
                if args[:2] == ["launchctl", "bootstrap"]:
                    label = (
                        "com.drover.postgres"
                        if args[-1] == agent
                        else "com.drover.server"
                    )
                    jobs[label] = {"PID": 456 if label.endswith("postgres") else 789}

            run = stack.enter_context(patch.object(pg, "run", side_effect=command))
            if tamper or stage in ("rolling-back", "rolled-back"):
                with self.assertRaises(RuntimeError):
                    pg.switch()
                run.assert_not_called()
                stop.assert_not_called()
                self.assertEqual(json.loads(active.read_text())["stage"], stage)
                return
            pg.switch()
            self.assertEqual(json.loads(active.read_text())["stage"], "complete")
            self.assertEqual(plistlib.loads(hub.read_bytes()), patched)
            stop.assert_not_called()
            backup.assert_not_called()
            preflight.assert_not_called()
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                sum(
                    c[:2] == ["launchctl", "bootstrap"] and c[-1] == agent
                    for c in commands
                ),
                int(not pg_loaded),
            )
            self.assertEqual(
                sum(
                    c[:2] == ["launchctl", "bootstrap"] and c[-1] == hub
                    for c in commands
                ),
                int(not hub_loaded),
            )
            self.assertEqual(original_file.read_bytes(), plistlib.dumps(original))
            # A repeated completed switch is a no-op and retains rollback evidence.
            run.reset_mock()
            pg.switch()
            run.assert_not_called()

    def test_resume_starting_supervised_postgres_before_and_after_bootstrap(self):
        for loaded in (False, True):
            with self.subTest(loaded=loaded):
                self.resume("starting-supervised-postgres", pg_loaded=loaded)

    def test_resume_applying_hub_wrapper_before_and_after_atomic_write(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                self.resume("applying-hub-wrapper", wrapped=wrapped)

    def test_resume_starting_hub_skips_successful_bootstrap(self):
        self.resume("starting-hub", hub_loaded=True, wrapped=True)

    def test_resume_rejects_tampered_backup_before_service_actions(self):
        self.resume("starting-supervised-postgres", tamper=True)

    def test_switch_cannot_resume_a_rollback(self):
        for stage in ("rolling-back", "rolled-back"):
            with self.subTest(stage=stage):
                self.resume(stage)


if __name__ == "__main__":
    unittest.main()
