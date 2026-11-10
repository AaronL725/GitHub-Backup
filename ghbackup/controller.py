from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from .archive import Archive
from .common import BackupError, atomic_json, read_json, run, utcnow
from .exporter import validate_metadata, verify_checksums
from .git import Git, github_clone_url
from .github import GitHub, backup_releases, check_write_cooldown


UNSUPPORTED = {
    "actions_secrets": "Values cannot be exported; keep original values in a password manager.",
    "actions_logs_artifacts": "Not collected by this version; subject to source retention.",
    "packages": "Not collected; use registry-specific export for package blobs.",
    "external_dependencies": "Third-party submodules, external LFS and arbitrary linked files need separate coverage.",
    "access_settings": "Collaborators, deploy keys and execution credentials are not replicated.",
}


def backup_name(name, repo_id):
    suffix = "--" + str(int(repo_id))
    return name[: 100 - len(suffix)] + suffix


def age_seconds(timestamp):
    if not timestamp:
        return float("inf")
    return (
        dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(timestamp)
    ).total_seconds()


class Controller:
    def __init__(self, config, *, source=None, destination=None, archive=None):
        self.c = config
        self.root = config.root
        self.state = read_json(
            self.root / "state.json", {"schema": 1, "repositories": {}}
        )
        if self.state.get("schema") != 1:
            raise BackupError("Unsupported state schema")
        self.source = source or GitHub(
            config.source_token,
            timeout=config.http_timeout_seconds,
            retries=config.api_retries,
        )
        self.destination = destination or GitHub(
            config.destination_token,
            writable=True,
            timeout=config.http_timeout_seconds,
            retries=config.api_retries,
            protected_repository_ids=config.protected_destination_ids,
            cooldown_file=config.root / "destination-backoff.json",
            cooldown_seconds=config.destination_write_cooldown_seconds,
        )
        self.archive = archive or Archive(config)
        self.source_git = Git(self.source.token, config.command_timeout_seconds)
        self.destination_git = Git(
            self.destination.token,
            config.command_timeout_seconds,
            write_guard=lambda: check_write_cooldown(
                self.root / "destination-backoff.json"
            ),
        )
        self._audit_cache = {}

    def save(self):
        atomic_json(self.root / "state.json", self.state)

    def record_run(self, kind, errors):
        report = {"at": utcnow(), "kind": kind, "errors": errors}
        self.state["last_run"] = report
        runs = self.state.setdefault("runs", {})
        for category in ["git", "metadata"] if kind == "all" else [kind]:
            runs[category] = report
        self.save()
        try:
            from .telemetry import emit

            emit(self.root, kind, report)
        except Exception as exc:
            self.state["notification_warning"] = type(exc).__name__
            self.save()

    def recovery_files(self):
        recovery = self.root / "recovery"
        shutil.copytree(
            Path(__file__).parent,
            recovery / "ghbackup",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        spec = importlib.util.find_spec("github_backup")
        if spec and spec.submodule_search_locations:
            shutil.copytree(
                Path(next(iter(spec.submodule_search_locations))),
                recovery / "github_backup",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        # Recovery configuration must never contain credentials.
        values = self.c.public_values()
        text = (
            "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
            + "\n"
        )
        (recovery / "config.example.toml").write_text(text)
        (recovery / "RESTORE.txt").write_text(
            "Files are plain text/binary, not encrypted. The code directory is the default branch; "
            "Git history contains all captured branches, tags, PR refs and LFS objects. "
            "Use ghbackup snapshots and ghbackup restore to reconstruct a restore point from current files "
            "and older file versions. Google Drive access is required; source GitHub credentials are not.\n"
        )

    def identities(self, roles=("source", "destination")):
        for role in roles:
            user = getattr(self, role).get("/user")
            owner = getattr(self.c, role + "_owner")
            if user["login"].lower() != owner.lower() or user.get("type") != "User":
                raise BackupError(
                    f"{role} credential does not identify the configured personal account"
                )
            key = role + "_owner_id"
            if self.state.get(key, user["id"]) != user["id"]:
                raise BackupError(f"{role} account ID changed")
            self.state[key] = user["id"]
        if self.state.get("source_owner_id") == self.state.get("destination_owner_id"):
            raise BackupError("Source and destination account IDs are identical")
        self.save()

    def discover(self):
        # Complete pagination first. A failed/partial listing never means deletion.
        repos = [
            r
            for r in self.source.pages(
                "/user/repos", {"affiliation": "owner", "sort": "full_name"}
            )
            if r["owner"]["id"] == self.state["source_owner_id"]
        ]
        seen = set()
        for repo in repos:
            rid = str(int(repo["id"]))
            seen.add(rid)
            github_clone_url(repo)
            if self.c.include_repo_ids and int(rid) not in self.c.include_repo_ids:
                if rid in self.state["repositories"]:
                    self.state["repositories"][rid]["source_status"] = (
                        "excluded_preserved"
                    )
                continue
            entry = self.state["repositories"].setdefault(
                rid, {"aliases": [], "target_id": None}
            )
            old = entry.get("name")
            if old and old != repo["name"] and old not in entry["aliases"]:
                entry["aliases"].append(old)
            entry.update(
                name=repo["name"],
                full_name=repo["full_name"],
                source_status="accessible",
            )
            entry["source"] = repo
        for rid, entry in self.state["repositories"].items():
            if rid not in seen:
                entry["source_status"] = "not_listed_preserved"
        self.state["last_discovery"] = utcnow()
        self.save()
        return repos

    def checked_target(self, entry, *, disable_actions=False):
        path = f"/repos/{self.c.destination_owner}/{entry['target_name']}"
        target = self.destination.get(path)
        if target["id"] in self.c.protected_destination_ids:
            raise BackupError(
                "Existing destination repository is protected; no settings or code will be changed"
            )
        if (
            target["id"] != entry["target_id"]
            or target["owner"]["id"] != self.state["destination_owner_id"]
        ):
            raise BackupError("Destination repository identity mismatch")
        if target.get("fork"):
            raise BackupError(
                "Destination must be an independent repository, not a fork"
            )
        if not target.get("private"):
            self.destination.call("PATCH", path, {"private": True})
            # Stop this run even if repair worked: possible exposure deserves an alert.
            raise BackupError(
                "Destination was public; privacy repair requested; upload stopped"
            )
        if target.get("archived"):
            raise BackupError(
                "Destination is archived; preserving it without changing archive status"
            )
        if disable_actions:
            self.destination.call(
                "PUT", path + "/actions/permissions", {"enabled": False}
            )
        return target

    def ensure_target(self, rid, entry):
        wanted = backup_name(entry["name"], rid)
        marker = f"ghbackup source-id={rid} owner-id={self.state['source_owner_id']}"
        created = False
        if entry.get("target_id") is None:
            path = f"/repos/{self.c.destination_owner}/{wanted}"
            target = self.destination.optional(path)
            if target and target["id"] in self.c.protected_destination_ids:
                raise BackupError(
                    "Destination name belongs to a protected existing repository"
                )
            # Allows crash recovery between repository creation and state fsync;
            # never adopts unrelated existing repositories by name alone.
            if target is not None and target.get("description") != marker:
                raise BackupError(
                    "Destination name collision; refusing to adopt an unrelated repository"
                )
            if target is None:
                target = self.destination.call(
                    "POST",
                    "/user/repos",
                    {
                        "name": wanted,
                        "private": True,
                        "auto_init": False,
                        "description": marker,
                        "has_issues": False,
                        "has_wiki": False,
                        "has_projects": False,
                    },
                )
            created = True
            entry.update(target_id=target["id"], target_name=target["name"])
            self.save()
        target = self._audit_cache.get(rid)
        if (
            created
            or not target
            or target["id"] != entry["target_id"]
            or target["name"] != entry["target_name"]
        ):
            target = self.checked_target(entry, disable_actions=created)
        if target["name"] != entry["target_name"]:
            # GET may follow GitHub's rename redirect after a crash between PATCH
            # and local fsync. Reconcile by ID before issuing another mutation.
            entry["target_name"] = target["name"]
            self.save()
        if entry["target_name"] != wanted:
            collision = self.destination.optional(
                f"/repos/{self.c.destination_owner}/{wanted}"
            )
            if collision and collision["id"] != entry["target_id"]:
                raise BackupError("Rename collision; old destination preserved")
            target = self.destination.call(
                "PATCH",
                f"/repos/{self.c.destination_owner}/{entry['target_name']}",
                {"name": wanted},
            )
            if target["id"] != entry["target_id"]:
                raise BackupError("Repository identity changed during rename")
            entry["target_name"] = target["name"]
            self.save()
            target = self.checked_target(entry)
        return target

    def flush(self, rid, entry):
        pending = entry.get("pending")
        if not pending:
            return
        # begin() fsyncs intent before collection. A process can die before the
        # empty category/source paths are created; make that partial capture valid.
        folder = self.root / "repos" / rid
        (folder / pending["category"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (folder / "source.json").exists():
            atomic_json(folder / "source.json", entry["source"])
        if entry.get("drive_pending"):
            self.queue_capture(rid, entry)
            raise BackupError("Drive backlog retained; new capture queued in order")
        try:
            snapshot = self.archive.backup(
                rid, pending["category"], complete=pending["ready"]
            )
        except Exception as exc:
            self.queue_capture(rid, entry)
            message = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
            raise BackupError(message) from None

        if pending["ready"]:
            category = pending["category"]
            entry[category + "_snapshot"] = snapshot
            entry[category + "_archived_at"] = utcnow()
            if category == "git":
                entry["publish_pending"] = True
                entry["git_retry"] = False
            if category == "metadata":
                entry["metadata_force_full"] = False
                if pending.get("full"):
                    entry["metadata_full_at"] = utcnow()
        entry.pop("pending")
        self.save()

    def queue_capture(self, rid, entry):
        """Keep a durable local version while Drive is offline; GitHub can publish it."""
        pending = entry["pending"]
        queue = self.root / "drive-queue" / (".building-" + uuid.uuid4().hex)
        queue.mkdir(parents=True, mode=0o700)

        def copy_file(src, dst):
            if "/objects/" in str(src):
                os.link(src, dst)
            else:
                shutil.copy2(src, dst)

        category = pending["category"]
        target = queue / "repos" / rid / category
        shutil.copytree(
            self.root / "repos" / rid / category, target, copy_function=copy_file
        )
        shutil.copy2(
            self.root / "repos" / rid / "source.json", target.parent / "source.json"
        )
        if (self.root / "recovery").exists():
            shutil.copytree(self.root / "recovery", queue / "recovery")
        atomic_json(queue / "state.json", self.state)
        atomic_json(
            queue / "capture.json",
            {
                "at": pending["started_at"],
                "repo_id": rid,
                "category": category,
                "complete": pending["ready"],
            },
        )
        queue.rename(queue.with_name(queue.name.removeprefix(".building-")))
        if pending["ready"]:
            entry[category + "_local_capture"] = True
        entry["drive_pending"] = True
        if pending["ready"] and category == "metadata" and pending.get("content_hash"):
            entry["metadata_content_hash"] = pending["content_hash"]
        if pending["ready"] and category == "git":
            entry["git_retry"] = False
            entry["publish_pending"] = True
        entry.pop("pending")
        self.save()

    def drain_queue(self):
        folders = sorted(
            (
                p
                for p in (self.root / "drive-queue").glob("*")
                if not p.name.startswith(".")
            ),
            key=lambda p: p.stat().st_mtime_ns,
        )
        remaining = {}
        for folder in folders:
            rid = read_json(folder / "capture.json")["repo_id"]
            remaining[rid] = remaining.get(rid, 0) + 1
            self.state["repositories"][rid]["drive_pending"] = True
        self.save()
        for folder in folders:
            capture = read_json(folder / "capture.json")
            config = copy.copy(self.c)
            config.data_dir = str(folder)
            if capture["complete"]:
                verify_restored(folder, capture["repo_id"])
            snapshot = Archive(config).backup(
                capture["repo_id"], capture["category"], complete=capture["complete"]
            )
            entry = self.state["repositories"][capture["repo_id"]]
            if capture["complete"]:
                entry[capture["category"] + "_snapshot"] = snapshot
                entry[capture["category"] + "_archived_at"] = utcnow()
            remaining[capture["repo_id"]] -= 1
            if not remaining[capture["repo_id"]]:
                entry.pop("drive_pending", None)
            self.save()
            shutil.rmtree(folder)
        for entry in self.state["repositories"].values():
            entry.pop("drive_pending", None)
        self.save()

    def begin(self, rid, entry, category, *, full=False):
        if entry.get("pending"):
            raise BackupError("Unarchived previous capture; collection refused")
        if shutil.disk_usage(self.root).free < self.c.minimum_free_bytes:
            raise BackupError(
                "Insufficient free disk space; history will not be deleted"
            )
        (self.root / "repos" / rid).mkdir(parents=True, exist_ok=True, mode=0o700)
        entry["pending"] = {
            "category": category,
            "ready": False,
            "started_at": utcnow(),
            "full": full,
        }
        if category == "metadata":
            entry["metadata_force_full"] = True
        self.save()
        (self.root / "repos" / rid / category).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        atomic_json(self.root / "repos" / rid / "source.json", entry["source"])

    def ready(self, rid, entry):
        entry["pending"]["ready"] = True
        self.save()
        self.flush(rid, entry)

    def collect_git(self, rid, entry):
        repo = entry["source"]
        folder = self.root / "repos" / rid / "git"
        git_dir = folder / "repository.git"
        url = github_clone_url(repo)
        before = read_json(folder / "manifest.json", {})
        refs = self.source_git.remote_refs(url)
        wiki_url = url[:-4] + ".wiki.git"
        wiki_refs, wiki_status = {}, "disabled"
        if repo.get("has_wiki"):
            try:
                wiki_refs = self.source_git.remote_refs(wiki_url)
                wiki_status = "present" if wiki_refs else "empty"
            except BackupError:
                wiki_refs = before.get("wiki_refs", {})
                if before.get("wiki_refs") or (
                    int(rid) not in self.c.empty_wiki_ids
                    and not self.c.attest_uninitialized_wikis
                ):
                    # Code and wiki are collected separately below: preserve code even
                    # if the wiki's absence cannot be distinguished from lack of access.
                    wiki_status = "unavailable_or_uninitialized"
                else:
                    wiki_status = "attested_empty"
        expected = {
            "refs": refs,
            "wiki_refs": wiki_refs,
            "wiki_status": wiki_status,
            "default_branch": repo.get("default_branch"),
        }
        current = {k: before.get(k) for k in expected}
        if (
            expected == current
            and (entry.get("git_snapshot") or entry.get("git_local_capture"))
            and not entry.get("git_retry")
        ):
            if wiki_status == "unavailable_or_uninitialized":
                raise BackupError(
                    "Enabled wiki is inaccessible or uninitialized; explicit attestation required"
                )
            entry["git_checked_at"] = utcnow()
            self.save()
            return
        self.begin(rid, entry, "git")
        entry["git_retry"] = True
        self.save()
        self.source_git.fetch(url, git_dir, repo.get("default_branch"))
        lfs = self.source_git.lfs(git_dir)
        if wiki_refs and wiki_status == "present":
            self.source_git.fetch(wiki_url, folder / "wiki.git", "master")
            self.source_git.lfs(folder / "wiki.git")
            expected["wiki_refs"] = self.source_git.refs(folder / "wiki.git")
            if expected["wiki_refs"] != self.source_git.remote_refs(wiki_url):
                raise BackupError("Wiki refs changed during collection; retry required")
        atomic_json(
            folder / "manifest.json",
            {**expected, "refs": self.source_git.refs(git_dir), "lfs": lfs},
        )
        atomic_json(self.root / "repos" / rid / "source.json", repo)
        self.ready(rid, entry)
        if wiki_status == "unavailable_or_uninitialized":
            raise BackupError(
                "Code archived; enabled wiki inaccessible or uninitialized"
            )
        entry["git_checked_at"] = utcnow()
        self.save()

    def collect_metadata(self, rid, entry, *, full=False):
        existing = self.root / "repos" / rid / "metadata"
        if (existing / "checksums.json").exists():
            verify_checksums(existing)
        full = (
            full
            or entry.get("metadata_force_full", False)
            or age_seconds(entry.get("metadata_full_at")) >= 86400
        )
        self.begin(rid, entry, "metadata", full=full)
        root = self.root / "repos" / rid
        atomic_json(root / "source.json", entry["source"])
        metadata = root / "metadata"
        output = metadata / "export"
        args = [
            sys.executable,
            "-m",
            "ghbackup.exporter",
            root / "source.json",
            output,
            "--config",
            self.c.private_config_path,
        ]
        if full:
            args.append("--full")
        try:
            run(args, timeout=self.c.command_timeout_seconds)
        except BackupError:
            diagnostic = read_json(root / "metadata-diagnostic.json", {})
            if diagnostic.get("failed") and diagnostic.get("messages"):
                raise BackupError(
                    "Metadata export incomplete: " + diagnostic["messages"][0][:200]
                ) from None
            raise
        backup_releases(self.source, entry["source"], metadata / "releases")
        atomic_json(
            metadata / "coverage.json",
            {
                "collected_at": utcnow(),
                "full_refresh": full,
                "mode": "archive; old deleted items retained",
                "unsupported": UNSUPPORTED,
            },
        )
        # Include coverage.json itself in the checksum inventory.
        checksums = validate_metadata(metadata)
        content = {
            name: digest
            for name, digest in checksums.items()
            if Path(name).name
            not in {"last_update", "reviews_last_update", "coverage.json"}
        }
        import hashlib

        signature = hashlib.sha256(
            json.dumps([entry["source"], content], sort_keys=True).encode()
        ).hexdigest()
        if (
            entry.get("metadata_snapshot") or entry.get("metadata_local_capture")
        ) and entry.get("metadata_content_hash") == signature:
            entry.pop("pending")
            entry["metadata_force_full"] = False
        else:
            entry["pending"]["content_hash"] = signature
            self.ready(rid, entry)
            entry["metadata_content_hash"] = signature
        entry["metadata_checked_at"] = utcnow()
        if full:
            entry["metadata_full_at"] = utcnow()
        self.save()

    def publish(self, rid, entry):
        if not entry.get("publish_pending"):
            return
        if (
            not (entry.get("git_snapshot") or entry.get("git_local_capture"))
            or entry.get("pending")
            or entry.get("git_retry")
        ):
            raise BackupError("Publishing requires a completed durable capture")
        target = self.ensure_target(rid, entry)
        folder = self.root / "repos" / rid / "git" / "repository.git"
        manifest = read_json(folder.parent / "manifest.json")
        if self.source_git.refs(folder) != manifest["refs"]:
            raise BackupError("Local refs changed after archival")
        url = github_clone_url(target)
        branch = manifest.get("default_branch")
        expected = {
            k: v
            for k, v in manifest["refs"].items()
            if k.startswith(("refs/heads/", "refs/tags/"))
        }
        if (
            entry.get("published_at")
            and not manifest["lfs"]
            and target.get("default_branch") == branch
        ):
            actual = {
                k: v
                for k, v in self.destination_git.remote_refs(url).items()
                if k.startswith(("refs/heads/", "refs/tags/"))
            }
            if actual == expected:
                # A wiki/manifest-only archive does not require another write to
                # GitHub. Freshly verify privacy and identity, then acknowledge it.
                self.checked_target(entry)
                entry["publish_pending"] = False
                entry["published_at"] = utcnow()
                self.save()
                return
        target = self.checked_target(entry, disable_actions=True)
        # Set destination LFS URL explicitly: origin points at the source account.
        if manifest["lfs"]:
            self.destination_git.push_lfs(folder, url)
        path = f"/repos/{self.c.destination_owner}/{entry['target_name']}"
        # GitHub refuses deletion of its current default branch. Publish the new
        # default first, change the setting, then prune all captured branches/tags.
        if branch and f"refs/heads/{branch}" in manifest["refs"]:
            self.destination_git.command(
                "-C", folder, "push", url, f"+refs/heads/{branch}:refs/heads/{branch}"
            )
            self.destination.call("PATCH", path, {"default_branch": branch})
        self.checked_target(entry)
        self.destination_git.push(folder, url, branch)
        self.checked_target(entry)
        entry["publish_pending"] = False
        entry["published_at"] = utcnow()
        self.save()

    def sync(self, kind="all", *, full=False):
        self.recovery_files()
        errors = []
        self._audit_cache = {}
        try:
            self.archive.available()
            self.drain_queue()
        except Exception as exc:
            errors.append(
                {
                    "stage": "archive",
                    "error": str(exc)
                    if isinstance(exc, BackupError)
                    else type(exc).__name__,
                }
            )
        target_ok = True
        try:
            self.identities(("destination",))
        except Exception as exc:
            target_ok = False
            errors.append(
                {
                    "stage": "target",
                    "error": str(exc)
                    if isinstance(exc, BackupError)
                    else type(exc).__name__,
                }
            )
        # Upload queued generations even if the original account is now banned.
        for rid, entry in self.state["repositories"].items():
            try:
                self.flush(rid, entry)
            except BackupError as exc:
                errors.append({"repo_id": rid, "stage": "archive", "error": str(exc)})
        target_blocked = set()
        # Audit only affects publication, never source collection.
        for rid, entry in self.state["repositories"].items():
            if target_ok and entry.get("target_id"):
                try:
                    self._audit_cache[rid] = self.checked_target(entry)
                except Exception as exc:
                    message = (
                        str(exc) if isinstance(exc, BackupError) else type(exc).__name__
                    )
                    target_blocked.add(rid)
                    errors.append(
                        {"repo_id": rid, "stage": "privacy", "error": message}
                    )
        try:
            self.identities(("source",))
            self.discover()
        except Exception as exc:
            errors.append(
                {
                    "stage": "discovery",
                    "error": str(exc)
                    if isinstance(exc, BackupError)
                    else type(exc).__name__,
                }
            )
            self.record_run(kind, errors)
            raise BackupError(
                "Source/destination identity or inventory unavailable; existing backups preserved"
            ) from None
        blocked = {
            e["repo_id"]
            for e in errors
            if e.get("repo_id")
            and e["stage"] == "archive"
            and self.state["repositories"][e["repo_id"]].get("pending")
        }
        for rid, entry in self.state["repositories"].items():
            if rid in blocked:
                continue
            try:
                self.flush(rid, entry)
            except BackupError as exc:
                errors.append({"repo_id": rid, "stage": "archive", "error": str(exc)})
                continue
            if entry["source_status"] != "accessible":
                if entry["source_status"] == "excluded_preserved":
                    continue
                errors.append(
                    {
                        "repo_id": rid,
                        "stage": "discovery",
                        "error": "Source not listed; backups retained",
                    }
                )
                continue
            try:
                if not target_ok or rid in target_blocked:
                    raise BackupError("Target unavailable; source collection continues")
                self.ensure_target(rid, entry)
                target_available = True
            except Exception as exc:
                message = (
                    str(exc) if isinstance(exc, BackupError) else type(exc).__name__
                )
                errors.append({"repo_id": rid, "stage": "target", "error": message})
                target_available = False
                entry["publish_pending"] = True
                self.save()
                # A destination outage must not prevent off-site collection.
            for category in ["git", "metadata"] if kind == "all" else [kind]:
                try:
                    if category == "git":
                        self.collect_git(rid, entry)
                        if (
                            target_available
                            and entry.get("git_snapshot")
                            and entry.get("target_id")
                        ):
                            target = self._audit_cache.get(rid) or self.checked_target(
                                entry
                            )
                            expected = {
                                k: v
                                for k, v in self.source_git.refs(
                                    self.root / "repos" / rid / "git" / "repository.git"
                                ).items()
                                if k.startswith(("refs/heads/", "refs/tags/"))
                            }
                            actual = {
                                k: v
                                for k, v in self.destination_git.remote_refs(
                                    github_clone_url(target)
                                ).items()
                                if k.startswith(("refs/heads/", "refs/tags/"))
                            }
                            if expected != actual:
                                entry["publish_pending"] = True
                                self.save()
                    else:
                        self.collect_metadata(rid, entry, full=full)
                except Exception as exc:
                    message = (
                        str(exc) if isinstance(exc, BackupError) else type(exc).__name__
                    )
                    errors.append({"repo_id": rid, "stage": category, "error": message})
                    # Preserve partially collected data before any subsequent mutation.
                    try:
                        self.flush(rid, entry)
                    except BackupError:
                        continue
                try:
                    if target_available:
                        self.publish(rid, entry)
                except Exception as exc:
                    message = (
                        str(exc) if isinstance(exc, BackupError) else type(exc).__name__
                    )
                    errors.append(
                        {"repo_id": rid, "stage": "publish", "error": message}
                    )
        self.record_run(kind, errors)
        if errors:
            raise BackupError(
                f"{len(errors)} backup stage(s) incomplete; see status for repository IDs"
            )


def verify_restored(root: Path, repo_id: str):
    repo = root / "repos" / repo_id
    if not repo.is_dir():
        raise BackupError("Restored repository directory missing")
    git = Git()
    manifest = read_json(repo / "git" / "manifest.json")
    if manifest:
        folder = repo / "git" / "repository.git"
        git.command("-C", folder, "fsck", "--full", "--no-dangling")
        if (
            git.refs(folder) != manifest["refs"]
            or git.verify_lfs(folder) != manifest["lfs"]
        ):
            raise BackupError("Restored Git/LFS manifest mismatch")
        if manifest.get("wiki_refs"):
            wiki = repo / "git" / "wiki.git"
            git.command("-C", wiki, "fsck", "--full", "--no-dangling")
            if git.refs(wiki) != manifest["wiki_refs"]:
                raise BackupError("Restored wiki manifest mismatch")
            git.verify_lfs(wiki)
    if (repo / "metadata").exists():
        verify_checksums(repo / "metadata")
