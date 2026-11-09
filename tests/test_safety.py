import copy
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ghbackup.common import (
    BackupError,
    Config,
    atomic_json,
    exclusive,
    read_json,
    secret,
)
from ghbackup.controller import Controller, backup_name
from ghbackup.cli import status
from ghbackup.exporter import validate_metadata, verify_checksums
from ghbackup.github import APIError, GitHub, SafeRedirect, backup_releases


def config(root):
    return Config(
        source_owner="alice",
        destination_owner="bob",
        data_dir=str(root / "data"),
        source_token="fixture-source",
        destination_token="fixture-destination",
        drive_repository=str(root / "archive"),
        minimum_free_bytes=0,
    )


def repository(rid=10, name="project", owner="alice", owner_id=1):
    return {
        "id": rid,
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner, "id": owner_id},
        "private": True,
        "fork": False,
        "archived": False,
        "clone_url": f"https://github.com/{owner}/{name}.git",
        "has_wiki": False,
        "has_issues": True,
        "has_discussions": False,
        "default_branch": "main",
    }


class Source:
    token = "source-secret"

    def __init__(self):
        self.repos = [repository()]

    def get(self, path):
        assert path == "/user"
        return {"id": 1, "login": "alice", "type": "User"}

    def pages(self, path, params):
        return copy.deepcopy(self.repos)


