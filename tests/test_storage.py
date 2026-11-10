import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ghbackup.common import BackupError
from ghbackup.storage import Storage
from test_safety import config


class StorageTests(unittest.TestCase):
    def test_cleanup_removes_only_abandoned_work_and_uncommitted_queue(self):
        with tempfile.TemporaryDirectory() as t:
            c = config(Path(t))
            for name in (
                ".plain-old",
                "tmpold",
                ".hydrate-old",
                "repos/10/git",
                "drive-queue/capture",
                "drive-queue/.building-old",
            ):
                path = c.root / name
                path.mkdir(parents=True)
                (path / "data").write_text("keep until acknowledged")
            self.assertEqual(Storage(c).cleanup(), 4)
            self.assertTrue((c.root / "repos/10/git/data").exists())
            self.assertTrue((c.root / "drive-queue/capture/data").exists())

    def test_budget_requires_bounded_mount_and_stops_without_deleting_data(self):
        with tempfile.TemporaryDirectory() as t:
            c = config(Path(t))
            c.root.mkdir()
            c.local_storage_limit_bytes = 8_000_000_000
            with patch(
                "ghbackup.storage.shutil.disk_usage",
                return_value=type(
                    "Usage", (), {"total": 50_000_000_000, "free": 100}
                )(),
            ):
                with self.assertRaisesRegex(BackupError, "not mounted"):
                    Storage(c).validate()
            c.minimum_free_bytes = 1024
            file = c.root / "unuploaded"
            file.write_text("original")
            with patch(
                "ghbackup.storage.shutil.disk_usage",
                return_value=type("Usage", (), {"free": 100})(),
            ):
                with self.assertRaises(BackupError):
                    Storage(c).ensure_space()
                Storage(c).ensure_space(32, reserve=False)
            self.assertEqual(file.read_text(), "original")
