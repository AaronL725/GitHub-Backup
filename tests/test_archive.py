"""Real Git/rclone tests in local temporary directories; no external accounts."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from subprocess import TimeoutExpired

from ghbackup.archive import Archive
from ghbackup.common import atomic_json
from ghbackup.controller import verify_restored
from ghbackup.exporter import validate_metadata
from ghbackup.git import Git
from test_safety import config, repository


@unittest.skipUnless(
    shutil.which("rclone") and shutil.which("git-lfs"), "Git LFS and rclone required"
)
class ArchiveTests(unittest.TestCase):
    def test_evicted_metadata_restores_before_incremental_export_and_keeps_old_items(
        self,
    ):
        from ghbackup.controller import Controller
        from test_safety import Source, Destination
        from unittest.mock import patch

        self.c.evict_completed_cache = True
        controller = Controller(
            self.c, source=Source(), destination=Destination(), archive=self.archive
        )
        controller.identities()
        controller.discover()
        entry = controller.state["repositories"]["10"]
        metadata = self.repo / "metadata"
        atomic_json(
            metadata / "export/repositories/project/issues/1.json",
            {"body": "deleted upstream later"},
        )
        validate_metadata(metadata / "export")
        validate_metadata(metadata)
        entry["metadata_snapshot"] = self.archive.backup(
            "10", "metadata", complete=True
        )
        controller.evict_cache("10", entry)
        self.assertFalse(metadata.exists())

        def incremental(*args, **kwargs):
            atomic_json(
                metadata / "export/repositories/project/issues/2.json", {"body": "new"}
            )
            validate_metadata(metadata / "export")
            return ""

        with (
            patch("ghbackup.controller.run", side_effect=incremental),
            patch("ghbackup.controller.backup_releases"),
        ):
            controller.collect_metadata("10", entry)
        controller.evict_cache("10", entry)
        self.assertFalse(metadata.exists())
        target = self.base / "metadata-after-eviction"
        self.archive.restore("10", target, category="metadata")
        verify_restored(target, "10")
        issues = target / "repos/10/metadata/export/repositories/project/issues"
        self.assertTrue((issues / "1.json").exists())
        self.assertTrue((issues / "2.json").exists())

    def test_evicted_git_cache_skips_unchanged_and_repairs_target_drift_from_drive(
        self,
    ):
        from ghbackup.controller import Controller
        from test_safety import Source, Destination
        from ghbackup.common import BackupError
        from unittest.mock import patch

        target = self.base / "evicted-target.git"
        self.git.command("init", "--bare", "--initial-branch=main", target)
        mapping = {
            "https://github.com/alice/project.git": str(self.work),
            "https://github.com/bob/project--10.git": str(target),
        }

        class LocalGit(Git):
            def command(inner, *args, **kwargs):
                return super().command(
                    *(mapping.get(str(a), a) for a in args), **kwargs
                )

        self.c.evict_completed_cache = True
        controller = Controller(
            self.c, source=Source(), destination=Destination(), archive=self.archive
        )
        controller.source_git = LocalGit()
        controller.destination_git = LocalGit()
        controller.sync("git")
        self.assertFalse((self.repo / "git/repository.git").exists())
        first = self.archive.snapshots("10")[-1]["id"]
        controller.sync("git")
        self.assertEqual(len(self.archive.snapshots("10")), 1)
        self.git.command(
            "-C",
            target,
            "update-ref",
            "refs/heads/unwanted",
            self.git.refs(target)["refs/heads/main"],
        )
        controller.sync("git")
        self.assertEqual(self.git.refs(target), self.git.refs(self.work))
        self.assertFalse((self.repo / "git/repository.git").exists())
        (self.work / "README.md").write_text("updated after eviction\n")
        self.commit()
        controller.sync("git")
        self.assertFalse((self.repo / "git/repository.git").exists())
        self.archive.restore("10", self.base / "before-eviction", first, category="git")
        self.assertEqual(
            (self.base / "before-eviction/repos/10/git/code/README.md").read_text(),
            "first version\n",
        )
        (self.work / "README.md").write_text("captured while target unavailable\n")
        self.commit()
        with patch.object(
            controller.destination, "get", side_effect=BackupError("target unavailable")
        ):
            with self.assertRaises(BackupError):
                controller.sync("git")
        self.assertTrue(controller.state["repositories"]["10"]["publish_pending"])
        self.assertFalse((self.repo / "git/repository.git").exists())
        controller.sync("git")
        self.assertEqual(self.git.refs(target), self.git.refs(self.work))
        self.assertFalse((self.repo / "git/repository.git").exists())

    def test_destination_outage_keeps_restorable_drive_capture_and_retries_publish(
        self,
    ):
        from ghbackup.controller import Controller
        from ghbackup.common import BackupError
        from test_safety import Source, Destination
        from unittest.mock import patch

        target = self.base / "github-target.git"
        self.git.command("init", "--bare", "--initial-branch=main", target)
        mapping = {
            "https://github.com/alice/project.git": str(self.work),
            "https://github.com/bob/project--10.git": str(target),
        }

        class LocalGit(Git):
            def command(inner, *args, **kwargs):
                return super().command(
                    *(mapping.get(str(a), a) for a in args), **kwargs
                )

        destination = Destination()
        controller = Controller(
            self.c, source=Source(), destination=destination, archive=self.archive
        )
        controller.source_git = LocalGit()
        controller.destination_git = LocalGit()
        with patch.object(
            destination, "get", side_effect=BackupError("account unavailable")
        ):
            with self.assertRaises(BackupError):
                controller.sync("git")
        entry = controller.state["repositories"]["10"]
        self.assertTrue(entry["git_snapshot"])
        self.assertTrue(entry["publish_pending"])
        restored = self.base / "target-outage-restore"
        self.archive.restore("10", restored, category="git")
        verify_restored(restored, "10")
        self.assertEqual(
            (restored / "repos/10/git/code/README.md").read_text(), "first version\n"
        )
        controller.sync("git")
        self.assertFalse(entry["publish_pending"])
        self.assertEqual(self.git.refs(target), self.git.refs(self.work))

    def test_drive_outage_keeps_github_live_and_preserves_each_queued_version(self):
        from ghbackup.controller import Controller
        from ghbackup.common import BackupError
        from test_safety import Source, Destination
        from unittest.mock import patch

        target = self.base / "github-target.git"
        self.git.command("init", "--bare", "--initial-branch=main", target)
        mapping = {
            "https://github.com/alice/project.git": str(self.work),
            "https://github.com/bob/project--10.git": str(target),
        }

        class LocalGit(Git):
            def command(inner, *args, **kwargs):
                return super().command(
                    *(mapping.get(str(a), a) for a in args), **kwargs
                )

        controller = Controller(
            self.c, source=Source(), destination=Destination(), archive=self.archive
        )
        controller.source_git = LocalGit()
        controller.destination_git = LocalGit()
        with (
            patch.object(
                self.archive, "backup", side_effect=TimeoutExpired("rclone", 1)
            ),
            patch.object(
                controller, "drain_queue", side_effect=BackupError("Drive offline")
            ),
        ):
            with self.assertRaises(BackupError):
                controller.sync("git")
            (self.work / "README.md").write_text("second captured version\n")
            self.commit()
            with self.assertRaises(BackupError):
                controller.sync("git")
        self.assertEqual(
            controller.destination_git.refs(target)["refs/heads/main"],
            self.git.refs(self.work)["refs/heads/main"],
        )
        self.assertEqual(len(list((self.c.root / "drive-queue").iterdir())), 2)
        controller.sync("git")
        self.assertEqual(list((self.c.root / "drive-queue").iterdir()), [])
        points = self.archive.snapshots("10")
        self.assertEqual(len(points), 2)
        self.archive.restore(
            "10", self.base / "old-queued", points[0]["id"], category="git"
        )
        self.assertEqual(
            (self.base / "old-queued/repos/10/git/code/README.md").read_text(),
            "first version\n",
        )

    def test_legacy_flat_indexes_migrate_once_without_deleting_history(self):
        point = self.archive.backup("10", "git", complete=True)
        root = self.base / "archive/_系统"
        shutil.copy2(
            root / "index/10" / f"{point}.json", root / "index" / f"{point}.json"
        )
        (root / "index/10" / f"{point}.json").unlink()
        (root / "index-version.json").unlink()
        self.assertEqual(self.archive.snapshots("10")[0]["id"], point)
        self.assertTrue((root / "index" / f"{point}.json").exists())
        self.assertEqual(len(self.archive.snapshots()), 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.c = config(self.base)
        self.c.root.mkdir()
        self.git = Git()
        self.work = self.base / "work"
        self.repo = self.c.root / "repos/10"
        self.repo.mkdir(parents=True)
        atomic_json(self.repo / "source.json", repository())
        atomic_json(self.c.root / "state.json", {"schema": 1, "repositories": {}})
        self.git.command("init", "-b", "main", self.work)
        self.git.command("-C", self.work, "config", "user.name", "Test")
        self.git.command(
            "-C", self.work, "config", "user.email", "test@example.invalid"
        )
        (self.work / "README.md").write_text("first version\n")
        self.commit()
        self.bare = self.repo / "git/repository.git"
        self.git.fetch(str(self.work), self.bare, "main")
        self.manifest()
        self.archive = Archive(self.c)
        self.archive.init()

    def commit(self):
        self.git.command("-C", self.work, "add", ".")
        self.git.command("-C", self.work, "commit", "-m", "change")

    def manifest(self):
        atomic_json(
            self.repo / "git/manifest.json",
            {
                "refs": self.git.refs(self.bare),
                "lfs": [],
                "wiki_refs": {},
                "default_branch": "main",
                "wiki_status": "disabled",
            },
        )

    def test_files_are_readable_and_old_code_restores_after_updates(self):
        first = self.archive.backup("10", "git", complete=True)
        visible = self.base / "archive/project--10/代码/README.md"
        self.assertEqual(visible.read_text(), "first version\n")
        (self.work / "README.md").write_text("second version\n")
        self.commit()
        self.git.fetch(str(self.work), self.bare, "main")
        self.manifest()
        self.archive.backup("10", "git", complete=True)
        self.assertEqual(visible.read_text(), "second version\n")
        self.archive.restore("10", self.base / "restored", first, category="git")
        verify_restored(self.base / "restored", "10")
        self.assertEqual(
            (self.base / "restored/repos/10/git/code/README.md").read_text(),
            "first version\n",
        )

    def test_metadata_old_version_restores_without_password(self):
        meta = self.repo / "metadata"
        atomic_json(meta / "export/repositories/project/issues/1.json", {"body": "old"})
        validate_metadata(meta)
        first = self.archive.backup("10", "metadata", complete=True)
        atomic_json(meta / "export/repositories/project/issues/1.json", {"body": "new"})
        validate_metadata(meta)
        self.archive.backup("10", "metadata", complete=True)
        self.assertEqual(
            json.loads(
                (self.base / "archive/project--10/元数据/issues/1.json").read_text()
            )["body"],
            "new",
        )
        self.archive.restore(
            "10", self.base / "restored-meta", first, category="metadata"
        )
        verify_restored(self.base / "restored-meta", "10")
        self.assertEqual(
            json.loads(
                (
                    self.base
                    / "restored-meta/repos/10/metadata/export/repositories/project/issues/1.json"
                ).read_text()
            )["body"],
            "old",
        )

    def test_repo_rename_moves_existing_backup_without_duplicate(self):
        first = self.archive.backup("10", "git", complete=True)
        source = repository(name="renamed")
        atomic_json(self.repo / "source.json", source)
        self.archive.backup("10", "git", complete=True)
        self.assertFalse((self.base / "archive/project--10").exists())
        self.assertTrue((self.base / "archive/renamed--10/代码/README.md").exists())
        self.archive.restore("10", self.base / "renamed-restore", first, category="git")
        verify_restored(self.base / "renamed-restore", "10")

    def test_verify_skips_repository_missing_metadata(self):
        self.archive.backup("10", "git", complete=True)
        other = self.c.root / "repos/20"
        shutil.copytree(self.repo, other)
        atomic_json(other / "source.json", repository(rid=20, name="complete"))
        atomic_json(other / "metadata/issue.json", {"body": "ok"})
        validate_metadata(other / "metadata")
        self.archive.backup("20", "git", complete=True)
        self.archive.backup("20", "metadata", complete=True)
        self.archive.check()
        self.assertEqual(
            json.loads((self.c.root / "plain-verification.json").read_text())[
                "repo_id"
            ],
            "20",
        )

    def test_restore_reads_only_requested_repository_index(self):
        point = self.archive.backup("10", "git", complete=True)
        from unittest.mock import patch

        with patch.object(self.archive, "json", wraps=self.archive.json) as reads:
            self.archive.restore(
                "10", self.base / "scoped-restore", point, category="git"
            )
        indexes = [
            call.args[0]
            for call in reads.call_args_list
            if call.args[0].startswith("_系统/index/")
        ]
        self.assertTrue(indexes)
        self.assertTrue(all(path.startswith("_系统/index/10/") for path in indexes))
