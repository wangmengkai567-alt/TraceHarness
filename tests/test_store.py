import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from harness.store import RUN_ID, RunStore


def allocate_run(root):
    return RunStore(Path(root)).run_id


class RunIDTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runs"

    def create_on(self, day):
        with patch("harness.store.datetime") as clock:
            clock.now.return_value = datetime.strptime(day, "%Y%m%d")
            return RunStore(self.root)

    def test_daily_sequence_and_new_day(self):
        self.assertEqual(self.create_on("20261015").run_id, "20261015001")
        self.assertEqual(self.create_on("20261015").run_id, "20261015002")
        self.assertEqual(self.create_on("20261016").run_id, "20261016001")
        self.assertEqual(self.create_on("20261015").run_id, "20261015003")

    def test_persisted_maximum_and_unfinished_reservations(self):
        self.root.mkdir()
        for name in ("a" * 32, "stage-three-workspace-example", "20261014099",
                     "20261015002", "20261015009"):
            (self.root / name).mkdir()
        store = self.create_on("20261015")
        self.assertEqual(store.run_id, "20261015010")
        self.assertTrue(store.directory.is_dir())
        self.assertEqual(self.create_on("20261015").run_id, "20261015011")

    def test_sequence_grows_beyond_three_digits(self):
        self.root.mkdir()
        (self.root / "20261015999").mkdir()
        (self.root / "202610151000").mkdir()
        self.assertEqual(self.create_on("20261015").run_id, "202610151001")

    def test_atomic_reservation_collision_does_not_overwrite(self):
        self.root.mkdir()
        original_mkdir = Path.mkdir
        collided = False

        def concurrent_mkdir(path, *args, **kwargs):
            nonlocal collided
            if path.name == "20261015001" and not collided:
                collided = True
                original_mkdir(path)
                (path / "existing.txt").write_bytes(b"reserved by another process")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", concurrent_mkdir):
            store = self.create_on("20261015")
        self.assertEqual(store.run_id, "20261015002")
        self.assertEqual((self.root / "20261015001" / "existing.txt").read_bytes(),
                         b"reserved by another process")

    def test_multiple_processes_share_daily_sequence(self):
        with ProcessPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(allocate_run, [str(self.root)] * 24))
        self.assertEqual(len(set(ids)), 24)
        for day in {run_id[:8] for run_id in ids}:
            sequences = sorted(int(run_id[8:]) for run_id in ids if run_id.startswith(day))
            self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_validator_accepts_both_formats_and_rejects_paths(self):
        for value in ("20261015001", "202610151000", "a" * 32):
            self.assertIsNotNone(RUN_ID.fullmatch(value))
        for value in ("2026101501", "../20261015001", "20261015001/other", "", "z" * 32):
            self.assertIsNone(RUN_ID.fullmatch(value))
