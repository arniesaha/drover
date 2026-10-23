import importlib.util
import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/backup/nas_snapshot.py"
spec = importlib.util.spec_from_file_location("nas_snapshot", HELPER)
nas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nas)


class BackupTests(unittest.TestCase):
    def remote(self, action, root, payload):
        return subprocess.run(
            [sys.executable, str(HELPER), action, str(root)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )

    def test_retention_spans_year_and_keeps_seven_daily_four_weekly(self):
        import datetime as dt

        dates = [
            (dt.date(2026, 1, 9) - dt.timedelta(days=i)).isoformat() for i in range(50)
        ]
        keep = nas.retained_dates(dates + [".pending-2026-01-10", "unrelated"])
        self.assertTrue(set(dates[:7]) <= keep)
        self.assertEqual(
            len({dt.date.fromisoformat(n).isocalendar()[:2] for n in keep}), 4
        )
        self.assertEqual(keep, set(dates[:7] + ["2025-12-28", "2025-12-21"]))

    def test_verify_publish_and_detect_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                self.remote("prepare", root, {"date": "2026-10-12"}).returncode, 0
            )
            pending = root / ".pending-2026-10-12"
            artifact = pending / "sample.dump"
            artifact.write_bytes(b"test archive")
            artifact.chmod(0o600)
            payload = {
                "date": "2026-10-12",
                "files": [
                    {
                        "path": artifact.name,
                        "size": artifact.stat().st_size,
                        "sha256": nas.digest(artifact),
                    }
                ],
            }
            artifact.write_bytes(b"bad archive!")
            result = self.remote("verify", root, payload)
            self.assertFalse(json.loads(result.stdout)["verification_ok"])
            self.assertEqual(
                json.loads(result.stdout)["failures"],
                [{"path": "sample.dump", "reason": "checksum mismatch"}],
            )
            self.assertTrue((pending / "MANIFEST.json").exists())
            self.assertNotEqual(
                self.remote(
                    "publish", root, {"date": "2026-10-12", "nas_pg_restore_list": True}
                ).returncode,
                0,
            )
            artifact.write_bytes(b"test archive")
            self.assertEqual(self.remote("verify", root, payload).returncode, 0)
            self.assertNotEqual(
                self.remote(
                    "publish",
                    root,
                    {"date": "2026-10-12", "nas_pg_restore_list": False},
                ).returncode,
                0,
            )
            self.assertEqual(
                self.remote(
                    "publish", root, {"date": "2026-10-12", "nas_pg_restore_list": True}
                ).returncode,
                0,
            )
            self.assertTrue((root / "2026-10-12/SUCCESS.json").exists())
            self.assertNotEqual(
                self.remote("prepare", root, {"date": "2026-10-12"}).returncode, 0
            )

    def test_unique_cutover_generation_preserves_daily_and_rejects_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daily = "2026-10-22"
            cutover = daily + "-cutover-a1b2c3"
            for generation in (daily, cutover):
                self.assertEqual(
                    self.remote("prepare", root, {"generation_id": generation}).returncode,
                    0,
                )
                pending = root / (".pending-" + generation)
                artifact = pending / "sample.dump"
                artifact.write_bytes(generation.encode())
                artifact.chmod(0o600)
                payload = {
                    "generation_id": generation,
                    "files": [
                        {
                            "path": artifact.name,
                            "size": artifact.stat().st_size,
                            "sha256": nas.digest(artifact),
                        }
                    ],
                    "nas_pg_restore_list": True,
                }
                self.assertTrue(
                    json.loads(self.remote("verify", root, payload).stdout)[
                        "verification_ok"
                    ]
                )
                self.assertEqual(self.remote("publish", root, payload).returncode, 0)
            self.assertTrue((root / daily / "SUCCESS.json").is_file())
            self.assertTrue((root / cutover / "SUCCESS.json").is_file())
            self.assertEqual(nas.retained_dates([daily, cutover]), {daily})
            self.assertNotEqual(
                self.remote(
                    "prepare", root, {"generation_id": daily + "-../../escape"}
                ).returncode,
                0,
            )

    def test_rsync_uses_hardlinks_private_modes_and_preserves_previous(self):
        backup_path = ROOT / "scripts/backup/studio_backup.py"
        spec = importlib.util.spec_from_file_location("studio_backup", backup_path)
        backup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backup)
        if not Path(backup.RSYNC).is_file():
            self.skipTest("Studio GNU rsync is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "file").write_text("first generation")
            (source / "nested").mkdir()
            (source / "nested/key").write_text("placeholder")
            backup.sync(source, str(root / "one"))
            backup.sync(source, str(root / "two"), str(root / "one"))
            one, two = root / "one/file", root / "two/file"
            self.assertEqual(one.stat().st_ino, two.stat().st_ino)
            self.assertEqual(two.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "two/nested").stat().st_mode & 0o777, 0o700)
            (source / "file").write_text("changed second generation")
            backup.sync(source, str(root / "two"), str(root / "one"))
            self.assertEqual(one.read_text(), "first generation")
            self.assertEqual(two.read_text(), "changed second generation")
            self.assertNotEqual(one.stat().st_ino, two.stat().st_ino)

    def test_verify_rejects_world_readable_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.remote("prepare", root, {"date": "2026-10-12"})
            artifact = root / ".pending-2026-10-12/file"
            artifact.write_bytes(b"secret placeholder")
            artifact.chmod(0o644)
            entry = {
                "path": "file",
                "size": artifact.stat().st_size,
                "sha256": nas.digest(artifact),
            }
            result = self.remote(
                "verify", root, {"date": "2026-10-12", "files": [entry]}
            )
            self.assertFalse(json.loads(result.stdout)["verification_ok"])

            entry["path"] = "../file"
            self.assertNotEqual(
                self.remote(
                    "verify", root, {"date": "2026-10-12", "files": [entry]}
                ).returncode,
                0,
            )

    def test_failed_generation_retained_and_same_date_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            date = "2026-10-13"
            self.remote("prepare", root, {"date": date})
            (root / (".pending-" + date) / "evidence").write_text("placeholder")
            failed = json.loads(self.remote("fail", root, {"date": date}).stdout)[
                "failed"
            ]
            self.assertTrue(failed.startswith(".failed-2026-10-13-"))
            self.assertEqual((root / failed / "evidence").read_text(), "placeholder")
            self.assertEqual(self.remote("prepare", root, {"date": date}).returncode, 0)
            # Also recover a run interrupted before its exception handler ran.
            result = self.remote("prepare", root, {"date": date})
            self.assertEqual(result.returncode, 0)
            self.assertNotEqual(json.loads(result.stdout)["failed"], failed)
            self.assertTrue((root / failed).exists())

    def test_inventory_frozen_copy_survives_source_change_and_detects_nas_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            date = "2026-10-13"
            self.remote("prepare", root, {"date": date})
            pending = root / (".pending-" + date)
            tree = pending / "drover-data"
            tree.mkdir()
            source = root / "live-file"
            source.write_bytes(b"before")
            artifact = tree / "export.parquet"
            artifact.write_bytes(source.read_bytes())
            artifact.chmod(0o600)
            source.write_bytes(b"after!")
            entries = nas.inventory(pending, ["drover-data"])
            request = {"date": date, "files": entries}
            self.assertTrue(nas.verify(pending, request)["verification_ok"])
            # Same-size mutation after inventory must still fail.
            artifact.write_bytes(b"after!")
            result = nas.verify(pending, request)
            self.assertEqual(
                result["failures"],
                [{"path": "drover-data/export.parquet", "reason": "checksum mismatch"}],
            )
            self.assertEqual(
                json.loads((pending / "MANIFEST.json").read_text()), request
            )

    def test_verify_reports_all_paths_and_change_during_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary)
            artifact = pending / "file"
            artifact.write_bytes(b"before")
            artifact.chmod(0o600)
            request = {
                "files": [
                    {"path": "file", "size": 6, "sha256": nas.digest(artifact)},
                    {"path": "missing", "size": 1, "sha256": "unused"},
                ]
            }
            digest = nas.digest

            def mutate(path):
                value = digest(path)
                path.write_bytes(b"after!")
                return value

            with mock.patch.object(nas, "digest", side_effect=mutate):
                result = nas.verify(pending, request)
            self.assertEqual(
                result["failures"],
                [
                    {"path": "file", "reason": "artifact changed during verification"},
                    {"path": "missing", "reason": "missing artifact"},
                ],
            )
            with mock.patch.object(nas, "digest", side_effect=mutate):
                with self.assertRaisesRegex(ValueError, "changed during inventory"):
                    tree = pending / "drover-data"
                    tree.mkdir()
                    artifact.rename(tree / "file")
                    nas.inventory(pending, ["drover-data"])

    def test_manifest_rejects_symlink_ancestor_and_invalid_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / "pending"
            pending.mkdir()
            (pending / "escape").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink manifest path"):
                nas.verify(pending, {"files": [{"path": "escape/file"}]})
            for date in ("../escape", "2026-10-13/extra", "2026-99-13"):
                self.assertNotEqual(
                    self.remote("prepare", root, {"date": date}).returncode, 0
                )

    def test_fail_quarantines_old_pending_and_duplicate_failed_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for date in ("2026-10-13", "2026-10-14"):
                pending = root / (".pending-" + date)
                pending.mkdir()
                (pending / "evidence").write_text(date)
            duplicate = root / ".failed-2026-10-14-123"
            duplicate.mkdir()
            (duplicate / "evidence").write_text("original")
            with mock.patch.object(nas.time, "time_ns", return_value=123):
                older = nas.quarantine_pending(root, "2026-10-14")
                failed = nas.preserve_failed(root, "2026-10-14")
            self.assertEqual(failed, ".failed-2026-10-14-123-1")
            self.assertEqual((root / failed / "evidence").read_text(), "2026-10-14")
            self.assertEqual((duplicate / "evidence").read_text(), "original")
            self.assertEqual((root / older[0] / "evidence").read_text(), "2026-10-13")
            self.assertIsNone(nas.preserve_failed(root, "2026-10-14"))

    def test_prepare_quarantines_prior_dates_and_keeps_last_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "2026-10-12"
            good.mkdir()
            (good / "SUCCESS.json").write_text("{}")
            for date in ("2026-10-13", "2026-10-14", "2026-10-15"):
                (root / (".pending-" + date)).mkdir()
            result = self.remote("prepare", root, {"date": "2026-10-14"})
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["previous"], "2026-10-12")
            self.assertEqual(len(receipt["quarantined"]), 1)
            self.assertFalse((root / ".pending-2026-10-13").exists())
            self.assertTrue((root / receipt["failed"]).is_dir())
            self.assertTrue((root / ".pending-2026-10-15").is_dir())
            result = self.remote("fail", root, {"date": "2026-10-15"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["quarantined"]), 1)
            self.assertFalse(list(root.glob(".pending-*")))
            self.assertTrue(good.is_dir())

    def test_fail_repairs_parent_permissions_without_traversing_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pending = root / ".pending-2026-10-14"
            pending.mkdir()
            (pending / "evidence").write_text("placeholder")
            pending.chmod(0o000)
            root.chmod(0o500)
            try:
                failed = nas.preserve_failed(root, "2026-10-14")
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                preserved = root / failed
                self.assertEqual(preserved.stat().st_mode & 0o777, 0)
                preserved.chmod(0o700)
                self.assertEqual((preserved / "evidence").read_text(), "placeholder")
            finally:
                root.chmod(0o700)
                for path in root.iterdir():
                    path.chmod(0o700)


class LiveBackupTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "studio_backup", ROOT / "scripts/backup/studio_backup.py"
        )
        self.backup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.backup)

    def test_nas_pulls_from_studio_with_link_dest_excludes_and_quoted_paths(self):
        backup = self.backup
        target = "backup-user@nas-host"
        destination = "/srv/backups/drover/Studio/.pending-2026-10-12/data's files"
        previous = "/srv/backups/drover/Studio/2026-10-11/data's files"
        source = Path("/Volumes/M2 1/drover-data")
        with (
            mock.patch.object(backup, "NAS", target),
            mock.patch.object(backup, "run") as run,
        ):
            backup.sync(source, target + ":" + destination, previous, ["*.sock"])
        pull = [
            "rsync",
            "-a",
            "--no-owner",
            "--no-group",
            "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
            "--rsync-path=" + backup.RSYNC,
            "--link-dest=" + previous,
            "--exclude=*.sock",
            "drover-studio:" + str(source) + "/",
            destination + "/",
        ]
        command = run.call_args.args[0][-1]
        self.assertIn(shlex.join(pull), command)
        self.assertIn("mkdir -p " + shlex.quote(destination), command)
        self.assertIn("chmod -R go= " + shlex.quote(destination), command)
        self.assertIn("case $status in 0)", command)
        self.assertEqual(run.call_args.kwargs, {"operation": "nas_pull"})

    def test_pull_chmod_removes_nas_acl_modes_and_pull_failure_propagates(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_rsync = root / "rsync"
            fake_rsync.write_text(
                "#!/bin/sh\n"
                "for destination do :; done\n"
                'printf placeholder > "$destination/file"\n'
                'chmod 777 "$destination" "$destination/file"\n'
            )
            fake_rsync.chmod(0o700)
            destination = root / "generation's files"
            real_run = backup.run

            def run_nas_command(args, **kwargs):
                # Execute only the generated NAS shell command in a private local
                # fixture, with a fake receiver that reproduces the NAS ACL modes.
                environment = {
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                }
                result = subprocess.run(
                    ["/bin/sh", "-c", args[-1]],
                    env=environment,
                    capture_output=True,
                )
                if result.returncode:
                    raise RuntimeError("command failed (exit %d)" % result.returncode)
                return result.stdout

            with (
                mock.patch.object(backup, "NAS", "backup-user@nas-host"),
                mock.patch.object(backup, "run", side_effect=run_nas_command),
            ):
                backup.sync(
                    Path("/Volumes/M2 1/drover-data"),
                    "backup-user@nas-host:" + str(destination),
                )
                self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
                self.assertEqual((destination / "file").stat().st_mode & 0o777, 0o700)
                # chmod strips only group/other bits, preserving the owner's mode.
                fake_rsync.write_text("#!/bin/sh\nexit 23\n")
                with self.assertRaisesRegex(
                    RuntimeError, r"command failed \(exit 23\)"
                ):
                    backup.sync(
                        Path("/Volumes/M2 1/drover-data"),
                        "backup-user@nas-host:" + str(destination),
                    )
            # The execution fixture did not replace the production wrapper.
            self.assertIs(backup.run, real_run)

    def test_missing_target_root_fails_before_contacting_nas(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".drover/maintenance/backup-config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"nas": "backup-user@nas-host"}))
            with (
                mock.patch.object(backup, "HOME", home),
                mock.patch.object(backup, "NAS", ""),
                mock.patch.object(backup, "DATA", {}),
                mock.patch.object(
                    backup.socket, "gethostname", return_value="Studio test.local"
                ),
                mock.patch.object(backup, "log"),
                mock.patch.object(
                    backup, "remote", side_effect=RuntimeError("stop at preflight")
                ) as remote,
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid NAS backup root"):
                    backup.backup()
                remote.assert_not_called()

    def test_configurable_target_root_and_invalid_roots(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".drover/maintenance/backup-config.json"
            config.parent.mkdir(parents=True)
            for target in ("/volume/backups", "relative", "/home/../escape", 42):
                config.write_text(
                    json.dumps({"nas": "backup-user@nas-host", "target_root": target})
                )
                with (
                    mock.patch.dict(
                        backup.os.environ,
                        {backup.RECEIPT_KEY_ENV: "fixture-key"},
                    ),
                    mock.patch.object(backup, "HOME", home),
                    mock.patch.object(backup, "NAS", ""),
                    mock.patch.object(backup, "DATA", {}),
                    mock.patch.object(
                        backup.socket, "gethostname", return_value="backup host.local"
                    ),
                    mock.patch.object(backup, "log"),
                    mock.patch.object(
                        backup, "remote", side_effect=RuntimeError("preflight")
                    ) as remote,
                ):
                    with self.assertRaises(RuntimeError):
                        backup.backup()
                    if target == "/volume/backups":
                        self.assertEqual(
                            remote.call_args.args[2],
                            "/volume/backups/backup_host.local",
                        )
                    else:
                        remote.assert_not_called()

    def test_failure_log_includes_command_exit_code_and_caffeinate_tracks_pid(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.CompletedProcess(
                ["rsync"], 23, stdout=b"", stderr=b"private diagnostic placeholder"
            )
            with (
                mock.patch.object(backup, "STATE", root / "state"),
                mock.patch.object(backup, "LOG", root / "backup.log"),
                mock.patch.object(backup.os, "umask"),
                mock.patch.object(backup.subprocess, "Popen") as caffeinate,
                mock.patch.object(backup.subprocess, "run", return_value=result),
                mock.patch.object(
                    backup, "backup", side_effect=lambda: backup.run(["rsync"])
                ),
                mock.patch.object(backup, "log") as log,
            ):
                self.assertEqual(backup.main(), 1)
                caffeinate.assert_called_once_with(
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
                )
                log.assert_called_once_with(
                    'FAILURE type=CommandFailure {"operation": "rsync", "exit_code": 23, '
                    '"stderr_tail": "private diagnostic placeholder"}; '
                    "generation unpublished; inspect NAS failed or pending directory"
                )

    def test_failure_log_omits_arbitrary_exception_contents(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(backup, "STATE", root / "state"),
                mock.patch.object(backup, "LOG", root / "backup.log"),
                mock.patch.object(backup.os, "umask"),
                mock.patch.object(backup.subprocess, "Popen"),
                mock.patch.object(
                    backup,
                    "backup",
                    side_effect=ValueError("command failed (exit private placeholder)"),
                ),
                mock.patch.object(backup, "log") as log,
            ):
                self.assertEqual(backup.main(), 1)
                log.assert_called_once_with(
                    "FAILURE type=ValueError; generation unpublished; inspect NAS failed or pending directory"
                )

    def test_failure_diagnostics_redact_before_bounding_and_escape_newlines(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stderr = (
                b"x" * 3000
                + b'\nrsync: open "/private/tree/data file": Permission denied\n'
                b"password=credential token='credential with spaces'\n"
                b"postgres://user:credential@host/db /private/config.json\n"
                b"Authorization: Bearer credential\n"
            )
            result = subprocess.CompletedProcess(["rsync"], 23, b"", stderr)
            with (
                mock.patch.object(backup, "STATE", root / "state"),
                mock.patch.object(backup, "LOG", root / "backup.log"),
                mock.patch.object(backup.subprocess, "Popen"),
                mock.patch.object(backup.subprocess, "run", return_value=result),
                mock.patch.object(
                    backup, "backup", side_effect=lambda: backup.run(["rsync"])
                ),
            ):
                self.assertEqual(backup.main(), 1)
            line = (root / "backup.log").read_text()
            self.assertEqual(len(line.splitlines()), 1)
            self.assertIn('"exit_code": 23', line)
            self.assertIn("Permission denied", line)
            for private in ("credential", "/private", "data file", "user:"):
                self.assertNotIn(private, line)
            diagnostic = backup.redact_diagnostic(stderr)
            self.assertLessEqual(len(diagnostic.encode()), 2048)

    def test_ssh_transport_retries_once_and_resets_readback(self):
        backup = self.backup
        failed = subprocess.CompletedProcess([], 255, b"", b"Connection reset by peer")
        ok = subprocess.CompletedProcess([], 0, b"ok", b"")
        with tempfile.TemporaryFile() as output:

            def attempt(*args, **kwargs):
                output.write(b"partial" if attempt.count == 0 else b"complete")
                attempt.count += 1
                return failed if attempt.count == 1 else ok

            attempt.count = 0
            with (
                mock.patch.object(backup.subprocess, "run", side_effect=attempt) as run,
                mock.patch.object(backup, "log"),
            ):
                backup.run(backup.SSH + ["fixture", "cat"], output=output)
            self.assertEqual(run.call_count, 2)
            output.seek(0)
            self.assertEqual(output.read(), b"complete")
        for code, stderr, expected in (
            (255, b"Connection reset by peer", 2),
            (12, b"ssh: Connection reset by peer", 2),
            (255, b"Permission denied (publickey)", 1),
            (1, b"Permission denied", 1),
        ):
            result = subprocess.CompletedProcess([], code, b"", stderr)
            with (
                mock.patch.object(backup.subprocess, "run", return_value=result) as run,
                mock.patch.object(backup, "log"),
            ):
                with self.assertRaises(backup.CommandFailure):
                    backup.run(backup.SSH + ["fixture", "command"])
                self.assertEqual(run.call_count, expected)

    def test_second_pass_tolerates_only_rsync_24_and_still_chmods(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receiver = root / "rsync"
            environment = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
            }

            def execute(args, **kwargs):
                result = subprocess.run(
                    ["/bin/sh", "-c", args[-1]], env=environment, capture_output=True
                )
                if result.returncode:
                    raise backup.CommandFailure(kwargs["operation"], result)
                return result.stdout

            with (
                mock.patch.object(backup, "NAS", "fixture"),
                mock.patch.object(backup, "run", side_effect=execute),
            ):
                for code in (0, 24, 23):
                    receiver.write_text(
                        "#!/bin/sh\nfor destination do :; done\n"
                        'touch "$destination/file"\nchmod 777 "$destination/file"\n'
                        + "exit %d\n" % code
                    )
                    receiver.chmod(0o700)
                    for second_pass in (False, True):
                        dest = root / ("copy-%d-%s" % (code, second_pass))
                        if code == 0 or (code == 24 and second_pass):
                            backup.sync(
                                root, "fixture:" + str(dest), second_pass=second_pass
                            )
                            self.assertEqual((dest / "file").stat().st_mode & 0o077, 0)
                        else:
                            with self.assertRaises(backup.CommandFailure) as caught:
                                backup.sync(
                                    root,
                                    "fixture:" + str(dest),
                                    second_pass=second_pass,
                                )
                            self.assertEqual(caught.exception.returncode, code)
                            self.assertIn("operation=rsync", caught.exception.stderr)
                # A chmod failure must not be tolerated as a vanished source.
                chmod = root / "chmod"
                chmod.write_text("#!/bin/sh\necho 'Permission denied' >&2\nexit 1\n")
                chmod.chmod(0o700)
                receiver.write_text("#!/bin/sh\nexit 24\n")
                with self.assertRaises(backup.CommandFailure) as caught:
                    backup.sync(
                        root, "fixture:" + str(root / "chmod-failure"), second_pass=True
                    )
                self.assertEqual(caught.exception.returncode, 1)
                self.assertIn("operation=chmod", caught.exception.stderr)
        result = subprocess.CompletedProcess([], 24, b"", b"vanished")
        with mock.patch.object(backup.subprocess, "run", return_value=result):
            with self.assertRaises(backup.CommandFailure):
                backup.sync(root, str(root / "local"))
            backup.sync(root, str(root / "local"), second_pass=True)

    def test_launchagent_uses_stable_dedicated_python(self):
        template = ROOT / "scripts/launchd/com.drover.backup.plist.template"
        plist = plistlib.loads(template.read_bytes())
        self.assertEqual(
            plist["ProgramArguments"][0],
            "__HOME__/.drover/maintenance/backup-python/bin/python",
        )

    def test_lake_exclusions_remove_live_stores_locks_and_snapshot_scratch(self):
        backup = self.backup
        if not Path(backup.RSYNC).is_file():
            self.skipTest("Studio GNU rsync is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            excluded = [
                "registry.duckdb",
                "registry.duckdb.wal",
                "writer.lock",
                "writer.lck",
                "writer.wal",
                "nested/store.duckdb",
                ".drover-snapshots/copy",
                "nested/.drover-backup-snapshots/copy",
            ]
            included = [
                "uv.lock",
                "nested/uv.lock",
                "nested/data.parquet",
                "export.json",
            ]
            for name in excluded + included:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("placeholder")
            backup.sync(source, str(root / "copy"), excludes=backup.LAKE_EXCLUDES)
            for name in excluded:
                self.assertFalse((root / "copy" / name).exists(), name)
            for name in included:
                self.assertTrue((root / "copy" / name).exists(), name)

    def test_duckdb_snapshot_retries_wal_change_and_checkpoints_private_clone(self):
        import duckdb

        backup = self.backup
        if sys.platform != "darwin":
            self.skipTest("atomic clone requires macOS")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "registry.duckdb"
            destination = root / "stage" / "registry.duckdb"
            with duckdb.connect(str(source)) as writer:
                writer.execute("CREATE TABLE sample(value INTEGER)")
                writer.execute("CHECKPOINT")
                writer.execute("INSERT INTO sample VALUES (1)")
                wal = Path(str(source) + ".wal")
                self.assertTrue(wal.is_file())
                clone = backup.clone_file
                clones = []

                def mutate_after_clone(src, dst):
                    clone(src, dst)
                    clones.append(src.name)
                    if len(clones) == 1:
                        writer.execute("INSERT INTO sample VALUES (2)")

                with mock.patch.object(
                    backup, "clone_file", side_effect=mutate_after_clone
                ):
                    backup.snapshot_duckdb(source, destination)
                self.assertGreaterEqual(len(clones), 4)
                self.assertFalse(Path(str(destination) + ".wal").exists())
                self.assertTrue(wal.is_file())
                with duckdb.connect(str(destination), read_only=True) as reader:
                    self.assertEqual(
                        reader.execute(
                            "SELECT value FROM sample ORDER BY value"
                        ).fetchall(),
                        [(1,), (2,)],
                    )
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list((root / ".drover-backup-snapshots").iterdir()), [])

    def test_atomic_clone_unavailable_fails_without_streamed_fallback(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "registry.duckdb"
            source.write_bytes(b"placeholder")
            destination = Path(temporary) / "result.duckdb"
            with (
                mock.patch.object(
                    backup, "clone_file", side_effect=RuntimeError("unavailable")
                ),
                mock.patch.object(backup.shutil, "copyfile") as copy,
            ):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    backup.snapshot_duckdb(source, destination)
                copy.assert_not_called()
            self.assertFalse(destination.exists())

    def test_generation_failure_renames_pending_and_preserves_original_error(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".drover/maintenance/backup-config.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {"nas": "backup-user@nas-host", "target_root": "/srv/backups"}
                )
            )
            for rename_fails in (False, True):

                def remote(helper, action, root, payload):
                    if action == "prepare":
                        return {"previous": None}
                    if rename_fails:
                        raise RuntimeError("rename failure")
                    return {"failed": "fixture"}

                with (
                    mock.patch.dict(
                        backup.os.environ,
                        {backup.RECEIPT_KEY_ENV: "fixture-key"},
                    ),
                    mock.patch.object(backup, "HOME", home),
                    mock.patch.object(backup, "DATA", {}),
                    mock.patch.object(backup, "log"),
                    mock.patch.object(backup, "remote", side_effect=remote) as call,
                    mock.patch.object(
                        backup, "backup_generation", side_effect=ValueError("original")
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, "original"):
                        backup.backup()
                    self.assertEqual(
                        [args.args[1] for args in call.call_args_list],
                        ["prepare", "fail"],
                    )

    def test_mismatch_logs_all_relative_paths_without_child_diagnostics(self):
        backup = self.backup
        failures = [
            {"path": "drover-data/export.json", "reason": "checksum mismatch"},
            {"path": "drover-data/name\nwith-newline", "reason": "missing artifact"},
        ]
        with (
            mock.patch.object(
                backup,
                "remote",
                return_value={"verification_ok": False, "failures": failures},
            ),
            mock.patch.object(backup, "log") as log,
        ):
            with self.assertRaisesRegex(RuntimeError, "NAS verification failed"):
                backup.verify_generation("helper", "/fixture", "2026-10-13", [])
        self.assertEqual(
            [call.args[0] for call in log.call_args_list],
            ["NAS_verification_failure " + json.dumps(item) for item in failures],
        )
        self.assertTrue(all("\n" not in call.args[0] for call in log.call_args_list))

    def test_complete_generation_hashes_final_nas_copy(self):
        backup = self.backup
        if not Path(backup.RSYNC).is_file():
            self.skipTest("Studio GNU rsync is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "export.parquet").write_bytes(b"before")
            (source / "registry.duckdb").write_bytes(b"excluded")
            nas_root = root / "nas"
            state = root / "state"
            state.mkdir()
            date = "2026-10-13"
            nas.pending_path(nas_root, date).mkdir(parents=True)
            (root / ".drover").mkdir()
            (root / "Library/LaunchAgents").mkdir(parents=True)
            real_sync = backup.sync
            real_run = backup.run
            calls = []
            published = {}
            mutations = iter((b"second", b"third!"))

            def sync(src, dest, previous=None, excludes=(), **kwargs):
                calls.append((src, dest, excludes, kwargs))
                real_sync(
                    src,
                    dest.removeprefix("backup-user@nas-host:"),
                    excludes=excludes,
                    **kwargs,
                )
                if src == source:
                    # The source changes after each transfer, including final pass.
                    (source / "export.parquet").write_bytes(next(mutations))

            def run(args, *, output=None, **kwargs):
                if args[0] == backup.RSYNC:
                    return real_run(args, output=output, **kwargs)
                if args[0].endswith("psql"):
                    return b"fixture\n"
                if "-f" in args:
                    Path(args[args.index("-f") + 1]).write_bytes(b"dump placeholder")
                if output:
                    output.write(b"placeholder")
                return b""

            def remote(helper, action, path, payload):
                pending = nas.pending_path(Path(path), date)
                if action == "inventory":
                    return {"files": nas.inventory(pending, payload["trees"])}
                if action == "verify":
                    return nas.verify(pending, payload)
                if action == "publish":
                    published.update(payload)
                return {}

            with (
                mock.patch.object(backup, "HOME", root),
                mock.patch.object(backup, "STATE", state),
                mock.patch.object(backup, "NAS", "backup-user@nas-host"),
                mock.patch.object(backup, "DATA", {"drover-data": source}),
                mock.patch.object(backup, "sync", side_effect=sync),
                mock.patch.object(backup, "run", side_effect=run),
                mock.patch.object(backup, "snapshot_databases"),
                mock.patch.object(backup, "remote", side_effect=remote),
                mock.patch.object(backup, "log"),
            ):
                backup.backup_generation(
                    "helper",
                    str(nas_root),
                    date,
                    {"previous": None},
                    0,
                    "fixture",
                    b"fixture-key",
                )
            lake_calls = [call for call in calls if call[0] == source]
            self.assertEqual(len(lake_calls), 2)
            self.assertEqual(lake_calls[0][3], {})
            self.assertEqual(lake_calls[1][3], {"second_pass": True})
            self.assertTrue(all(call[2] == backup.LAKE_EXCLUDES for call in lake_calls))
            pending = nas.pending_path(nas_root, date)
            self.assertFalse((pending / "drover-data/registry.duckdb").exists())
            self.assertNotEqual(
                nas.digest(source / "export.parquet"),
                nas.digest(pending / "drover-data/export.parquet"),
            )
            self.assertTrue(
                json.loads((pending / "VERIFICATION.json").read_text())[
                    "verification_ok"
                ]
            )
            self.assertTrue(published["verification_ok"])
            self.assertEqual(len(published["artifact_manifest_sha256"]), 64)
            self.assertEqual(
                published["local_backup_manifest_sha256"],
                published["artifact_manifest_sha256"],
            )
            self.assertEqual(
                published["producer"]["name"], backup.RECEIPT_PRODUCER["name"]
            )
            self.assertEqual(len(published["producer"]["signature"]), 64)


if __name__ == "__main__":
    unittest.main()
