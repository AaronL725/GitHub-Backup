from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .archive import Archive
from .common import (
    BackupError,
    Config,
    atomic_json,
    exclusive,
    read_json,
    run,
    utcnow,
)
from .controller import Controller, UNSUPPORTED, age_seconds, verify_restored
from .github import GitHub


def doctor(config):
    versions = {}
    for name, args in (
        ("python", [sys.executable, "--version"]),
        ("git", ["git", "--version"]),
        ("git-lfs", ["git", "lfs", "version"]),
        ("rclone", ["rclone", "version"]),
    ):
        versions[name] = run(args).splitlines()[0]
    if not all(
        [
            config.source_token,
            config.destination_token,
            config.drive_client_id,
            config.drive_client_secret,
            config.drive_refresh_token,
        ]
    ):
        raise BackupError("Required credentials are missing from config.toml")
    import github_backup  # noqa: F401

    return {
        "versions": versions,
        "free_bytes": shutil.disk_usage(config.root).free,
        "coverage_exclusions": UNSUPPORTED,
        "note": "Local preflight only. First sync verifies account IDs and actual API access.",
    }


def status(config, max_age=None):
    state = read_json(config.root / "state.json", {"repositories": {}})
    rows = []
    unhealthy = not state.get("last_discovery")
    for rid, entry in state["repositories"].items():
        if entry.get("source_status") == "excluded_preserved":
            continue
        row = {
            k: entry.get(k)
            for k in (
                "name",
                "target_name",
                "source_status",
                "git_archived_at",
                "metadata_archived_at",
                "metadata_full_at",
                "git_checked_at",
                "published_at",
                "pending",
                "publish_pending",
                "drive_pending",
            )
        }
        row["repo_id"] = rid
        if (
            entry.get("pending")
            or entry.get("drive_pending")
            or entry.get("publish_pending")
            or entry.get("source_status") != "accessible"
            or not entry.get("git_snapshot")
            or not entry.get("metadata_snapshot")
        ):
            unhealthy = True
        if max_age is not None:
            row["stale"] = any(
                age_seconds(entry.get(key)) > max_age
                for key in ("git_checked_at", "metadata_checked_at")
            )
            unhealthy |= row["stale"]
        rows.append(row)
    runs = state.get("runs", {"legacy": state.get("last_run", {})})
    errors = [e for report in runs.values() for e in report.get("errors", [])]
    unhealthy |= bool(errors)
    return {
        "healthy": not unhealthy,
        "repositories": rows,
        "last_run": state.get("last_run"),
        "task_runs": runs,
        "last_discovery": state.get("last_discovery"),
        "coverage_exclusions": UNSUPPORTED,
    }