class Destination:
    token = "target-secret"

    def __init__(self):
        self.repos = {}
        self.calls = []

    def get(self, path):
        if path == "/user":
            return {"id": 2, "login": "bob", "type": "User"}
        name = path.split("/")[3]
        return copy.deepcopy(self.repos[name])

    def optional(self, path):
        name = path.split("/")[3]
        return self.get(path) if name in self.repos else None

    def call(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "POST":
            r = repository(100 + len(self.repos), body["name"], "bob", 2)
            r.update(body)
            self.repos[r["name"]] = r
            return copy.deepcopy(r)
        name = path.split("/")[3]
        if method == "PUT":
            assert body == {"enabled": False}
            return None
        r = self.repos[name]
        if "name" in body:
            del self.repos[name]
            r["name"] = body["name"]
            r["full_name"] = "bob/" + r["name"]
            r["clone_url"] = "https://github.com/" + r["full_name"] + ".git"
            self.repos[r["name"]] = r
        r.update(body)
        return copy.deepcopy(r)


class FakeArchive:
    def __init__(self):
        self.fail = False
        self.captures = []

    def available(self):
        pass

    def backup(self, rid, kind, complete):
        if self.fail:
            raise BackupError("archive unavailable")
        self.captures.append((rid, kind, complete))
        return "a" * 64


class ControllerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = config(Path(self.temp.name))
        self.c.root.mkdir()
        self.src, self.dst, self.archive = Source(), Destination(), FakeArchive()
        self.ctrl = Controller(
            self.c, source=self.src, destination=self.dst, archive=self.archive
        )
        self.ctrl.identities()
        self.ctrl.discover()

    def test_rename_preserves_identity_and_does_not_duplicate(self):
        entry = self.ctrl.state["repositories"]["10"]
        first = self.ctrl.ensure_target("10", entry)["id"]
        self.src.repos = [repository(name="renamed")]
        self.ctrl.discover()
        target = self.ctrl.ensure_target("10", entry)
        self.assertEqual(target["id"], first)
        self.assertEqual(target["name"], "renamed--10")
        self.assertEqual(len(self.dst.repos), 1)
        self.assertEqual(entry["aliases"], ["project"])

    def test_metadata_checkpoint_only_change_does_not_create_restore_point(self):
        from ghbackup.common import utcnow

        entry = self.ctrl.state["repositories"]["10"]

        def export(args, **kwargs):
            root = Path(args[4]) / "repositories/project/issues"
            atomic_json(root / "1.json", {"body": "unchanged"})
            (root / "last_update").write_text(utcnow())

        with (
            patch("ghbackup.controller.run", side_effect=export),
            patch("ghbackup.controller.backup_releases"),
        ):
            self.ctrl.collect_metadata("10", entry)
            self.ctrl.collect_metadata("10", entry)
        self.assertEqual(len(self.archive.captures), 1)
        self.assertIn("metadata_checked_at", entry)

    def test_reused_name_is_new_identity(self):
        self.ctrl.ensure_target("10", self.ctrl.state["repositories"]["10"])
        self.src.repos = [repository(11)]
        self.ctrl.discover()
        self.ctrl.ensure_target("11", self.ctrl.state["repositories"]["11"])
        self.assertEqual(set(self.dst.repos), {"project--10", "project--11"})
        self.assertEqual(
            self.ctrl.state["repositories"]["10"]["source_status"],
            "not_listed_preserved",
        )
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.dst.calls))

    def test_unrelated_collision_is_never_adopted(self):
        self.dst.repos["project--10"] = repository(500, "project--10", "bob", 2)
        with self.assertRaisesRegex(BackupError, "collision"):
            self.ctrl.ensure_target("10", self.ctrl.state["repositories"]["10"])
        self.assertFalse(self.dst.calls)

    def test_creation_crash_can_recover_marker(self):
        entry = self.ctrl.state["repositories"]["10"]
        target = self.ctrl.ensure_target("10", entry)
        entry["target_id"] = None
        again = self.ctrl.ensure_target("10", entry)
        self.assertEqual(again["id"], target["id"])

    def test_public_target_repaired_and_current_run_stops(self):
        entry = self.ctrl.state["repositories"]["10"]
        self.ctrl.ensure_target("10", entry)
        self.dst.repos[entry["target_name"]]["private"] = False
        with self.assertRaisesRegex(BackupError, "was public"):
            self.ctrl.checked_target(entry)
        self.assertTrue(self.dst.repos[entry["target_name"]]["private"])

    def test_target_id_mismatch_never_repaired_or_overwritten(self):
        entry = self.ctrl.state["repositories"]["10"]
        self.ctrl.ensure_target("10", entry)
        self.dst.repos[entry["target_name"]]["id"] += 1
        before = len(self.dst.calls)
        with self.assertRaisesRegex(BackupError, "identity"):
            self.ctrl.checked_target(entry)
        self.assertEqual(before, len(self.dst.calls))

    def test_failed_listing_does_not_mark_repositories_missing(self):
        def broken(*args):
            yield repository(11)
            raise BackupError("page 2 failed")

        self.src.pages = broken
        with self.assertRaises(BackupError):
            self.ctrl.discover()
        self.assertEqual(set(self.ctrl.state["repositories"]), {"10"})
        self.assertEqual(
            self.ctrl.state["repositories"]["10"]["source_status"], "accessible"
        )

    def test_pending_archive_blocks_new_collection(self):
        entry = self.ctrl.state["repositories"]["10"]
        self.ctrl.begin("10", entry, "git")
        self.archive.fail = True
        with self.assertRaises(BackupError):
            self.ctrl.flush("10", entry)
        with self.assertRaisesRegex(BackupError, "Unarchived"):
            self.ctrl.begin("10", entry, "metadata")
        self.assertFalse(entry.get("publish_pending", False))
        self.assertTrue(entry["pending"])
        self.assertTrue((self.c.root / "repos/10/source.json").is_file())

    def test_ready_capture_retries_without_recapture(self):
        entry = self.ctrl.state["repositories"]["10"]
        self.ctrl.begin("10", entry, "git")
        self.archive.fail = True
        with self.assertRaises(BackupError):
            self.ctrl.ready("10", entry)
        self.assertTrue(entry["pending"]["ready"])
        self.archive.fail = False
        self.ctrl.flush("10", entry)
        self.assertNotIn("pending", entry)
        self.assertTrue(entry["publish_pending"])
        self.assertEqual(self.archive.captures, [("10", "git", True)])

    def test_partial_capture_not_marked_successful(self):
        entry = self.ctrl.state["repositories"]["10"]
        self.ctrl.begin("10", entry, "metadata")
        self.ctrl.flush("10", entry)
        self.assertNotIn("metadata_snapshot", entry)
        self.assertTrue(entry["metadata_force_full"])
        self.assertEqual(self.archive.captures, [("10", "metadata", False)])

    def test_foreign_collaborator_repositories_excluded(self):
        self.src.repos.append(repository(50, owner="other", owner_id=99))
        self.ctrl.discover()
        self.assertEqual(set(self.ctrl.state["repositories"]), {"10"})

    def test_account_replacement_rejected(self):
        self.ctrl.state["source_owner_id"] = 999
        with self.assertRaisesRegex(BackupError, "account ID"):
            self.ctrl.identities()

    def test_preexisting_public_repository_never_has_privacy_or_actions_changed(self):
        entry = self.ctrl.state["repositories"]["10"]
        target = self.ctrl.ensure_target("10", entry)
        self.c.protected_destination_ids = [target["id"]]
        self.dst.repos[entry["target_name"]]["private"] = False
        before = len(self.dst.calls)
        with self.assertRaisesRegex(BackupError, "protected"):
            self.ctrl.checked_target(entry, disable_actions=True)
        self.assertEqual(before, len(self.dst.calls))
        self.assertFalse(self.dst.repos[entry["target_name"]]["private"])

    def test_protected_repository_cannot_be_adopted_even_with_matching_marker(self):
        entry = self.ctrl.state["repositories"]["10"]
        target = self.ctrl.ensure_target("10", entry)
        self.c.protected_destination_ids = [target["id"]]
        entry["target_id"] = None
        before = len(self.dst.calls)
        with self.assertRaisesRegex(BackupError, "protected"):
            self.ctrl.ensure_target("10", entry)
        self.assertEqual(before, len(self.dst.calls))

    def test_metadata_success_does_not_hide_previous_git_failure(self):
        self.ctrl.record_run(
            "git", [{"repo_id": "10", "stage": "git", "error": "wiki inaccessible"}]
        )
        self.ctrl.record_run("metadata", [])
        self.assertFalse(status(self.c)["healthy"])
        self.assertTrue(status(self.c)["task_runs"]["git"]["errors"])

    def test_pending_capture_uploaded_even_when_original_account_is_unavailable(self):
        entry = self.ctrl.state["repositories"]["10"]
        self.ctrl.begin("10", entry, "git")
        entry["pending"]["ready"] = True
        self.src.get = Mock(side_effect=BackupError("Account inaccessible"))
        with self.assertRaises(BackupError):
            self.ctrl.sync("git")
        self.assertEqual(self.archive.captures, [("10", "git", True)])
        self.assertNotIn("pending", entry)


