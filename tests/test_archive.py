"""Real Git/rclone tests in local temporary directories; no external accounts."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest

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
