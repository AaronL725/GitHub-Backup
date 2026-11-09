import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ghbackup.common import BackupError, atomic_json, read_json
from ghbackup.exporter import export, verify_checksums
from ghbackup.github import backup_releases
from test_safety import repository


@unittest.skipUnless(
    importlib.util.find_spec("github_backup"), "Install requirements.lock"
)
class UpstreamCompatibilityTests(unittest.TestCase):
    def setUp(self):
        from github_backup import github_backup

        self.upstream = github_backup
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = repository()
        self.repo["has_issues"] = False
        atomic_json(self.root / "source.json", self.repo)
        (self.root / "token").write_text("github_pat_fake")
        (self.root / "token").chmod(0o600)

    def test_pinned_upstream_empty_export(self):
        with patch.object(self.upstream, "retrieve_data", return_value=[]):
            export(
                self.root / "source.json",
                self.root / "export",
                (self.root / "token").read_text().strip(),
                full=True,
            )
        verify_checksums(self.root / "export")
        self.assertTrue((self.root / "export/repositories/project/pulls").is_dir())

    def test_upstream_warning_is_not_a_success(self):
        def warn(*args, **kwargs):
            self.upstream.logger.warning("Attachment was skipped")
            return []

        with patch.object(self.upstream, "retrieve_data", side_effect=warn):
            with self.assertRaisesRegex(BackupError, "warnings/errors"):
                export(
                    self.root / "source.json",
                    self.root / "export",
                    (self.root / "token").read_text().strip(),
                    full=True,
                )
        diagnostic = read_json(self.root / "metadata-diagnostic.json")
        self.assertTrue(diagnostic["failed"])
        self.assertIn("Attachment was skipped", diagnostic["messages"][0])

    def test_diagnostic_redacts_tokens_urls_and_repo_names(self):
        def warn(*args, **kwargs):
            self.upstream.logger.warning(
                "github_pat_fake https://private.example/secret "
                + self.repo["full_name"]
            )
            return []

        with patch.object(self.upstream, "retrieve_data", side_effect=warn):
            with self.assertRaises(BackupError):
                export(
                    self.root / "source.json",
                    self.root / "export",
                    (self.root / "token").read_text().strip(),
                    full=True,
                )
        text = (self.root / "metadata-diagnostic.json").read_text()
        self.assertNotIn("github_pat_fake", text)
        self.assertNotIn("private.example", text)
        self.assertNotIn(self.repo["full_name"], text)

    def test_issue_pr_reviews_and_comments_use_real_upstream_serialization(self):
        self.repo["has_issues"] = True
        atomic_json(self.root / "source.json", self.repo)

        def response(args, url, **kwargs):
            if url.endswith("/issues"):
                return [
                    {"number": 1, "body": "issue", "updated_at": "2026-11-01T00:00:00Z"}
                ]
            if url.endswith("/pulls") or url.endswith("/pulls/2"):
                return [
                    {"number": 2, "body": "PR", "updated_at": "2026-11-02T00:00:00Z"}
                ]
            if url.endswith("/reviews"):
                return [{"id": 9, "state": "APPROVED", "body": "review"}]
            if url.endswith("/comments"):
                return [{"id": 8, "body": "comment"}]
            return []

        with patch.object(self.upstream, "retrieve_data", side_effect=response):
            export(
                self.root / "source.json",
                self.root / "export",
                (self.root / "token").read_text().strip(),
                full=True,
            )
        folder = self.root / "export/repositories/project"
        self.assertEqual(
            read_json(folder / "issues/1.json")["comment_data"][0]["body"], "comment"
        )
        pull = read_json(folder / "pulls/2.json")
        self.assertEqual(pull["review_data"][0]["state"], "APPROVED")
        self.assertEqual(pull["comment_regular_data"][0]["body"], "comment")
        verify_checksums(self.root / "export")


class ReleaseTests(unittest.TestCase):
    def test_replaced_same_name_asset_is_redownloaded_and_paths_use_ids(self):
        class API:
            version = 1
            downloads = 0

            def pages(self, path):
                if path.endswith("/assets"):
                    return [
                        {
                            "id": 4,
                            "name": "../../payload",
                            "size": 3,
                            "updated_at": str(self.version),
                        }
                    ]
                return [{"id": 2, "tag_name": "../v1"}]

            def asset(self, path, file, size, digest):
                from ghbackup.common import sha256

                self.downloads += 1
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"one" if self.version == 1 else b"two")
                return sha256(file)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "releases"
            api = API()
            backup_releases(api, repository(), root)
            backup_releases(api, repository(), root)
            self.assertEqual(api.downloads, 1)
            api.version = 2
            backup_releases(api, repository(), root)
            self.assertEqual(api.downloads, 2)
            self.assertEqual((root / "2/4.bin").read_bytes(), b"two")
            self.assertEqual(
                read_json(root / "inventory.json")["assets"][0]["original_name"],
                "../../payload",
            )


if __name__ == "__main__":
    unittest.main()
