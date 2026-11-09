"""Private Telegram UI. Blocking backup work runs in separately locked processes."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

from .common import BackupError, clean_env
from .telemetry import JOBS, Store
from .telegram_views import (
    HELP,
    RESTORE,
    LABELS,
    busy,
    dashboard_png,
    errors_card,
    history_card,
    repo_page,
    snapshot,
    stamp,
    status_card,
)


def credentials(config):
    token, owner = config.telegram_token, config.telegram_user_id
    if not isinstance(token, str) or not re.fullmatch(
        r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", token
    ):
        raise BackupError("Invalid Telegram token format")
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        raise BackupError(
            "A positive owner user ID is required; anonymous first-user binding is forbidden"
        )
    return token, owner


def authorized(update, owner):
    chat, user = update.effective_chat, update.effective_user
    return bool(
        chat
        and user
        and chat.type == "private"
        and chat.id == owner
        and user.id == owner
    )


def command_args(kind):
    if kind not in JOBS:
        raise BackupError("Unsupported Telegram operation")
    return list(JOBS[kind])


def keyboard(page=None, pages=None):
    from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M

    rows = [
        [
            B("🔄 刷新状态", callback_data="view:status"),
            B("📊 仪表盘", callback_data="view:dashboard"),
        ],
        [
            B("📚 仓库清单", callback_data="view:repos:0"),
            B("🩺 诊断", callback_data="view:errors"),
        ],
        [
            B("▶️ 增量备份", callback_data="run:backup"),
            B("🧾 任务记录", callback_data="view:history"),
        ],
    ]
    if page is not None:
        rows.insert(
            0,
            [
                B("‹ 上一页", callback_data=f"view:repos:{max(0, page - 1)}"),
                B(f"{page + 1}/{pages}", callback_data="view:noop"),
                B("下一页 ›", callback_data=f"view:repos:{min(pages - 1, page + 1)}"),
            ],
        )
    return M(rows)


class Control:
    def __init__(self, config, config_path, owner):
        self.config, self.config_path, self.owner = config, config_path, owner
        self.store = Store(config.root)
        self.cooldown = {}
        self.child = None
        self.worker_task = None
        self.monitor_task = None

    async def text(self, update, content, markup=None):
        from telegram import LinkPreviewOptions
        from telegram.error import BadRequest

        kwargs = dict(
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        query = update.callback_query
        if query and query.message and not query.message.photo:
            try:
                return await query.edit_message_text(content, **kwargs)
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return None
                raise
        return await update.effective_message.reply_text(
            content, protect_content=True, **kwargs
        )

    async def dispatch(self, update, context):
        if not authorized(update, self.owner):
            if update.callback_query:
                await update.callback_query.answer("无权访问", show_alert=True)
            return
        query = update.callback_query
        if query:
            await query.answer()
            if len(query.data or "") > 64:
                return
            parts = query.data.split(":")
            if parts[0] == "run" and len(parts) == 2:
                return await self.enqueue(update, parts[1], "callback:" + query.id)
            if parts[0] != "view" or len(parts) < 2:
                return
            action = parts[1]
            page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
        else:
            action = update.effective_message.text.split()[0].split("@")[0].lstrip("/")
            page = 0
        if action == "noop":
            return
        if action in ("backup", "verify", "drill"):
            return await self.enqueue(update, action, "update:" + str(update.update_id))
        if action in ("start", "help"):
            return await self.text(update, HELP, keyboard())
        if action == "restore":
            return await self.text(update, RESTORE, keyboard())
        if action == "history":
            return await self.text(
                update, history_card(self.store.recent(), self.config), keyboard()
            )
        view = snapshot(self.config)
        if action == "repos":
            text, page, pages = repo_page(view, self.config, page)
            return await self.text(update, text, keyboard(page, pages))
        if action == "errors":
            return await self.text(update, errors_card(view), keyboard())
        if action == "dashboard":
            if time.monotonic() - self.cooldown.get("dashboard", -100) < 3:
                return
            self.cooldown["dashboard"] = time.monotonic()
            self.store.sample(view)
            image = await asyncio.to_thread(dashboard_png, view, self.store.samples())
            if query and query.message and query.message.photo:
                from telegram import InputMediaPhoto

                return await query.edit_message_media(
                    InputMediaPhoto(
                        image, caption=status_card(view, self.config), parse_mode="HTML"
                    ),
                    reply_markup=keyboard(),
                )
            return await update.effective_message.reply_photo(
                image,
                caption=status_card(view, self.config),
                parse_mode="HTML",
                reply_markup=keyboard(),
                protect_content=True,
            )
        return await self.text(update, status_card(view, self.config), keyboard())

    async def enqueue(self, update, kind, request_key):
        if kind not in JOBS:
            return await self.text(update, "不支持该操作。", keyboard())
        try:
            job, created = self.store.enqueue(kind, request_key)
        except BackupError:
            return await self.text(
                update, "⏳ 队列已满，请等待现有任务完成。", keyboard()
            )
        message = (
            "✅ <b>任务已加入队列</b>"
            if created
            else "⏳ <b>已有相同任务，无需重复提交</b>"
        )
        message += f"\n\n{LABELS[kind]}\n编号 <code>{job['id']}</code>\n"
        message += "自动等待正在运行的备份；原有受保护仓库保持不变。"
        return await self.text(update, message, keyboard())

    async def send(self, bot, text, *, quiet=False):
        from telegram import LinkPreviewOptions

        await bot.send_message(
            self.owner,
            text,
            parse_mode="HTML",
            reply_markup=keyboard(),
            protect_content=True,
            disable_notification=quiet,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def worker(self, app):
        self.store.interrupted()
        while True:
            if busy(self.config.root):
                await asyncio.sleep(5)
                continue
            job = self.store.claim()
            if not job:
                await asyncio.sleep(3)
                continue
            folder = self.config.root / "telegram-jobs"
            folder.mkdir(mode=0o700, exist_ok=True)
            try:
                with (folder / (job["id"] + ".log")).open("w") as log:
                    self.child = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "ghbackup",
                        "--config",
                        self.config_path,
                        "--wait-lock",
                        *command_args(job["kind"]),
                        cwd=Path(__file__).resolve().parents[1],
                        env=clean_env(),
                        stdout=log,
                        stderr=log,
                    )
                    code = await self.child.wait()
                self.store.finish(job["id"], code)
                self.child = None
                self.store.emit(
                    "job", {"job_id": job["id"], "kind": job["kind"], "exit_code": code}
                )
            except asyncio.CancelledError:
                if self.child:
                    self.child.terminate()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.child.wait(), timeout=10)
                raise
            except Exception:
                self.store.finish(job["id"], 1)
                self.store.emit(
                    "job", {"job_id": job["id"], "kind": job["kind"], "exit_code": 1}
                )
                self.child = None

    async def monitor(self, app):
        while True:
            try:
                view = snapshot(self.config)
                self.store.sample(view)
                for event in self.store.pending():
                    if event["kind"] == "deployment":
                        result = event["payload"]
                        await self.send(
                            app.bot,
                            "🛡 <b>首次恢复验收通过，定时任务已启用</b>\n\n"
                            f"代码归档 <b>{result['git']}/{result['total']}</b>\n"
                            f"相关数据 <b>{result['metadata']}/{result['total']}</b>\n"
                            f"私密副本 <b>{result['published']}/{result['total']}</b>\n"
                            "朋友原有仓库复核未变。尚未完成的副本将按限流退避策略自动重试。",
                        )
                        self.store.delivered(event["sequence"])
                        continue
                    if event["kind"] == "job":
                        job = event["payload"]
                        ok = job["exit_code"] == 0
                        await self.send(
                            app.bot,
                            ("✅" if ok else "⚠️")
                            + f" <b>{LABELS.get(job['kind'], '任务')}"
                            + ("完成" if ok else "未全部通过")
                            + f"</b>\n任务 <code>{job['job_id']}</code>\n使用 /status 查看最新结果。",
                        )
                        self.store.delivered(event["sequence"])
                        continue
                    errors = event["payload"].get("errors", [])
                    if errors:
                        fingerprint = hashlib.sha256(
                            json.dumps(errors, sort_keys=True).encode()
                        ).hexdigest()
                        key = "alert:" + event["kind"]
                        previous = self.store.get(key, {})
                        if (
                            previous.get("fingerprint") != fingerprint
                            or time.time() - previous.get("sent", 0)
                            >= self.config.telegram_reminder_seconds
                        ):
                            await self.send(
                                app.bot,
                                f"🔴 <b>{LABELS.get(event['kind'], '备份任务')}需要处理</b>\n\n"
                                f"失败阶段 <b>{len(errors)}</b> 项\n"
                                f"发生时间 {stamp(event['created'], self.config.telegram_timezone)}\n"
                                "现有备份保留；使用 /errors 查看诊断。",
                            )
                            self.store.put(
                                key, {"fingerprint": fingerprint, "sent": time.time()}
                            )
                            self.store.put("has_alert", True)
                    elif event["kind"] in ("verify", "drill"):
                        await self.send(
                            app.bot,
                            f"✅ <b>{LABELS[event['kind']]}通过</b>\n"
                            f"{stamp(event['created'], self.config.telegram_timezone)}",
                            quiet=True,
                        )
                    self.store.delivered(event["sequence"])
                if view["healthy"] and self.store.get("has_alert", False):
                    await self.send(
                        app.bot,
                        "🟢 <b>备份已恢复正常</b>\n\n代码、相关数据和私密副本均通过检查。",
                    )
                    self.store.put("has_alert", False)
                # Stopped timers or stale backups are visible even when no new event arrives.
                if not view["busy"] and not view["healthy"] and view["last_run"]:
                    previous = self.store.get("stale_alert_at", 0)
                    if time.time() - previous >= self.config.telegram_reminder_seconds:
                        await self.send(
                            app.bot,
                            "🟠 <b>备份健康检查需要关注</b>\n\n存在未完成、失败或超过一小时未核对的仓库。使用 /status 查看。",
                        )
                        self.store.put("stale_alert_at", time.time())
                        self.store.put("has_alert", True)
                now = dt.datetime.now(ZoneInfo(self.config.telegram_timezone))
                day = now.date().isoformat()
                if (
                    now.hour == self.config.telegram_daily_digest_hour
                    and self.store.get("digest_day") != day
                ):
                    await self.send(
                        app.bot,
                        "☀️ <b>每日备份摘要</b>\n\n" + status_card(view, self.config),
                        quiet=True,
                    )
                    self.store.put("digest_day", day)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log exception messages/tracebacks: HTTP URLs contain bot tokens.
                print(
                    json.dumps(
                        {"event": "telegram_monitor_retry", "type": type(exc).__name__}
                    ),
                    flush=True,
                )
            await asyncio.sleep(15)

    async def initialize(self, app):
        from telegram import BotCommand, BotCommandScopeChat, MenuButtonCommands

        if (await app.bot.get_webhook_info()).url:
            raise BackupError("Existing bot webhook detected; use a dedicated bot")
        commands = [
            ("status", "实时备份状态"),
            ("dashboard", "可视化仪表盘"),
            ("repos", "分页仓库清单"),
            ("errors", "失败诊断"),
            ("history", "手动任务记录"),
            ("backup", "开始增量备份"),
            ("verify", "校验云端归档"),
            ("drill", "执行恢复演练"),
            ("restore", "独立恢复说明"),
            ("help", "帮助"),
        ]
        await app.bot.set_my_commands(
            [BotCommand(k, v) for k, v in commands],
            scope=BotCommandScopeChat(self.owner),
        )
        await app.bot.set_chat_menu_button(
            chat_id=self.owner, menu_button=MenuButtonCommands()
        )
        await app.bot.set_my_short_description(
            "🛡 私人 GitHub 备份控制面板 · 明文归档 · 历史保护"
        )
        if not self.store.get("welcomed", False):
            await self.send(
                app.bot,
                "🛡 <b>GitHub Backup 已就绪</b>\n\n点击下方按钮或输入 /status。"
                "\n仅接受已绑定用户的私聊，其他用户不能查看状态或提交任务。",
                quiet=True,
            )
            view = snapshot(self.config)
            self.store.sample(view)
            preview = await asyncio.to_thread(dashboard_png, view, self.store.samples())
            await app.bot.send_photo(
                self.owner,
                preview,
                caption=status_card(view, self.config),
                parse_mode="HTML",
                reply_markup=keyboard(),
                protect_content=True,
                disable_notification=True,
            )
            self.store.put("welcomed", True)
        print(
            json.dumps({"event": "telegram_ready", "private_owner_bound": True}),
            flush=True,
        )
        self.worker_task = asyncio.create_task(self.worker(app))
        self.monitor_task = asyncio.create_task(self.monitor(app))

    async def stop(self, app):
        for task in [self.worker_task, self.monitor_task]:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def serve(config, config_path):
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        AIORateLimiter,
    )
    from telegram.request import HTTPXRequest

    token, owner = credentials(config)
    logging.disable(logging.CRITICAL)
    with (config.root / ".telegram.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("Another Telegram poller is already running") from None
        control = Control(config, config_path, owner)
        app = (
            Application.builder()
            .token(token)
            .rate_limiter(AIORateLimiter(overall_max_rate=1, max_retries=2))
            .request(
                HTTPXRequest(connect_timeout=15, read_timeout=30, write_timeout=30)
            )
            .get_updates_request(HTTPXRequest(connect_timeout=15, read_timeout=40))
            .post_init(control.initialize)
            .post_stop(control.stop)
            .build()
        )
        app.add_handler(
            CommandHandler(
                [
                    "start",
                    "help",
                    "status",
                    "dashboard",
                    "repos",
                    "errors",
                    "history",
                    "backup",
                    "verify",
                    "drill",
                    "restore",
                ],
                control.dispatch,
            )
        )
        app.add_handler(CallbackQueryHandler(control.dispatch))

        async def error(update, context):
            print(
                json.dumps(
                    {
                        "event": "telegram_handler_error",
                        "type": type(context.error).__name__,
                    }
                ),
                flush=True,
            )

        app.add_error_handler(error)
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            timeout=30,
            bootstrap_retries=5,
            drop_pending_updates=False,
        )
