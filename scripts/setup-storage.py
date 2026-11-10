#!/usr/bin/env python3
"""Provision the bounded work disk and boot ordering for this Compose project."""

import fcntl
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    return subprocess.run(
        args, cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout.strip()


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def copy_verified(source, target):
    inodes = {}

    def copy_file(src, dst):
        st = Path(src).stat()
        key = (st.st_dev, st.st_ino)
        if key in inodes:
            os.link(inodes[key], dst)
        else:
            shutil.copy2(src, dst)
            inodes[key] = dst
        if digest(Path(src)) != digest(Path(dst)):
            raise RuntimeError("Storage migration checksum mismatch")

    shutil.copytree(source, target, dirs_exist_ok=True, copy_function=copy_file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Prepare a recovery machine without starting background jobs",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run this host setup as root")
    config_path = ROOT / "config.toml"
    text = config_path.read_text()
    config = tomllib.loads(text)
    limit = config.get("local_storage_limit_bytes", 8_000_000_000)
    if type(limit) is not int or limit < 1024**3:
        raise SystemExit("Set local_storage_limit_bytes to at least 1 GiB")
    image, storage = ROOT / "storage.img", ROOT / "storage"
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    print("Waiting for the active backup to finish before migration", flush=True)
    with (data / ".controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run("docker", "compose", "stop")
        if not image.exists():
            with image.open("xb") as file:
                file.truncate(limit)
            image.chmod(0o600)
            run("mkfs.ext4", "-q", "-m", "2", "-F", str(image))
        if image.stat().st_size != limit:
            raise SystemExit("Existing storage.img size differs; no automatic resizing")
        storage.mkdir(exist_ok=True)
        unit = run("systemd-escape", "--path", "--suffix=mount", str(storage))
        (Path("/etc/systemd/system") / unit).write_text(
            f"[Unit]\nDescription=GitHub Backup bounded work disk\n\n"
            f"[Mount]\nWhat={image}\nWhere={storage}\nType=ext4\n"
            "Options=loop,discard,nodev,nosuid,noexec\n\n[Install]\nWantedBy=multi-user.target\n"
        )
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", unit)
        print("Bounded work disk mounted; copying and verifying local data", flush=True)
        for name in ("data", "restore"):
            source, target = ROOT / name, storage / name
            target.mkdir(exist_ok=True)
            if not source.is_symlink() and source.exists():
                if name == "data":
                    for pattern in (
                        ".plain-*",
                        "tmp*",
                        "ghbackup-drill-*",
                        ".hydrate-*",
                    ):
                        for path in source.glob(pattern):
                            if path.is_dir():
                                shutil.rmtree(path)
                copy_verified(source, target)
                source.rename(ROOT / (name + ".pre-storage"))
                source.symlink_to(Path("storage") / name, target_is_directory=True)
            elif not source.exists() and not source.is_symlink():
                source.symlink_to(Path("storage") / name, target_is_directory=True)
        run(
            "chown",
            "-R",
            "10001:10001",
            str(storage / "data"),
            str(storage / "restore"),
        )
        updates = {
            "local_storage_limit_bytes": str(limit),
            "minimum_free_bytes": str(
                config.get("minimum_free_bytes", 1024**3)
                if config.get("minimum_free_bytes", 0) < limit // 2
                else 1024**3
            ),
            "evict_completed_cache": "true",
            "restore_retention_hours": str(config.get("restore_retention_hours", 24)),
        }
        for key, value in updates.items():
            if re.search(rf"^{key}\s*=", text, re.M):
                text = re.sub(rf"^{key}\s*=.*$", f"{key} = {value}", text, flags=re.M)
            else:
                text += f"\n{key} = {value}\n"
        config_path.write_text(text)
        config_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_UN)
        # Mount validation also works on a recovery host without GitHub credentials.
        run(
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "scheduler",
            "-c",
            "from ghbackup.common import Config; from ghbackup.storage import Storage; Storage(Config.load('/run/secrets/ghbackup.toml')).validate()",
        )
        run("docker", "compose", "run", "--rm", "--no-deps", "scheduler", "cleanup")
        run("sync", "-f", str(storage))
        run("fstrim", str(storage))
        for name in ("data", "restore"):
            previous = ROOT / (name + ".pre-storage")
            if previous.exists():
                shutil.rmtree(previous)
        # This service orders only this project after its mount, leaving other apps alone.
        (Path("/etc/systemd/system") / "github-backup-compose.service").write_text(
            "[Unit]\nDescription=GitHub Backup Compose\nRequires=docker.service\n"
            f"After=docker.service\nRequiresMountsFor={storage}\n\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\nWorkingDirectory={ROOT}\n"
            "ExecStart=/usr/bin/docker compose up -d --no-build\n"
            "ExecStop=/usr/bin/docker compose stop\nTimeoutStartSec=180\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        (Path("/etc/systemd/system") / "github-backup-trim.service").write_text(
            f"[Unit]\nDescription=Release GitHub Backup sparse disk blocks\nRequiresMountsFor={storage}\n\n"
            f"[Service]\nType=oneshot\nExecStart=/usr/sbin/fstrim {storage}\n"
        )
        (Path("/etc/systemd/system") / "github-backup-trim.timer").write_text(
            "[Unit]\nDescription=Reclaim unused GitHub Backup disk blocks\n\n"
            "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=5min\nAccuracySec=30s\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "github-backup-trim.timer")
        if not args.no_start:
            run("systemctl", "enable", "--now", "github-backup-compose.service")
        print(
            f"Storage ready: {limit} bytes maximum; credentials were not printed",
            flush=True,
        )


if __name__ == "__main__":
    main()