class APISafetyTests(unittest.TestCase):
    def test_secondary_limit_persists_write_cooldown_without_blocking_reads(self):
        with tempfile.TemporaryDirectory() as t:
            file = Path(t) / "backoff.json"
            api = GitHub("fixture", writable=True, cooldown_file=file)
            body = io.BytesIO(b'{"message":"You have exceeded a secondary rate limit"}')
            api.opener.open = Mock(
                side_effect=HTTPError(
                    "url", 403, "", {"X-RateLimit-Remaining": "4940"}, body
                )
            )
            with self.assertRaises(APIError) as caught:
                api.call("POST", "/user/repos", {"private": True, "name": "copy"})
            self.assertTrue(caught.exception.secondary)
            self.assertTrue(file.exists())
            fresh = GitHub("fixture", writable=True, cooldown_file=file)
            fresh.opener.open = Mock()
            with self.assertRaises(APIError):
                fresh.call("POST", "/user/repos", {"private": True, "name": "copy"})
            fresh.opener.open.assert_not_called()
            response = Mock()
            response.__enter__ = Mock(return_value=io.BytesIO(b'{"id":1}'))
            response.__exit__ = Mock(return_value=False)
            fresh.opener.open = Mock(return_value=response)
            self.assertEqual(fresh.get("/user"), {"id": 1})

    def test_api_blocks_all_writes_to_protected_repository_id(self):
        api = GitHub("fixture", writable=True, protected_repository_ids=[42])
        api.get = Mock(return_value={"id": 42})
        api.opener.open = Mock()
        for method, path, body in [
            ("PATCH", "/repos/bob/original", {"private": True}),
            ("PATCH", "/repos/bob/original", {"name": "other"}),
            ("PUT", "/repos/bob/original/actions/permissions", {"enabled": False}),
        ]:
            with self.assertRaisesRegex(BackupError, "protected"):
                api.call(method, path, body)
        api.opener.open.assert_not_called()

    def test_mutation_allowlist_rejects_deletion_and_public_visibility(self):
        for writable in (False, True):
            api = GitHub("not-logged", writable=writable)
            api.opener = Mock()
            for method, path, body in [
                ("DELETE", "/repos/bob/x", None),
                ("PATCH", "/repos/bob/x", {"private": False}),
                ("POST", "/user/repos", {"name": "x"}),
                ("PATCH", "/repos/bob/x", {"archived": False}),
            ]:
                with self.assertRaises(BackupError):
                    api.call(method, path, body)
            api.opener.open.assert_not_called()

    def test_source_cannot_create_even_private_repository(self):
        with self.assertRaisesRegex(BackupError, "read only"):
            GitHub("secret").call("POST", "/user/repos", {"private": True})

    def test_asset_redirect_strips_authorization(self):
        request = Request(
            "https://api.github.com/repos/a/b/releases/assets/1",
            headers={"Authorization": "Bearer sensitive"},
        )
        redirected = SafeRedirect().redirect_request(
            request, None, 302, "", {}, "https://release-assets.githubusercontent.com/a"
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(BackupError):
            SafeRedirect().redirect_request(
                request, None, 302, "", {}, "https://attacker.example/a"
            )

    def test_429_respects_backoff(self):
        sleep = Mock()
        api = GitHub("secret", sleep=sleep)
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"ok":true}'))
        response.__exit__ = Mock(return_value=False)
        api.opener.open = Mock(
            side_effect=[
                HTTPError("url", 429, "", {"Retry-After": "70"}, None),
                response,
            ]
        )
        self.assertEqual(api.get("/user"), {"ok": True})
        sleep.assert_called_once_with(70.0)

    def test_errors_do_not_include_response_body_or_token(self):
        api = GitHub("private-token", retries=0)
        api.opener.open = Mock(
            side_effect=HTTPError("https://secret", 401, "private-token", {}, None)
        )
        with self.assertRaises(APIError) as caught:
            api.get("/user")
        self.assertNotIn("private-token", str(caught.exception))


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_secret_permissions_enforced(self):
        file = self.root / "token"
        file.write_text("secret")
        file.chmod(0o644)
        with self.assertRaises(BackupError):
            secret(str(file))
        file.chmod(0o600)
        self.assertEqual(secret(str(file)), "secret")

    def test_checksum_detects_truncation_and_same_size_corruption(self):
        atomic_json(self.root / "issue.json", {"body": "hello"})
        validate_metadata(self.root)
        verify_checksums(self.root)
        (self.root / "issue.json").write_text('{"body":"wrong"}')
        with self.assertRaises(BackupError):
            verify_checksums(self.root)

    def test_attachment_success_without_file_is_failure(self):
        atomic_json(
            self.root / "manifest.json",
            {"attachments": [{"success": True, "saved_as": "missing"}]},
        )
        with self.assertRaisesRegex(BackupError, "missing"):
            validate_metadata(self.root)

    def test_failed_attachment_is_not_green(self):
        atomic_json(self.root / "manifest.json", {"attachments": [{"success": False}]})
        with self.assertRaisesRegex(BackupError, "not downloaded"):
            validate_metadata(self.root)

    def test_lock_rejects_overlap(self):
        with exclusive(self.root):
            with self.assertRaisesRegex(BackupError, "running"):
                with exclusive(self.root):
                    self.fail("lock not enforced")

    def test_names_are_bounded_and_id_suffix_preserved(self):
        name = backup_name("a" * 100, 123456789)
        self.assertEqual(len(name), 100)
        self.assertTrue(name.endswith("--123456789"))


if __name__ == "__main__":
    unittest.main()
