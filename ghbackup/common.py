from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import tomllib


class BackupError(RuntimeError):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    if path.exists() and path.read_bytes() == data:
        return
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def secret(path: str):
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        raise BackupError(f"Secret must be a regular file: {p}")
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        raise BackupError(f"Secret permissions must be 0600 or 0400: {p}")
    value = p.read_text().strip()
    if not value:
        raise BackupError(f"Empty secret file: {p}")
    return value


def clean_env():
    # Do not inherit another tool's credentials, hooks, remote overrides or tracing.
    env = {
        k: v
        for k, v in os.environ.items()
        if not (
            k.startswith(("GIT_", "GH_", "GITHUB_", "RESTIC_", "RCLONE_", "GHBACKUP_"))
        )
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
    )
    return env


def run(args, *, env=None, cwd=None, timeout=14400):
    try:
        result = subprocess.run(
            [str(a) for a in args],
            env=env or clean_env(),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Never print subprocess environments or raw HTTP responses.
        raise BackupError(f"{Path(str(args[0])).name}: {type(exc).__name__}") from None
    if result.returncode:
        raise BackupError(
            f"{Path(str(args[0])).name} failed (exit {result.returncode}); "
            "operation was not marked successful"
        )
    return result.stdout


@contextlib.contextmanager
def exclusive(root: Path, *, wait=False):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise BackupError(
                "Another backup/verify/restore operation is running"
            ) from None
        yield


@dataclasses.dataclass
class Config:
    source_owner: str
    destination_owner: str
    data_dir: str = "/var/lib/ghbackup"
    source_token: str = dataclasses.field(default="", repr=False)
    destination_token: str = dataclasses.field(default="", repr=False)
    drive_repository: str = "gdrive:GitHub-Backup"
    drive_client_id: str = dataclasses.field(default="", repr=False)
    drive_client_secret: str = dataclasses.field(default="", repr=False)
    drive_refresh_token: str = dataclasses.field(default="", repr=False)
    telegram_token: str = dataclasses.field(default="", repr=False)
    telegram_user_id: int = 0
    minimum_free_bytes: int = 5 * 1024**3
    command_timeout_seconds: int = 14400
    http_timeout_seconds: int = 60
    api_retries: int = 5
    # Explicitly attested IDs only; otherwise an unavailable enabled wiki is a failure.
    empty_wiki_ids: list[int] = dataclasses.field(default_factory=list)
    include_repo_ids: list[int] = dataclasses.field(default_factory=list)
    # Immutable repository IDs present in the destination before initial setup.
    protected_destination_ids: list[int] = dataclasses.field(default_factory=list)
    telegram_show_repo_names: bool = False
    telegram_timezone: str = "Asia/Shanghai"
    telegram_reminder_seconds: int = 21600
    telegram_daily_digest_hour: int = 9
    destination_write_cooldown_seconds: int = 3600
    private_config_path: str = dataclasses.field(default="", init=False, repr=False)

    @property
    def root(self):
        return Path(self.data_dir)

    def public_values(self):
        values = {
            f.name: getattr(self, f.name) if f.repr else ""
            for f in dataclasses.fields(self)
            if f.init
        }
        values["telegram_user_id"] = 0
        return values

    def rclone_config(self):
        """Generate the tool's private runtime cache from the single user config."""
        import configparser

        path = self.root / "runtime/rclone.conf"
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path)
        section = parser["gdrive"] if parser.has_section("gdrive") else {}
        token = json.loads(section.get("token", "{}"))
        if (
            section.get("client_id") == self.drive_client_id
            and section.get("client_secret") == self.drive_client_secret
            and token.get("refresh_token") == self.drive_refresh_token
        ):
            return path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parser["gdrive"] = {
            "type": "drive",
            "client_id": self.drive_client_id,
            "client_secret": self.drive_client_secret,
            "scope": "drive",
            "token": json.dumps(
                {
                    # rclone treats an empty access token as its obsolete token
                    # format. An expired placeholder preserves the refresh token.
                    "access_token": "refresh-required",
                    "refresh_token": self.drive_refresh_token,
                    "expiry": "2000-01-01T00:00:00Z",
                }
            ),
        }
        fd, name = tempfile.mkstemp(prefix=".rclone-", dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            parser.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        return path

    @classmethod
    def load(cls, path):
        c = cls(**tomllib.loads(secret(str(path))))
        c.private_config_path = str(Path(path).resolve())
        import re

        for owner in (c.source_owner, c.destination_owner):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
                raise BackupError("Invalid GitHub personal account name")
        if c.source_owner.lower() == c.destination_owner.lower():
            raise BackupError("Source and destination accounts must differ")
        if not c.root.is_absolute() or c.root == Path("/"):
            raise BackupError("data_dir must be an absolute dedicated directory")
        if Path(c.private_config_path).is_relative_to(c.root.resolve()):
            raise BackupError("Private config must be outside data_dir")
        if c.minimum_free_bytes < 0 or c.api_retries < 0 or c.api_retries > 10:
            raise BackupError("Invalid limits")
        if (
            c.destination_write_cooldown_seconds < 60
            or c.telegram_reminder_seconds < 60
        ):
            raise BackupError("Retry/reminder intervals must be at least sixty seconds")
        if not 0 <= c.telegram_daily_digest_hour <= 23:
            raise BackupError("Invalid Telegram digest hour")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(c.telegram_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise BackupError("Invalid Telegram timezone") from None
        return c
