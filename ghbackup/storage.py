"""Bounded work volume and cleanup, called under the controller lock."""

import shutil
import time
from pathlib import Path

from .common import BackupError


class Storage:
    def __init__(self, config):
        self.c = config

    def validate(self):
        limit = self.c.local_storage_limit_bytes
        if not limit:
            return
        for path in (self.c.root, Path("/restore")):
            if not path.is_dir() or shutil.disk_usage(path).total > limit:
                raise BackupError(
                    "Bounded storage is not mounted; refusing unbounded writes"
                )
        if self.c.root.stat().st_dev != Path("/restore").stat().st_dev:
            raise BackupError("Restore output must share the bounded work volume")

    def ensure_space(self, additional=0, *, reserve=True):
        minimum = self.c.minimum_free_bytes if reserve else 0
        if shutil.disk_usage(self.c.root).free < minimum + additional:
            raise BackupError(
                "Local storage budget exhausted; pending backups are retained"
            )

    def cleanup(self):
        removed = 0
        for pattern in (".plain-*", "tmp*", "ghbackup-drill-*", ".hydrate-*"):
            for path in self.c.root.glob(pattern):
                if path.is_dir():
                    shutil.rmtree(path)
                    removed += 1
        for path in (self.c.root / "drive-queue").glob(".building-*"):
            shutil.rmtree(path)
            removed += 1
        for path in self.c.root.glob("repos/*/.metadata-baseline-building"):
            shutil.rmtree(path)
            removed += 1
        # Explicit recovery exports expire; unfinished backup captures never do.
        output = Path("/restore")
        if self.c.local_storage_limit_bytes and output.exists():
            cutoff = time.time() - self.c.restore_retention_hours * 3600
            for path in output.iterdir():
                if path.stat().st_mtime < cutoff:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    removed += 1
        return removed