def parser():
    p = argparse.ArgumentParser(
        description="GitHub Backup: private replicas and readable versioned Drive files"
    )
    p.add_argument("--config", default="/run/secrets/ghbackup.toml")
    p.add_argument(
        "--wait-lock",
        action="store_true",
        help="Queue behind another backup task",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "doctor",
        help="Check installed tools and secret permissions without remote writes",
    )
    sub.add_parser(
        "inventory",
        help="Read-only inventory of all owned repositories; no target access",
    )
    sub.add_parser(
        "init-archive", help="Initialize an empty plain-file backup directory"
    )
    sync = sub.add_parser("sync", help="Discover, collect, archive, and publish")
    sync.add_argument("--kind", choices=["git", "metadata", "all"], default="all")
    sync.add_argument("--full", action="store_true", help="Force full metadata refresh")
    st = sub.add_parser("status", help="Read local status; exit 1 when unhealthy")
    st.add_argument("--max-age", type=int, help="Maximum backup age, in seconds")
    sub.add_parser("snapshots", help="List complete and partial off-site snapshots")
    sub.add_parser(
        "verify",
        help="Restore and verify one repository, rotating between repositories",
    )
    sub.add_parser(
        "drill",
        help="Restore and verify one repo's Git and metadata without source access",
    )
    sub.add_parser("telegram", help="Run the optional private Telegram control plane")
    sub.add_parser("scheduler", help="Run the Compose backup scheduler")
    restore = sub.add_parser(
        "restore", help="Restore files offline; never writes to GitHub"
    )
    restore.add_argument("--repo-id", required=True)
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument("--category", choices=["git", "metadata"])
    restore.add_argument(
        "--target", type=Path, required=True, help="A new directory outside data_dir"
    )
    return p


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    config, controller = None, None
    try:
        config = Config.load(args.config)
        if args.command == "telegram":
            from .telegram_bot import serve

            serve(config, args.config)
            return 0
        if args.command == "scheduler":
            from .scheduler import serve

            serve(config, args.config)
            return 0
        with exclusive(config.root, wait=args.wait_lock):
            if args.command == "status":
                report = status(config, args.max_age)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["healthy"] else 1
            if args.command == "doctor":
                print(json.dumps(doctor(config), ensure_ascii=False, indent=2))
                return 0
            if args.command == "inventory":
                api = GitHub(
                    config.source_token,
                    timeout=config.http_timeout_seconds,
                    retries=config.api_retries,
                )
                owner = api.get("/user")
                if owner["login"].lower() != config.source_owner.lower():
                    raise BackupError(
                        "Source token does not identify the configured account"
                    )
                repos = [
                    r
                    for r in api.pages(
                        "/user/repos", {"affiliation": "owner", "sort": "full_name"}
                    )
                    if r["owner"]["id"] == owner["id"]
                ]
                fields = (
                    "id",
                    "full_name",
                    "private",
                    "fork",
                    "archived",
                    "size",
                    "has_wiki",
                    "has_issues",
                    "has_discussions",
                )
                print(
                    json.dumps(
                        {
                            "owner_id": owner["id"],
                            "repositories": [
                                {k: r.get(k) for k in fields} for r in repos
                            ],
                            "note": "size is GitHub's approximate KiB figure, not a complete LFS/assets/wiki capacity estimate",
                            "coverage_exclusions": UNSUPPORTED,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.command == "sync":
                controller = Controller(config)
                controller.sync(args.kind, full=args.full)
            else:
                archive = Archive(config)
                if args.command == "init-archive":
                    archive.init()
                elif args.command == "snapshots":
                    print(json.dumps(archive.snapshots(), ensure_ascii=False, indent=2))
                elif args.command == "restore":
                    target = args.target.resolve()
                    if target.is_relative_to(
                        config.root.resolve()
                    ) or config.root.resolve().is_relative_to(target):
                        raise BackupError(
                            "Restore target must be outside and not contain data_dir"
                        )
                    snapshot = archive.restore(
                        args.repo_id, target, args.snapshot, category=args.category
                    )
                    verify_restored(target, args.repo_id)
                    print(
                        json.dumps(
                            {
                                "snapshot": snapshot,
                                "target": str(target),
                                "verified": True,
                            }
                        )
                    )
                elif args.command == "verify":
                    archive.check()
                    atomic_json(
                        config.root / "verification.json", {"verified_at": utcnow()}
                    )
                elif args.command == "drill":
                    # No Controller: restoration never reads source/target credentials.
                    state = read_json(config.root / "state.json", {})
                    eligible = sorted(
                        rid
                        for rid, entry in state.get("repositories", {}).items()
                        if entry.get("git_snapshot") and entry.get("metadata_snapshot")
                    )
                    if not eligible:
                        raise BackupError(
                            "No repository has both Git and metadata archives for a drill"
                        )
                    previous = read_json(config.root / "drill.json", {})
                    index = (previous.get("index", -1) + 1) % len(eligible)
                    rid = eligible[index]
                    with tempfile.TemporaryDirectory(
                        prefix="ghbackup-drill-", dir=config.root
                    ) as scratch:
                        for category in ("git", "metadata"):
                            target = Path(scratch) / category
                            archive.restore(rid, target, category=category)
                            verify_restored(target, rid)
                    atomic_json(
                        config.root / "drill.json",
                        {"index": index, "repo_id": rid, "verified_at": utcnow()},
                    )
            if args.command in ("verify", "drill"):
                from .telemetry import emit

                emit(config.root, args.command, {"at": utcnow(), "errors": []})
            print(json.dumps({"ok": True, "command": args.command}))
            return 0
    except Exception as exc:
        # Exceptions from third-party libraries can contain authenticated URLs.
        message = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
        if config:
            if args.command in ("verify", "drill") or (
                args.command == "sync" and controller is None
            ):
                try:
                    from .telemetry import emit

                    emit(
                        config.root,
                        args.command,
                        {"at": utcnow(), "errors": [{"error": message}]},
                    )
                except Exception:
                    pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
