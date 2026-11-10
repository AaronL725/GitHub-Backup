from __future__ import annotations

from pathlib import Path
import re
import shlex
import sys

from .common import BackupError, clean_env, run, sha256


REFSPECS = [
    "+refs/heads/*:refs/heads/*",
    "+refs/tags/*:refs/tags/*",
    "+refs/notes/*:refs/notes/*",
    "+refs/pull/*:refs/backup/pr/*",
]


def github_clone_url(repo):
    full_name = repo["full_name"]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", full_name):
        raise BackupError("Invalid repository identity")
    if full_name != repo["owner"]["login"] + "/" + repo["name"] or repo["name"] in (
        ".",
        "..",
    ):
        raise BackupError("Repository fields disagree")
    expected = "https://github.com/" + full_name + ".git"
    if repo.get("clone_url", expected) != expected:
        raise BackupError("Clone URL does not match GitHub repository identity")
    return expected


class Git:
    def __init__(self, token="", timeout=14400, write_guard=None):
        self.timeout = timeout
        self.write_guard = write_guard
        self.env = clean_env()
        self.env.update(
            GHBACKUP_TOKEN=token,
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="credential.helper",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="credential.helper",
            GIT_CONFIG_VALUE_1="!"
            + shlex.join(
                [sys.executable, str(Path(__file__).with_name("credential_helper.py"))]
            ),
        )

    def command(self, *args, cwd=None):
        if self.write_guard and "push" in args:
            self.write_guard()
        return run(
            [
                "git",
                "-c",
                "gc.auto=0",
                "-c",
                "maintenance.auto=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            env=self.env,
            cwd=cwd,
            timeout=self.timeout,
        )

    def refs(self, folder: Path):
        if not folder.exists():
            return {}
        output = self.command(
            "-C", folder, "for-each-ref", "--format=%(refname) %(objectname)"
        )
        return dict(line.split(" ", 1) for line in output.splitlines())

    def remote_refs(self, url):
        output = self.command("ls-remote", "--refs", url)
        refs = {}
        for line in output.splitlines():
            oid, name = line.split("\t", 1)
            if name.startswith(
                ("refs/heads/", "refs/tags/", "refs/notes/", "refs/pull/")
            ):
                refs[name.replace("refs/pull/", "refs/backup/pr/", 1)] = oid
        return refs

    def fetch(self, url: str, folder: Path, default_branch=None):
        if not folder.exists():
            self.command("init", "--bare", folder)
            self.command("-C", folder, "remote", "add", "origin", url)
            self.command("-C", folder, "config", "--unset-all", "remote.origin.fetch")
            for spec in REFSPECS:
                self.command(
                    "-C", folder, "config", "--add", "remote.origin.fetch", spec
                )
        else:
            self.command("-C", folder, "remote", "set-url", "origin", url)
        self.command(
            "-C",
            folder,
            "fetch",
            "--force",
            "--prune",
            "--no-recurse-submodules",
            "origin",
            *REFSPECS,
        )
        if default_branch and f"refs/heads/{default_branch}" in self.refs(folder):
            self.command(
                "-C", folder, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}"
            )
        self.command("-C", folder, "fsck", "--full", "--no-dangling")
        if self.refs(folder) != self.remote_refs(url):
            raise BackupError("Source refs changed during collection; retry required")

    def lfs(self, folder: Path):
        self.command("-C", folder, "lfs", "fetch", "--all", "origin")
        return self.verify_lfs(folder)

    def verify_lfs(self, folder: Path):
        output = self.command("-C", folder, "lfs", "ls-files", "--all", "--long")
        oids = sorted(set(line.split(" ", 1)[0] for line in output.splitlines()))
        for oid in oids:
            if not re.fullmatch(r"[0-9a-f]{64}", oid):
                raise BackupError("Invalid LFS object identity")
            obj = folder / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
            if not obj.is_file() or sha256(obj) != oid:
                raise BackupError("Missing or corrupt LFS object")
        return oids

    def push_lfs(self, folder: Path, url: str):
        options = ["-c", "remote.ghbackup.url=" + url]
        if url.startswith("https://"):
            endpoint = url + "/info/lfs"
            options += [
                "-c",
                "lfs.url=" + endpoint,
                "-c",
                "lfs.pushurl=" + endpoint,
                "-c",
                "remote.ghbackup.lfspushurl=" + endpoint,
            ]
        self.command("-C", folder, *options, "lfs", "push", "--all", "ghbackup")

    def push(self, folder: Path, url: str, default_branch: str | None):
        # Only these two namespaces can be overwritten. PR/history refs stay in archives.
        args = [
            "-C",
            folder,
            "push",
            "--atomic",
            "--force",
            "--prune",
            url,
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
        ]
        if self.refs(folder) or self.remote_refs(url):
            self.command(*args)
        expected = {
            k: v
            for k, v in self.refs(folder).items()
            if k.startswith(("refs/heads/", "refs/tags/"))
        }
        actual = {
            k: v
            for k, v in self.remote_refs(url).items()
            if k.startswith(("refs/heads/", "refs/tags/"))
        }
        if actual != expected:
            raise BackupError("Destination refs do not match captured refs")
