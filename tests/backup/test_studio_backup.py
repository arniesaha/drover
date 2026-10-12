import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/backup/nas_snapshot.py"
spec = importlib.util.spec_from_file_location("nas_snapshot", HELPER)
nas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nas)


class BackupTests(unittest.TestCase):
    def remote(self, action, root, payload):
        return subprocess.run([sys.executable, str(HELPER), action, str(root)],
                              input=json.dumps(payload), text=True, capture_output=True)

    def test_retention_spans_year_and_keeps_seven_daily_four_weekly(self):
        import datetime as dt
        dates = [(dt.date(2026, 1, 9) - dt.timedelta(days=i)).isoformat() for i in range(50)]
        keep = nas.retained_dates(dates + [".pending-2026-01-10", "unrelated"])
        self.assertTrue(set(dates[:7]) <= keep)
        self.assertEqual(len({dt.date.fromisoformat(n).isocalendar()[:2] for n in keep}), 4)
        self.assertEqual(keep, set(dates[:7] + ["2025-12-28", "2025-12-21"]))

    def test_verify_publish_and_detect_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.remote("prepare", root, {"date": "2026-10-12"}).returncode, 0)
            pending = root / ".pending-2026-10-12"
            artifact = pending / "sample.dump"
            artifact.write_bytes(b"test archive")
            artifact.chmod(0o600)
            payload = {"date": "2026-10-12", "files": [
                {"path": artifact.name, "size": artifact.stat().st_size, "sha256": nas.digest(artifact)}]}
            artifact.write_bytes(b"bad archive!")
            self.assertNotEqual(self.remote("verify", root, payload).returncode, 0)
            self.assertFalse((pending / "MANIFEST.json").exists())
            artifact.write_bytes(b"test archive")
            self.assertEqual(self.remote("verify", root, payload).returncode, 0)
            self.assertNotEqual(self.remote("publish", root, {"date": "2026-10-12", "nas_pg_restore_list": False}).returncode, 0)
            self.assertEqual(self.remote("publish", root, {"date": "2026-10-12", "nas_pg_restore_list": True}).returncode, 0)
            self.assertTrue((root / "2026-10-12/SUCCESS.json").exists())
            self.assertNotEqual(self.remote("prepare", root, {"date": "2026-10-12"}).returncode, 0)

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
            entry = {"path": "file", "size": artifact.stat().st_size, "sha256": nas.digest(artifact)}
            self.assertNotEqual(self.remote("verify", root, {"date": "2026-10-12", "files": [entry]}).returncode, 0)
            entry["path"] = "../file"
            self.assertNotEqual(self.remote("verify", root, {"date": "2026-10-12", "files": [entry]}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
