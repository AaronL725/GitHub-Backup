"""Durable local events and a bounded job queue; no Telegram dependency."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .common import BackupError, utcnow


JOBS = {
    "backup": ["sync", "--kind", "all"],
    "git": ["sync", "--kind", "git"],
    "metadata": ["sync", "--kind", "metadata"],
    "verify": ["verify"],
    "drill": ["drill"],
}


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "telemetry.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    created TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL,
                    started TEXT, finished TEXT, exit_code INTEGER);
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (
                    ts REAL PRIMARY KEY, total INTEGER, git INTEGER, metadata INTEGER,
                    published INTEGER, disk_percent REAL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return contextlib.closing(db)

    def emit(self, kind, payload):
        with self.connect() as db, db:
            db.execute(
                "INSERT INTO events(created,kind,payload) VALUES (?,?,?)",
                (utcnow(), kind, json.dumps(payload, ensure_ascii=False)),
            )

    def pending(self, limit=20):
        with self.connect() as db:
            return [
                dict(r) | {"payload": json.loads(r["payload"])}
                for r in db.execute(
                    "SELECT * FROM events WHERE delivered=0 ORDER BY sequence LIMIT ?",
                    (limit,),
                )
            ]

    def delivered(self, sequence):
        with self.connect() as db, db:
            db.execute("UPDATE events SET delivered=1 WHERE sequence=?", (sequence,))

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db, db:
            db.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def enqueue(self, kind, request_key):
        if kind not in JOBS:
            raise BackupError("Unsupported Telegram job")
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT * FROM jobs WHERE request_key=? OR "
                "(kind=? AND status IN ('queued','running')) LIMIT 1",
                (request_key, kind),
            ).fetchone()
            if duplicate:
                return dict(duplicate), False
            count = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if count >= 3:
                raise BackupError("Task queue is full")
            job = {
                "id": uuid.uuid4().hex[:12],
                "request_key": request_key,
                "kind": kind,
                "status": "queued",
                "created": utcnow(),
            }
            db.execute(
                "INSERT INTO jobs(id,request_key,kind,status,created) VALUES "
                "(:id,:request_key,:kind,:status,:created)",
                job,
            )
            return job, True

    def claim(self):
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running',started=? WHERE id=?",
                (utcnow(), row["id"]),
            )
            return dict(row)

    def finish(self, job_id, exit_code):
        with self.connect() as db, db:
            db.execute(
                "UPDATE jobs SET status=?,finished=?,exit_code=? WHERE id=?",
                ("done" if exit_code == 0 else "failed", utcnow(), exit_code, job_id),
            )

    def interrupted(self):
        with self.connect() as db, db:
            db.execute(
                "UPDATE jobs SET status='interrupted',finished=? WHERE status='running'",
                (utcnow(),),
            )

    def recent(self, limit=6):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)
                )
            ]

    def sample(self, view):
        with self.connect() as db, db:
            db.execute(
                "INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?)",
                (
                    int(time.time() / 60) * 60,
                    view["total"],
                    view["git"],
                    view["metadata"],
                    view["published"],
                    view["disk_percent"],
                ),
            )
            # Telemetry only: never delete backup snapshots or source history.
            db.execute("DELETE FROM samples WHERE ts<?", (time.time() - 30 * 86400,))

    def samples(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM samples WHERE ts>=? ORDER BY ts",
                    (time.time() - 86400,),
                )
            ]


def emit(root, kind, payload):
    Store(root).emit(kind, payload)
