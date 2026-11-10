import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ghbackup.common import BackupError, Config, atomic_json
from ghbackup.exporter import export, validate_metadata, verify_checksums
from ghbackup.git import Git
from ghbackup.github import check_write_cooldown
from ghbackup.telemetry import Store


class ReliabilityTests(unittest.TestCase):
    def test_metadata_rename_validates_old_paths_before_moving(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            output = root / "output"
            atomic_json(output / "repositories/old/issues/1.json", {"body": "intact"})
            validate_metadata(output)
            atomic_json(
                root / "source.json",
                {
                    "id": 10,
                    "name": "new",
                    "owner": {"login": "alice"},
                    "full_name": "alice/new",
                },
            )
            with patch("github_backup.github_backup.backup_repositories"):
                export(root / "source.json", output, "github_pat_fake")
            verify_checksums(output)
            self.assertTrue((output / "repositories/new/issues/1.json").is_file())
            self.assertFalse((output / "repositories/old").exists())

    def test_id_lists_reject_invalid_types_and_duplicates(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "config.toml"
            for name in (
                "include_repo_ids",
                "empty_wiki_ids",
                "protected_destination_ids",
            ):
                for invalid in ('["12"]', "[true]", "[0]", "[-1]", "[12,12]", '"12"'):
                    path.write_text(
                        f'source_owner="alice"\ndestination_owner="bob"\n{name}={invalid}\n'
                    )
                    path.chmod(0o600)
                    with (
                        self.subTest(name=name, value=invalid),
                        self.assertRaises(BackupError),
                    ):
                        Config.load(path)

    def test_corrupt_metadata_is_not_reblessed_by_incremental_export(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            output = root / "output"
            file = output / "repositories/project/issues/1.json"
            atomic_json(file, {"body": "good"})
            validate_metadata(output)
            before = (output / "checksums.json").read_bytes()
            file.write_text('{"body":"evil"}')
            atomic_json(
                root / "source.json",
                {
                    "id": 10,
                    "name": "project",
                    "owner": {"login": "alice"},
                    "full_name": "alice/project",
                },
            )
            with patch("github_backup.github_backup.backup_repositories") as upstream:
                with self.assertRaises(BackupError):
                    export(root / "source.json", output, "github_pat_fake")
                upstream.assert_not_called()
            self.assertEqual((output / "checksums.json").read_bytes(), before)

    def test_cooldown_blocks_git_and_lfs_before_any_process(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "backoff.json"
            atomic_json(path, {"until": time.time() + 3600})
            git = Git(write_guard=lambda: check_write_cooldown(path))
            with patch("ghbackup.git.run") as process:
                for args in [("push", "url", "main"), ("lfs", "push", "--all", "url")]:
                    with self.subTest(args=args), self.assertRaises(BackupError):
                        git.command(*args)
                process.assert_not_called()

    def test_event_query_uses_partial_index_and_retains_unsent_history(self):
        with tempfile.TemporaryDirectory() as t:
            store = Store(Path(t))
            with store.connect() as db, db:
                db.execute(
                    "INSERT INTO events(created,kind,payload,delivered) VALUES ('2000-01-01','git','{}',1)"
                )
                db.execute(
                    "INSERT INTO events(created,kind,payload,delivered) VALUES ('2000-01-01','git','{}',0)"
                )
                plan = " ".join(
                    str(tuple(row))
                    for row in db.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE delivered=0 ORDER BY sequence LIMIT 20"
                    )
                )
            self.assertIn("events_pending", plan)
            store.sample(
                {"total": 1, "git": 1, "metadata": 1, "published": 1, "disk_percent": 1}
            )
            with store.connect() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM events WHERE delivered=1"
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(len(store.pending()), 1)
