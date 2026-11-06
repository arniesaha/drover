"""Local writer shutdown contracts; no live services or database connections."""

import ast
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from drover.server.watcher import IncomingWatcher


class HeldObserver(threading.Thread):
    def __init__(self, release):
        super().__init__()
        self.release = release
        self.entered = threading.Event()

    def run(self):
        self.entered.set()
        self.release.wait(2)

    def stop(self):
        pass


class WatcherShutdownTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="watcher-shutdown-test-"))
        self.watcher = IncomingWatcher(
            incoming_dir=self.root,
            parquet_dir=self.root / "parquet",
            duckdb_path=self.root / "unused-db",
        )

    def test_observer_timeout_preserves_ownership_and_prevents_close(self):
        release = threading.Event()
        observer = HeldObserver(release)
        self.watcher._observer = observer
        observer.start()
        self.assertTrue(observer.entered.wait(1))
        closed = []
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "observer"):
                self.watcher.stop(timeout=0.02)
                closed.append(True)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(closed, [])
            self.assertIs(self.watcher._observer, observer)
            self.assertTrue(observer.is_alive())
        finally:
            release.set()
            observer.join(1)
        self.watcher.stop(timeout=1)
        self.assertIsNone(self.watcher._observer)

    def test_inflight_ingest_must_finish_before_stop_ack(self):
        entered, release = threading.Event(), threading.Event()
        source = self.root / "fixture.jsonl"
        source.write_text("{}\n")

        def ingest(_path):
            entered.set()
            release.wait(2)

        with patch.object(self.watcher._handler, "_ingest_once", side_effect=ingest):
            worker = threading.Thread(
                target=self.watcher._handler._maybe_ingest, args=(source,)
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaisesRegex(TimeoutError, "ingest"):
                    self.watcher.stop(timeout=0.02)
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(1)
            self.watcher.stop(timeout=1)

    def test_sweeper_timeout_retains_reference(self):
        entered, release = threading.Event(), threading.Event()

        def sweep():
            entered.set()
            release.wait(2)

        sweeper = threading.Thread(target=sweep)
        self.watcher._sweeper = sweeper
        sweeper.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaisesRegex(TimeoutError, "sweeper"):
                self.watcher.stop(timeout=0.02)
            self.assertIs(self.watcher._sweeper, sweeper)
        finally:
            release.set()
            sweeper.join(1)
        self.watcher.stop(timeout=1)
        self.assertIsNone(self.watcher._sweeper)

    def test_stopped_handler_leaves_new_input_pending(self):
        source = self.root / "fixture.jsonl"
        source.write_text("{}\n")
        self.watcher.stop(timeout=1)
        with patch.object(self.watcher._handler, "_ingest_once") as ingest:
            self.watcher._handler._maybe_ingest(source)
        ingest.assert_not_called()
        self.assertTrue(source.is_file())

    def test_successful_stop_is_idempotent(self):
        self.watcher.stop(timeout=1)
        self.watcher.stop(timeout=0)
        self.assertTrue(self.watcher._stopping.is_set())

    def test_actual_main_orders_stop_before_connection_close_without_swallow(self):
        import drover.server.__main__ as main

        tree = ast.parse(Path(main.__file__).read_text())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            statements = node.finalbody
            for i, statement in enumerate(statements):
                if ast.unparse(statement) != "watcher.stop()":
                    continue
                following = [ast.unparse(s) for s in statements[i + 1 :]]
                self.assertIn("close_analytical_connections()", following)
                self.assertIn("close_control_plane_connections()", following)
                found = True
        self.assertTrue(found, "shutdown order changed; review fail-closed propagation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
