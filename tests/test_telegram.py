import asyncio
import datetime as dt
from html.parser import HTMLParser
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from ghbackup.common import BackupError, atomic_json
from ghbackup.telemetry import Store
from ghbackup.telegram_bot import Control, authorized, command_args
from ghbackup.telegram_views import (
    bar,
    dashboard_png,
    errors_card,
    repo_page,
    snapshot,
    status_card,
)
from test_safety import config


class TelegramStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))

    def test_event_survives_restart_until_acknowledged(self):
        self.store.emit("git", {"errors": [{"stage": "archive"}]})
        other = Store(self.store.path.parent)
        rows = other.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["errors"][0]["stage"], "archive")
        other.delivered(rows[0]["sequence"])
        self.assertEqual(self.store.pending(), [])

    def test_repeated_update_or_same_pending_job_is_deduplicated(self):
        first, created = self.store.enqueue("backup", "update:1")
        second, created_again = self.store.enqueue("backup", "update:2")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        running = self.store.claim()
        self.assertEqual(running["id"], first["id"])
        self.assertIsNone(self.store.claim())
        self.store.finish(first["id"], 0)
        third, new = self.store.enqueue("backup", "update:1")
        self.assertFalse(new)
        self.assertEqual(third["id"], first["id"])

    def test_queue_bound_and_command_whitelist(self):
        for i, kind in enumerate(["backup", "verify", "drill"]):
            self.store.enqueue(kind, str(i))
        with self.assertRaisesRegex(BackupError, "full"):
            self.store.enqueue("git", "4")
        for kind in ["delete", "backup; rm -rf /", "restore", "../config"]:
            with self.assertRaises(BackupError):
                self.store.enqueue(kind, "bad")
            with self.assertRaises(BackupError):
                command_args(kind)

    def test_running_job_interrupted_after_bot_restart(self):
        job, _ = self.store.enqueue("backup", "1")
        self.store.claim()
        self.store.interrupted()
        self.assertEqual(self.store.recent()[0]["status"], "interrupted")


class TelegramAccessTests(unittest.TestCase):
    def test_failed_telegram_send_does_not_acknowledge_durable_event(self):
        with tempfile.TemporaryDirectory() as t:
            c = config(Path(t))
            c.root.mkdir()
            control = Control(c, "unused", 42)
            control.store.emit(
                "git", {"errors": [{"stage": "archive", "error": "unavailable"}]}
            )
            app = SimpleNamespace(
                bot=SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()))
            )

            async def once():
                task = asyncio.create_task(control.monitor(app))
                await asyncio.sleep(0.03)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(once())
            self.assertEqual(len(control.store.pending()), 1)

    def test_duplicate_failure_events_generate_one_alert(self):
        with tempfile.TemporaryDirectory() as t:
            c = config(Path(t))
            c.root.mkdir()
            control = Control(c, "unused", 42)
            for _ in range(2):
                control.store.emit(
                    "git", {"errors": [{"stage": "archive", "error": "unavailable"}]}
                )
            app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

            async def once():
                task = asyncio.create_task(control.monitor(app))
                await asyncio.sleep(0.03)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            asyncio.run(once())
            self.assertEqual(app.bot.send_message.await_count, 1)
            self.assertEqual(control.store.pending(), [])

    def test_both_numeric_identity_and_private_chat_required(self):
        def update(user, chat, kind):
            return SimpleNamespace(
                effective_user=SimpleNamespace(id=user),
                effective_chat=SimpleNamespace(id=chat, type=kind),
            )

        self.assertTrue(authorized(update(42, 42, "private"), 42))
        self.assertFalse(authorized(update(43, 42, "private"), 42))
        self.assertFalse(authorized(update(42, -1, "group"), 42))
        self.assertFalse(authorized(update(42, 43, "private"), 42))

    def test_unauthorized_callback_cannot_read_or_submit(self):
        with tempfile.TemporaryDirectory() as t:
            c = config(Path(t))
            c.root.mkdir()
            control = Control(c, "unused", 42)
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=43, type="private"),
                effective_user=SimpleNamespace(id=43),
                callback_query=SimpleNamespace(answer=AsyncMock()),
            )
            asyncio.run(control.dispatch(update, None))
            self.assertEqual(control.store.recent(), [])
            update.callback_query.answer.assert_awaited_once()


class TelegramViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = config(Path(self.tmp.name))
        self.c.root.mkdir()
        self.c.protected_destination_ids = [1, 2, 3, 4]
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        rows = {
            str(i): {
                "name": "secret<&" + str(i),
                "source_status": "accessible",
                "git_snapshot": "a",
                "metadata_snapshot": "b",
                "published_at": now,
                "git_archived_at": now,
                "metadata_archived_at": now,
                "git_checked_at": now,
            }
            for i in range(1, 33)
        }
        atomic_json(
            self.c.root / "state.json",
            {
                "schema": 1,
                "repositories": rows,
                "last_discovery": now,
                "runs": {"git": {"errors": []}, "metadata": {"errors": []}},
            },
        )
        self.view = snapshot(self.c)

    def test_dashboard_png_size_and_no_external_resources(self):
        from PIL import Image

        image = dashboard_png(self.view, [])
        with Image.open(image) as png:
            self.assertEqual(png.size, (1200, 760))
        self.assertLess(image.getbuffer().nbytes, 500000)

    def test_errors_escape_untrusted_html(self):
        self.view["errors"] = [{"stage": "git", "repo_id": "123", "error": "<b>&bad"}]
        text = errors_card(self.view)
        self.assertIn("&lt;b&gt;&amp;bad", text)


if __name__ == "__main__":
    unittest.main()
