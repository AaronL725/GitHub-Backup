import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ghbackup.common import BackupError, atomic_json
from ghbackup.controller import Controller
from ghbackup.exporter import (
    rename_export,
    recover_rename,
    validate_metadata,
    verify_checksums,
)
from ghbackup.telegram_views import RESTORE
from ghbackup.cli import parser
from test_safety import config, Source, Destination, FakeArchive, repository


class ReviewFaults(unittest.TestCase):
    def test_failed_queue_pauses_only_that_repository_collection(self):
        self.ctrl.source.repos.append(repository(11, name="second"))
        self.ctrl.collect_git = Mock()
        with patch.object(
            self.ctrl,
            "drain_queue",
            return_value=[{"stage": "queue", "repo_id": "10", "error": "bad capture"}],
        ):
            with self.assertRaises(BackupError):
                self.ctrl.sync("git")
        self.ctrl.collect_git.assert_called_once()
        self.assertEqual(self.ctrl.collect_git.call_args.args[0], "11")

    def test_retryable_local_io_error_is_not_a_drive_outage(self):
        from types import SimpleNamespace
        from ghbackup.archive import Archive, DriveUnavailable

        result = SimpleNamespace(
            returncode=5, stdout="", stderr="Cannot open local source file: I/O error"
        )
        with patch("ghbackup.archive.subprocess.run", return_value=result):
            with self.assertRaises(BackupError) as caught:
                Archive(self.c).command("copy", "local", "remote")
        self.assertNotIsInstance(caught.exception, DriveUnavailable)

    def test_first_service_failure_disables_further_uploads(self):
        from ghbackup.archive import DriveUnavailable

        self.archive.backup = Mock(side_effect=DriveUnavailable("quota unavailable"))
        for rid in ("10", "11"):
            entry = self.ctrl.state["repositories"].setdefault(
                rid, {"source": repository(int(rid))}
            )
            self.ctrl.begin(rid, entry, "metadata")
            with self.assertRaises(BackupError):
                self.ctrl.flush(rid, entry)
        self.assertEqual(self.archive.backup.call_count, 1)
        self.assertFalse(self.ctrl._drive_available)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = config(Path(self.temp.name))
        self.c.root.mkdir()
        self.archive = FakeArchive()
        self.ctrl = Controller(
            self.c, source=Source(), destination=Destination(), archive=self.archive
        )
        self.ctrl.identities()
        self.ctrl.discover()

    def test_interrupted_rename_recovers_both_checksum_inventories(self):
        meta = self.c.root / "repos/10/metadata"
        output = meta / "export"
        atomic_json(output / "repositories/old/issues/1.json", {"body": "old history"})
        validate_metadata(output)
        validate_metadata(meta)
        write = atomic_json

        def interrupted(path, value):
            if path == meta / "checksums.json":
                raise OSError("interrupted")
            write(path, value)

        with patch("ghbackup.exporter.atomic_json", side_effect=interrupted):
            with self.assertRaises(OSError):
                rename_export(output, "new")
        recover_rename(output)
        verify_checksums(output)
        verify_checksums(meta)
        self.assertTrue((output / "repositories/new/issues/1.json").exists())
        from ghbackup.exporter import export

        atomic_json(meta.parent / "source.json", repository(name="third"))
        with patch(
            "github_backup.github_backup.backup_repositories",
            side_effect=BackupError("API unavailable"),
        ):
            with self.assertRaises(BackupError):
                export(meta.parent / "source.json", output, "github_pat_fixture")
        verify_checksums(output)
        verify_checksums(meta)

    def test_offline_cold_metadata_pauses_before_export(self):
        self.c.evict_completed_cache = True
        entry = self.ctrl.state["repositories"]["10"]
        entry["metadata_snapshot"] = "a" * 32
        self.ctrl._drive_available = False
        self.ctrl._collect_metadata = Mock()
        with self.assertRaisesRegex(BackupError, "Historical metadata"):
            self.ctrl.collect_metadata("10", entry)
        self.ctrl._collect_metadata.assert_not_called()
        self.assertNotIn("pending", entry)

    def test_offline_round_queues_without_uploading_again(self):
        self.ctrl._drive_available = False
        self.archive.backup = Mock()
        for rid in ["10", "11"]:
            entry = self.ctrl.state["repositories"].setdefault(
                rid, {"source": repository(int(rid))}
            )
            self.ctrl.begin(rid, entry, "metadata")
            with self.assertRaises(BackupError):
                self.ctrl.flush(rid, entry)
        self.archive.backup.assert_not_called()
        self.assertEqual(len(list((self.c.root / "drive-queue").iterdir())), 2)

    def test_bad_task_blocks_only_its_repository(self):
        self.ctrl.state["repositories"]["11"] = {"source": repository(11)}
        for sequence, rid, category in [
            (1, "10", "git"),
            (2, "10", "metadata"),
            (3, "11", "metadata"),
        ]:
            folder = self.c.root / "drive-queue" / str(sequence)
            resource = folder / "repos" / rid / category
            resource.mkdir(parents=True)
            atomic_json(resource.parent / "source.json", repository(int(rid)))
            if category == "metadata":
                atomic_json(resource / "item.json", {"ok": True})
                validate_metadata(resource)
            atomic_json(
                folder / "capture.json",
                {"repo_id": rid, "category": category, "complete": True},
            )
        with patch("ghbackup.controller.Archive", return_value=self.archive):
            errors = self.ctrl.drain_queue()
        self.assertEqual(len(errors), 1)
        self.assertTrue(self.ctrl._drive_available)
        self.assertEqual(self.archive.captures, [("11", "metadata", True)])
        self.assertTrue((self.c.root / "drive-queue/1").exists())
        self.assertTrue((self.c.root / "drive-queue/2").exists())
        self.assertFalse((self.c.root / "drive-queue/3").exists())

    def test_failed_export_retains_partial_capture_and_restores_baseline(self):
        meta = self.c.root / "repos/10/metadata"
        file = meta / "export/repositories/project/issues/1.json"
        atomic_json(file, {"body": "trusted"})
        validate_metadata(meta)

        def failure(*args, **kwargs):
            self.ctrl.begin("10", self.ctrl.state["repositories"]["10"], "metadata")
            atomic_json(file, {"body": "partial export"})
            raise BackupError("API unavailable")

        with patch.object(self.ctrl, "_collect_metadata", side_effect=failure):
            with self.assertRaises(BackupError):
                self.ctrl.collect_metadata("10", self.ctrl.state["repositories"]["10"])
        verify_checksums(meta)
        self.assertIn("trusted", file.read_text())
        self.assertEqual(self.archive.captures, [("10", "metadata", False)])

    def test_restore_help_uses_bounded_output(self):
        self.assertNotIn("/var/tmp", RESTORE)
        self.assertIn("/restore/recovered-ID", RESTORE)
        args = parser().parse_args(
            ["restore", "--repo-id", "ID", "--target", "/restore/recovered-ID"]
        )
        self.assertTrue(args.target.is_relative_to("/restore"))

    def test_upload_task_error_does_not_mark_drive_offline(self):
        self.ctrl.state["repositories"]["11"] = {"source": repository(11)}
        for rid in ("10", "11"):
            folder = self.c.root / "drive-queue" / rid
            resource = folder / "repos" / rid / "metadata"
            atomic_json(resource.parent / "source.json", repository(int(rid)))
            atomic_json(resource / "data.json", {"body": "intact"})
            validate_metadata(resource)
            atomic_json(
                folder / "capture.json",
                {"repo_id": rid, "category": "metadata", "complete": True},
            )

        def upload(rid, *args, **kwargs):
            if rid == "10":
                raise BackupError("task invalid")
            return "valid-point"

        self.archive.backup = Mock(side_effect=upload)
        with patch("ghbackup.controller.Archive", return_value=self.archive):
            errors = self.ctrl.drain_queue()
        self.assertEqual(len(errors), 1)
        self.assertTrue(self.ctrl._drive_available)
        self.assertEqual(
            self.ctrl.state["repositories"]["11"]["metadata_snapshot"], "valid-point"
        )

    def test_ready_metadata_survives_crash_after_queue_acknowledgement(self):
        import shutil

        root = self.c.root / "repos/10"
        metadata = root / "metadata"
        file = metadata / "item.json"
        atomic_json(file, {"body": "previous"})
        validate_metadata(metadata)
        shutil.copytree(metadata, root / ".metadata-baseline")
        atomic_json(file, {"body": "newly captured"})
        validate_metadata(metadata)
        atomic_json(root / "metadata-transaction.json", {"ready": True})
        with patch.object(self.ctrl, "_collect_metadata"):
            self.ctrl.collect_metadata("10", self.ctrl.state["repositories"]["10"])
        verify_checksums(metadata)
        self.assertIn("newly captured", file.read_text())

    def test_malformed_later_task_does_not_block_earlier_versions(self):
        for name in ("first", "broken"):
            folder = self.c.root / "drive-queue" / name
            resource = folder / "repos/10/metadata"
            atomic_json(resource / "data.json", {"body": "valid"})
            atomic_json(resource.parent / "source.json", repository())
            validate_metadata(resource)
            atomic_json(
                folder / "capture.json",
                {"repo_id": "10", "category": "metadata", "complete": True},
            )
        (self.c.root / "drive-queue/broken/capture.json").write_text("broken JSON")
        with patch("ghbackup.controller.Archive", return_value=self.archive):
            errors = self.ctrl.drain_queue()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.archive.captures, [("10", "metadata", True)])
        self.assertFalse((self.c.root / "drive-queue/first").exists())
        self.assertTrue((self.c.root / "drive-queue/broken").exists())
