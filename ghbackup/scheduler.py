"""Compose's single scheduler; core file locking also serializes Bot jobs."""

import json
import os
import signal
import subprocess
import sys
import threading
import contextlib

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .common import atomic_json, clean_env, read_json, utcnow


def serve(config, config_path):
    scheduler = BlockingScheduler(
        timezone=config.telegram_timezone,
        job_defaults={"max_instances": 1, "coalesce": True},
    )
    children = set()
    children_lock = threading.Lock()

    def run(kind, args):
        print(
            json.dumps({"at": utcnow(), "task": kind, "status": "starting"}), flush=True
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ghbackup",
                "--config",
                config_path,
                "--wait-lock",
                *args,
            ],
            env=clean_env(),
            start_new_session=True,
        )
        with children_lock:
            children.add(process)
        code = process.wait()
        with children_lock:
            children.discard(process)
        print(
            json.dumps({"at": utcnow(), "task": kind, "exit_code": code}),
            flush=True,
        )

    scheduler.add_job(
        run,
        CronTrigger(minute="*/5", timezone=config.telegram_timezone),
        args=["git", ["sync", "--kind", "git"]],
        id="git",
    )
    scheduler.add_job(
        run,
        CronTrigger(minute="2,17,32,47", timezone=config.telegram_timezone),
        args=["metadata", ["sync", "--kind", "metadata"]],
        id="metadata",
    )
    scheduler.add_job(
        run,
        CronTrigger(
            day_of_week="sun", hour=4, minute=17, timezone=config.telegram_timezone
        ),
        args=["verify", ["verify"]],
        id="verify",
    )
    scheduler.add_job(
        run,
        CronTrigger(day=1, hour=5, minute=17, timezone=config.telegram_timezone),
        args=["drill", ["drill"]],
        id="drill",
    )
    state = read_json(config.root / "state.json", {})
    rows = state.get("repositories", {}).values()
    if not state.get("last_discovery") or any(
        not r.get("git_snapshot") or not r.get("metadata_snapshot") for r in rows
    ):
        scheduler.add_job(run, args=["initial", ["sync", "--full"]], id="initial")
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            atomic_json(
                config.root / "scheduler-health.json",
                {"at": utcnow(), "pid": os.getpid()},
            )
            stopped.wait(30)

    threading.Thread(target=heartbeat, daemon=True).start()

    def stop(signum, frame):
        stopped.set()
        with children_lock:
            for child in children:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler.start()
