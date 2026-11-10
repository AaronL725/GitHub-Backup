"""Strict wrapper around the pinned upstream metadata exporter, not a new exporter."""

from __future__ import annotations

import argparse
import logging
import re
import os
from pathlib import Path
import sys

from .common import BackupError, Config, atomic_json, read_json, sha256


def recover_rename(output):
    journal = output.parent / "metadata-rename.json"
    if not journal.exists():
        return
    intent = read_json(journal)
    old, new = (output / "repositories" / intent[k] for k in ("old", "new"))
    if old.exists():
        old.rename(new)
        fd = os.open(new.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    for relative, checksums in intent["inventories"].items():
        manifest = output.parent / relative
        for name, digest in checksums.items():
            file = manifest.parent / name
            if not file.is_file() or sha256(file) != digest:
                raise BackupError(
                    "Interrupted metadata rename failed integrity verification"
                )
        atomic_json(manifest, checksums)
    journal.unlink()
    fd = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_export(output, name):
    recover_rename(output)
    repos = output / "repositories"
    previous = [p for p in repos.iterdir() if p.is_dir()] if repos.exists() else []
    if len(previous) > 1:
        raise BackupError("Ambiguous metadata directories; refusing to merge")
    if not previous or previous[0].name == name:
        return
    inventories = {}
    for manifest in (output / "checksums.json", output.parent / "checksums.json"):
        if manifest.exists():
            verify_checksums(manifest.parent)
            prefix = str(previous[0].relative_to(manifest.parent)) + "/"
            replacement = str((repos / name).relative_to(manifest.parent)) + "/"
            inventories[str(manifest.relative_to(output.parent))] = {
                replacement + key[len(prefix) :]
                if key.startswith(prefix)
                else key: digest
                for key, digest in read_json(manifest).items()
            }
    atomic_json(
        output.parent / "metadata-rename.json",
        {
            "old": previous[0].name,
            "new": name,
            "inventories": inventories,
        },
    )
    recover_rename(output)


def validate_metadata(root: Path):
    checksums = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError("Symlinks are forbidden in metadata exports")
        if path.is_file():
            if path.name.endswith((".temp", ".partial")):
                raise BackupError("Unfinished metadata file")
            if path.suffix == ".json":
                data = read_json(path)
                if path.name == "manifest.json" and isinstance(data, dict):
                    for item in data.get("attachments", []):
                        if item.get("success") is not True:
                            raise BackupError("An attachment was not downloaded")
                        name = item.get("saved_as")
                        attachment = path.parent / (name or "")
                        if (
                            not name
                            or Path(name).name != name
                            or not attachment.is_file()
                            or attachment.is_symlink()
                        ):
                            raise BackupError(
                                "Attachment manifest references a missing/unsafe file"
                            )
                        if (
                            item.get("size_bytes") is not None
                            and attachment.stat().st_size != item["size_bytes"]
                        ):
                            raise BackupError("Attachment size mismatch")
            if path.name != "checksums.json":
                checksums[str(path.relative_to(root))] = sha256(path)
    atomic_json(root / "checksums.json", checksums)
    return checksums


def verify_checksums(root: Path):
    checksums = read_json(root / "checksums.json")
    if checksums is None:
        raise BackupError("Metadata checksum manifest missing")
    for name, digest in checksums.items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise BackupError("Unsafe metadata checksum path")
        if not path.is_file() or sha256(path) != digest:
            raise BackupError("Metadata file missing or checksum mismatch")


def export(repo_file: Path, output: Path, token: str, *, full=False):
    from github_backup import github_backup as upstream

    repo = read_json(repo_file)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    recover_rename(output)
    # Supply exact repository identity to the existing exporter, avoiding an extra
    # account-wide scan and never using --all (hooks require broader permissions).
    token_option = "-f" if token.startswith("github_pat_") else "-t"
    flags = [
        repo["owner"]["login"],
        token_option,
        token,
        "-P",
        "-F",
        "-R",
        repo["name"],
        "-o",
        str(output),
        "--pulls",
        "--pull-comments",
        "--pull-reviews",
        "--pull-commits",
        "--pull-details",
        "--attachments",
    ]
    if repo.get("has_issues", True):
        flags += [
            "--issues",
            "--issue-comments",
            "--issue-events",
            "--issue-timeline",
            "--labels",
            "--milestones",
        ]
    if repo.get("has_discussions"):
        flags.append("--discussions")
    if not full:
        flags.append("--incremental")
    args = upstream.parse_args(flags)
    if (output / "checksums.json").exists():
        verify_checksums(output)
    rename_export(output, repo["name"])

    class Errors(logging.Handler):
        failed = False

        def __init__(self):
            super().__init__()
            self.messages = []

        def sanitize(self, value):
            value = str(value).replace(token, "[REDACTED]")
            value = value.replace(repo["full_name"], "[repository]")
            if len(repo["name"]) > 3:
                value = value.replace(repo["name"], "[repository]")
            return re.sub(r"https?://\S+", "[URL]", value)[:400]

        def emit(self, record):
            if record.levelno >= logging.WARNING:
                self.failed = True
                if len(self.messages) < 20:
                    self.messages.append(self.sanitize(record.getMessage()))

    handler = Errors()
    upstream.logger.addHandler(handler)
    original_subprocess = upstream.logging_subprocess
    original_attachment = upstream.download_attachment_file

    def checked_process(*args, **kwargs):
        rc = original_subprocess(*args, **kwargs)
        if rc:
            raise BackupError("Upstream subprocess failed")
        return rc

    def confined_attachment(url, path, *args, **kwargs):
        p = Path(path)
        if not p.resolve().is_relative_to(output.resolve()) or p.is_symlink():
            raise BackupError("Unsafe upstream attachment path")
        return original_attachment(url, path, *args, **kwargs)

    upstream.logging_subprocess = checked_process
    upstream.download_attachment_file = confined_attachment
    # Failed permanent attachment entries must be retried rather than cached forever.
    previous_checksums = read_json(output / "checksums.json", {})
    for manifest in output.rglob("manifest.json"):
        data = read_json(manifest)
        if isinstance(data, dict) and "attachments" in data:
            valid = []
            for item in data["attachments"]:
                name = item.get("saved_as")
                file = manifest.parent / (name or "")
                key = str(file.relative_to(output))
                intact = (
                    name
                    and Path(name).name == name
                    and file.is_file()
                    and not file.is_symlink()
                    and (
                        key not in previous_checksums
                        or sha256(file) == previous_checksums[key]
                    )
                )
                if item.get("success") is True and intact:
                    valid.append(item)
                else:
                    # A corrupt/missing old attachment must be revisited even when
                    # its issue's updated_at did not change.
                    args.incremental = False
            data["attachments"] = valid
            atomic_json(manifest, data)
    # Always re-parse successful files, including after an interrupted earlier run.
    try:
        upstream.backup_repositories(args, str(output), [repo])
        validate_metadata(output)
        if handler.failed:
            raise BackupError(
                "Metadata exporter reported warnings/errors; full retry required"
            )
        atomic_json(
            repo_file.with_name("metadata-diagnostic.json"),
            {"failed": False, "messages": []},
        )
    except Exception as exc:
        messages = handler.messages or [
            handler.sanitize(type(exc).__name__ + ": " + str(exc))
        ]
        atomic_json(
            repo_file.with_name("metadata-diagnostic.json"),
            {"failed": True, "messages": messages},
        )
        raise
    finally:
        upstream.logging_subprocess = original_subprocess
        upstream.download_attachment_file = original_attachment
        upstream.logger.removeHandler(handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_file", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", default="/run/secrets/ghbackup.toml")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    # Suppress upstream raw URLs/response bodies: only generic failure reaches logs.
    logging.getLogger().handlers.clear()
    logging.getLogger().addHandler(logging.NullHandler())
    try:
        export(
            args.repo_file,
            args.output,
            Config.load(args.config).source_token,
            full=args.full,
        )
    except Exception:
        print(
            "Metadata collection incomplete; inspect coverage status and permissions",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
