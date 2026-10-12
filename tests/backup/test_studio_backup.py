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
            self.assertNotEqual(self.remote("verify", root, payload).returncode, 0)
            self.assertFalse((pending / "MANIFEST.json").exists())
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
            self.assertNotEqual(
                self.remote(
                    "verify", root, {"date": "2026-10-12", "files": [entry]}
                ).returncode,
                0,
            )
            entry["path"] = "../file"
            self.assertNotEqual(
                self.remote(
                    "verify", root, {"date": "2026-10-12", "files": [entry]}
                ).returncode,
                0,
            )


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
        destination = (
            "/home/Arnab/backups/drover/Studio/.pending-2026-10-12/data's files"
        )
        previous = "/home/Arnab/backups/drover/Studio/2026-10-11/data's files"
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
        command = (
            "umask 077; mkdir -p "
            + shlex.quote(destination)
            + " && "
            + shlex.join(pull)
            + " && chmod -R go= "
            + shlex.quote(destination)
        )
        run.assert_called_once_with(backup.SSH + [target, command])

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

            def run_nas_command(args):
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

    def test_default_root_uses_nas_home_and_sanitized_host(self):
        backup = self.backup
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".drover/maintenance/backup-config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"nas": "Arnab@nas-host"}))
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
                with self.assertRaisesRegex(RuntimeError, "stop at preflight"):
                    backup.backup()
                self.assertEqual(remote.call_args.args[1], "prepare")
                self.assertEqual(
                    remote.call_args.args[2],
                    "/home/Arnab/backups/drover/Studio_test.local",
                )

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
                    "FAILURE type=RuntimeError (command failed (exit 23)); "
                    "generation unpublished; inspect NAS pending directory"
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
                    backup, "backup", side_effect=ValueError("private placeholder")
                ),
                mock.patch.object(backup, "log") as log,
            ):
                self.assertEqual(backup.main(), 1)
                log.assert_called_once_with(
                    "FAILURE type=ValueError; generation unpublished; inspect NAS pending directory"
                )

    def test_launchagent_uses_drover_runtime_python(self):
        template = ROOT / "scripts/launchd/com.drover.backup.plist.template"
        plist = plistlib.loads(template.read_bytes())
        self.assertEqual(
            plist["ProgramArguments"][0],
            "__HOME__/.drover/runtime/current/bin/python",
        )


if __name__ == "__main__":
    unittest.main()
