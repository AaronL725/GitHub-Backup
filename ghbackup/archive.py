"""Readable current files, rclone previous-file history, checksummed restore points."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from .common import BackupError, atomic_json, clean_env, read_json, sha256
from .git import Git

FORMAT = "ghbackup-plain-files-v1"


def hashes(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
            md5.update(block)
    return {
        "sha256": sha.hexdigest(),
        "md5": md5.hexdigest(),
        "size": path.stat().st_size,
    }


def export_code(repository, branch, output):
    """Read committed blobs without hooks, checkout filters, or following links."""
    output.mkdir(parents=True, exist_ok=True)
    git = Git()
    if not branch or f"refs/heads/{branch}" not in git.refs(repository):
        return
    tree = git.command("-C", repository, "ls-tree", "-rz", "--full-tree", branch)
    entries = [entry.split("\t", 1) for entry in tree.split("\0") if entry]
    process = subprocess.Popen(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=clean_env(),
    )
    try:
        for header, name in entries:
            mode, kind, oid = header.split()
            path = output / name
            if not path.resolve().is_relative_to(output.resolve()):
                raise BackupError("Unsafe Git tree path")
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "commit":
                path.with_name(path.name + ".submodule.txt").write_text(
                    "Submodule commit: " + oid + "\n"
                )
                continue
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            _, _, length = process.stdout.readline().decode().split()
            remaining = int(length)
            if mode == "120000":
                path = path.with_name(path.name + ".symlink.txt")
            with path.open("wb") as stream:
                while remaining:
                    block = process.stdout.read(min(1024 * 1024, remaining))
                    if not block:
                        raise BackupError("Git blob export ended early")
                    stream.write(block)
                    remaining -= len(block)
            process.stdout.read(1)
            if mode != "120000" and path.stat().st_size < 1024:
                import re

                match = re.fullmatch(
                    rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize [0-9]+\n?",
                    path.read_bytes(),
                )
                if match:
                    oid = match[1].decode()
                    shutil.copyfile(
                        repository / "lfs/objects" / oid[:2] / oid[2:4] / oid, path
                    )
    finally:
        process.stdin.close()
        process.wait()
        process.stdout.close()


class Archive:
    def __init__(self, config):
        self.config = config
        self.remote = config.drive_repository.rstrip("/")
        self.env = clean_env()
        if config.drive_client_id:
            self.env["RCLONE_CONFIG"] = str(config.rclone_config())
        self.env["RCLONE_CACHE_DIR"] = str(config.root / ".cache/rclone")

    def path(self, relative):
        return self.remote + "/" + str(relative).lstrip("/")

    def command(self, *args, missing=False):
        result = subprocess.run(
            ["rclone", *map(str, args)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=self.config.command_timeout_seconds,
        )
        if missing and result.returncode in (3, 4):
            return None
        if result.returncode:
            raise BackupError(
                f"Drive operation failed (rclone exit {result.returncode})"
            )
        return result.stdout

    def json(self, relative, default=None):
        value = self.command("cat", self.path(relative), missing=True)
        return json.loads(value) if value is not None else default

    def upload_json(self, relative, data, workspace):
        file = workspace / (uuid.uuid4().hex + ".json")
        atomic_json(file, data)
        self.command("copyto", file, self.path(relative))

    def available(self):
        marker = self.json("_系统/format.json")
        if not marker or marker.get("format") != FORMAT:
            raise BackupError("Plain Drive archive is not initialized")

    def init(self):
        listing = self.command("lsjson", self.remote, "--max-depth", "1", missing=True)
        if listing and json.loads(listing):
            raise BackupError("Drive destination is not empty; initialization stopped")
        with tempfile.TemporaryDirectory(dir=self.config.root) as t:
            root = Path(t)
            self.upload_json(
                "_系统/format.json", {"format": FORMAT, "encrypted": False}, root
            )
            self.upload_json("_系统/repos.json", {}, root)
            info = root / "README.md"
            info.write_text(
                "# GitHub 备份\n\n每个仓库单独存放：代码、元数据和 Git历史是当前文件；历史保存被替换的旧文件；恢复点保存校验清单。\n\n"
                "文件未加密，可直接查看。初次全量，之后只传新增或改变的文件。不手工修改历史和恢复点。\n"
            )
            self.command("copyto", info, self.path("README.md"))

    def repo_folder(self, repo, workspace):
        catalog = self.json("_系统/repos.json", {})
        rid = str(repo["id"])
        wanted = repo["name"] + "--" + rid
        old = catalog.get(rid, wanted)
        if old != wanted and self.command(
            "lsjson", self.path(old), "--stat", missing=True
        ):
            self.command("moveto", self.path(old), self.path(wanted))
        catalog[rid] = wanted
        self.upload_json("_系统/repos.json", catalog, workspace)
        return wanted

    def sync(self, local, remote, history):
        self.command(
            "sync",
            local,
            self.path(remote),
            "--checksum",
            "--no-update-modtime",
            "--backup-dir",
            self.path(history),
            "--create-empty-src-dirs",
            "--transfers",
            "4",
            "--checkers",
            "8",
            "--tpslimit",
            "8",
        )
        self.command("check", local, self.path(remote), "--one-way", "--checksum")

    def backup(self, repo_id, category, *, complete):
        source = self.config.root / "repos" / repo_id
        repo = read_json(source / "source.json")
        identifier = uuid.uuid4().hex
        now = dt.datetime.now(dt.timezone.utc)
        stamp = (
            now.astimezone().strftime("%Y-%m-%d/%H%M%S")
            + "-"
            + category
            + "-"
            + identifier[:8]
        )
        with tempfile.TemporaryDirectory(prefix=".plain-", dir=self.config.root) as t:
            workspace = Path(t)
            stage = workspace / "files"
            stage.mkdir()
            folder = self.repo_folder(repo, workspace)
            mapping = {}
            directories = []
            if category == "git":
                dest = stage / "Git历史"
                dest.mkdir()
                for item in (source / "git").rglob("*"):
                    rel = item.relative_to(source / "git")
                    out = dest / rel
                    if "hooks" in rel.parts or tuple(rel.parts[1:3]) in [
                        ("lfs", "logs"),
                        ("lfs", "tmp"),
                    ]:
                        continue
                    if item.name == "FETCH_HEAD":
                        continue
                    if item.is_symlink():
                        raise BackupError("Unexpected symlink in Git storage")
                    if item.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                        directories.append("git/" + str(rel))
                        continue
                    if item.name.endswith(".lock"):
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    os.link(item, out)
                    mapping[str(out.relative_to(stage))] = "git/" + str(rel)
                if complete:
                    export_code(
                        source / "git/repository.git",
                        repo.get("default_branch"),
                        stage / "代码",
                    )
                    for item in (stage / "代码").rglob("*"):
                        if item.is_file():
                            mapping[str(item.relative_to(stage))] = "git/code/" + str(
                                item.relative_to(stage / "代码")
                            )
            else:
                metadata = source / "metadata"
                exported = metadata / "export/repositories" / repo["name"]
                for item in metadata.rglob("*"):
                    if not item.is_file():
                        continue
                    if item.is_relative_to(exported):
                        rel = item.relative_to(exported)
                    elif item.is_relative_to(metadata / "releases"):
                        rel = Path("releases") / item.relative_to(metadata / "releases")
                    else:
                        rel = Path("_校验资料") / item.relative_to(metadata)
                    out = stage / "元数据" / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    os.link(item, out)
                    mapping[str(out.relative_to(stage))] = "metadata/" + str(
                        item.relative_to(metadata)
                    )
            files = {
                key: dict(hashes(stage / key), original=value)
                for key, value in mapping.items()
            }
            prefixes = (
                ["Git历史", "代码"]
                if category == "git" and complete
                else ["Git历史"]
                if category == "git"
                else ["元数据"]
            )
            current = "" if complete else "未完成采集/" + category + "/"
            for prefix in prefixes:
                directory = stage / prefix
                directory.mkdir(parents=True, exist_ok=True)
                self.sync(
                    directory,
                    folder + "/" + current + prefix,
                    folder + "/历史/" + stamp + "/" + current + prefix,
                )
            control = "恢复点/" + stamp
            for name, local in [
                ("source.json", source / "source.json"),
                ("state.json", self.config.root / "state.json"),
            ]:
                self.command(
                    "copyto", local, self.path(folder + "/" + control + "/" + name)
                )
            manifest = {
                "id": identifier,
                "repo_id": repo_id,
                "category": category,
                "complete": complete,
                "time": now.isoformat(),
                "current_prefix": current,
                "control": control,
                "files": files,
                "directories": directories,
            }
            self.upload_json(
                folder + "/" + control + "/manifest.json", manifest, workspace
            )
            summary = {
                k: manifest[k]
                for k in ["id", "repo_id", "category", "complete", "time", "control"]
            }
            self.upload_json("_系统/index/" + identifier + ".json", summary, workspace)
            recovery = self.config.root / "recovery"
            if recovery.exists():
                self.command(
                    "copy",
                    recovery,
                    self.path("_系统/recovery"),
                    "--checksum",
                    "--no-update-modtime",
                )
        return identifier

    def snapshots(self):
        listing = self.command(
            "lsjson", self.path("_系统/index"), "--files-only", missing=True
        )
        rows = [
            self.json("_系统/index/" + item["Path"])
            for item in json.loads(listing or "[]")
        ]
        return sorted(rows, key=lambda r: r["time"])

    def restore(self, repo_id, target, snapshot="latest", *, category=None):
        if target.exists():
            raise BackupError("Restore target must not exist")
        rows = [
            r
            for r in self.snapshots()
            if r["repo_id"] == repo_id
            and r["complete"]
            and (snapshot == "latest" or r["id"].startswith(snapshot))
            and (category is None or r["category"] == category)
        ]
        if not rows:
            raise BackupError("No matching complete restore point")
        kinds = (
            [category]
            if category
            else ["git", "metadata"]
            if snapshot == "latest"
            else sorted({r["category"] for r in rows})
        )
        if any(not any(r["category"] == kind for r in rows) for kind in kinds):
            raise BackupError(
                "Both Git and metadata are required; select a category to restore only one"
            )
        chosen = [
            max((r for r in rows if r["category"] == kind), key=lambda r: r["time"])
            for kind in kinds
        ]
        folder = self.json("_系统/repos.json")[repo_id]
        target.mkdir(parents=True)
        for row in chosen:
            manifest = self.json(folder + "/" + row["control"] + "/manifest.json")
            history = None
            with tempfile.TemporaryDirectory(dir=self.config.root) as t:
                stage = Path(t)
                for prefix in {key.split("/")[0] for key in manifest["files"]}:
                    self.command(
                        "copy",
                        self.path(folder + "/" + manifest["current_prefix"] + prefix),
                        stage / prefix,
                        "--create-empty-src-dirs",
                        missing=True,
                    )
                for key, info in manifest["files"].items():
                    file = stage / key
                    out = target / "repos" / repo_id / info["original"]
                    if not out.resolve().is_relative_to(target.resolve()):
                        raise BackupError("Invalid restore path")
                    if not file.is_file() or sha256(file) != info["sha256"]:
                        if history is None:
                            history = json.loads(
                                self.command(
                                    "lsjson",
                                    self.path(folder + "/历史"),
                                    "--recursive",
                                    "--files-only",
                                    "--hash",
                                )
                                or "[]"
                            )
                        found = [
                            f
                            for f in history
                            if f["Path"].endswith(
                                "/" + manifest["current_prefix"] + key
                            )
                            and {
                                k.lower(): v for k, v in f.get("Hashes", {}).items()
                            }.get("md5")
                            == info["md5"]
                        ]
                        if not found:
                            raise BackupError("Historical file not found")
                        file.parent.mkdir(parents=True, exist_ok=True)
                        self.command(
                            "copyto",
                            self.path(folder + "/历史/" + found[-1]["Path"]),
                            file,
                        )
                    if sha256(file) != info["sha256"]:
                        raise BackupError("Restored file checksum mismatch")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(file, out)
                for relative in manifest.get("directories", []):
                    (target / "repos" / repo_id / relative).mkdir(
                        parents=True, exist_ok=True
                    )
                self.command(
                    "copy", self.path(folder + "/" + row["control"]), stage / "control"
                )
                shutil.copy2(
                    stage / "control/source.json",
                    target / "repos" / repo_id / "source.json",
                )
                shutil.copy2(stage / "control/state.json", target / "state.json")
        return [r["id"] for r in chosen]

    def check(self):
        rows = [r for r in self.snapshots() if r["complete"]]
        if not rows:
            raise BackupError("No completed restore point to verify")
        ids = sorted({row["repo_id"] for row in rows})
        cursor = self.config.root / "plain-verification.json"
        index = (read_json(cursor, {}).get("index", -1) + 1) % len(ids)
        with tempfile.TemporaryDirectory(dir=self.config.root) as t:
            target = Path(t) / "verify"
            self.restore(ids[index], target)
            from .controller import verify_restored

            verify_restored(target, ids[index])
        atomic_json(cursor, {"index": index, "repo_id": ids[index]})
