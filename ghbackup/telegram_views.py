"""Small, escaped Telegram cards and a deterministic PNG dashboard."""

from __future__ import annotations

import datetime as dt
import fcntl
from html import escape
from io import BytesIO
from pathlib import Path
import shutil
import time
from zoneinfo import ZoneInfo

from .cli import status
from .common import read_json


LABELS = {
    "backup": "完整增量备份",
    "all": "全量检查",
    "git": "代码同步",
    "metadata": "相关数据",
    "verify": "归档校验",
    "drill": "恢复演练",
}
STAGES = {
    "git": "代码采集",
    "metadata": "相关数据",
    "publish": "目标推送",
    "privacy": "私密检查",
    "archive": "云端归档",
    "target": "目标检查",
    "discovery": "自动发现",
}
EXPLANATIONS = {
    "GitHub destination write cooldown is active; Drive collection continues": "GitHub 目标写入处于限流冷却期，Drive 归档继续；到期后自动重试。",
    "Publishing requires a completed remote archive": "等待完整云端归档完成，暂不覆盖目标副本。",
    "Code archived; enabled wiki inaccessible or uninitialized": "代码已归档，Wiki 尚无法读取，需要确认是否未初始化。",
}


def busy(root):
    try:
        with (root / ".controller.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(file, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except OSError:
        return False


def stamp(value, timezone="Asia/Shanghai"):
    if not value:
        return "尚无记录"
    try:
        return (
            dt.datetime.fromisoformat(value)
            .astimezone(ZoneInfo(timezone))
            .strftime("%m-%d %H:%M")
        )
    except (ValueError, TypeError):
        return "时间未知"


def bar(count, total, width=12):
    filled = round(width * min(count, total) / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def snapshot(config):
    state = read_json(config.root / "state.json", {})
    rows = [
        dict(row, repo_id=rid)
        for rid, row in state.get("repositories", {}).items()
        if row.get("source_status") != "excluded_preserved"
    ]
    rows.sort(key=lambda row: int(row["repo_id"]))
    report = status(config, max_age=3600)
    disk = shutil.disk_usage(config.root)
    errors, seen = [], set()
    for run in state.get("runs", {}).values():
        for item in run.get("errors", []):
            key = (item.get("repo_id"), item.get("stage"), item.get("error"))
            if key not in seen:
                errors.append(item)
                seen.add(key)
    cooldown = read_json(config.root / "destination-backoff.json", {}).get("until", 0)
    return {
        "rows": rows,
        "total": len(rows),
        "git": sum(bool(r.get("git_snapshot")) for r in rows),
        "metadata": sum(bool(r.get("metadata_snapshot")) for r in rows),
        "published": sum(
            bool(r.get("published_at")) and not r.get("publish_pending") for r in rows
        ),
        "targets": sum(bool(r.get("target_id")) for r in rows),
        "drive_pending": sum(bool(r.get("drive_pending")) for r in rows),
        "healthy": report["healthy"],
        "busy": busy(config.root),
        "errors": errors,
        "protected": len(config.protected_destination_ids),
        "disk_free": disk.free,
        "disk_total": disk.total,
        "disk_percent": 100 * disk.used / disk.total,
        "last_discovery": state.get("last_discovery"),
        "runs": state.get("runs", {}),
        "last_run": state.get("last_run", {}),
        "destination_paused": cooldown > time.time(),
        "destination_resume_at": dt.datetime.fromtimestamp(
            cooldown, dt.timezone.utc
        ).isoformat()
        if cooldown
        else None,
        "now": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def status_card(view, config):
    title = (
        "🔄 任务进行中"
        if view["busy"]
        else "🟢 运行正常"
        if view["healthy"]
        else "🟠 需要关注"
    )
    total = view["total"]
    lines = [
        "🛡 <b>GitHub Backup · 控制面板</b>",
        "",
        f"<b>{title}</b>",
        f"仓库总数 <b>{total}</b>　原有受保护 <b>{view['protected']}</b>",
        "",
    ]
    for key, label in [
        ("git", "代码归档"),
        ("metadata", "相关数据"),
        ("published", "最新副本已同步"),
    ]:
        count = view[key]
        lines += [
            f"{label}　<b>{count}/{total}</b>",
            f"<code>{bar(count, total)}</code>",
        ]
    lines += [
        "",
        f"💾 工作盘剩余 <b>{view['disk_free'] / 1024**3:.1f} GiB</b>",
        f"🗃 已创建私密目标 <b>{view['targets']}</b> · 已有副本均保留",
        f"📤 Drive 待补传仓库 <b>{view['drive_pending']}</b>",
        "☁️ 明文文件 <code>Drive / GitHub-Backup</code>",
        f"⚠️ 已报告失败 <b>{len(view['errors'])}</b>",
        f"🕒 最近发现 {stamp(view['last_discovery'], config.telegram_timezone)}",
        f"<i>更新于 {stamp(view['now'], config.telegram_timezone)}</i>",
    ]
    if not config.telegram_show_repo_names:
        lines.append("<i>隐私模式：仓库名隐藏</i>")
    if view.get("destination_paused"):
        lines += [
            "",
            "⏸ GitHub 写入限流退避",
            "恢复重试时间 <b>"
            + stamp(view["destination_resume_at"], config.telegram_timezone)
            + "</b>",
            "<i>Drive 归档继续，不反复请求目标写入。</i>",
        ]
    return "\n".join(lines)


def repo_page(view, config, page=0, page_size=6):
    pages = max(1, (len(view["rows"]) + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    lines = [f"📚 <b>仓库进度</b>　{page + 1}/{pages}", ""]
    for row in view["rows"][page * page_size : (page + 1) * page_size]:
        label = (
            row.get("name", row["repo_id"])
            if config.telegram_show_repo_names
            else "仓库 #" + row["repo_id"]
        )
        flags = " · ".join(
            (
                "✅"
                if row.get(field)
                and (field != "published_at" or not row.get("publish_pending"))
                else "⏳"
            )
            + text
            for field, text in [
                ("git_snapshot", "代码"),
                ("metadata_snapshot", "数据"),
                ("published_at", "副本"),
            ]
        )
        lines += [
            f"<b>{escape(str(label)[:48])}</b>",
            flags,
            f"归档 {stamp(row.get('git_archived_at'), config.telegram_timezone)}",
            "",
        ]
    if not view["rows"]:
        lines.append("首次发现尚未完成。")
    return "\n".join(lines), page, pages


def errors_card(view):
    lines = ["🩺 <b>备份诊断</b>", ""]
    if not view["errors"]:
        lines.append("✅ 没有已报告的失败。运行中的任务和过期状态请查看 /status。")
    for error in view["errors"][:10]:
        label = STAGES.get(error.get("stage"), "任务")
        rid = str(error.get("repo_id", "账户级"))
        lines += [
            f"⚠️ <b>{escape(label)}</b> · <code>{escape(rid)}</code>",
            escape(
                EXPLANATIONS.get(
                    str(error.get("error", "")), str(error.get("error", "未知错误"))
                )[:160]
            ),
            "",
        ]
    if len(view["errors"]) > 10:
        lines.append(f"另有 {len(view['errors']) - 10} 项，完整记录保存在 VPS。")
    return "\n".join(lines)


def history_card(jobs, config):
    lines = ["🧾 <b>按需任务记录</b>", ""]
    icons = {
        "queued": "⏳",
        "running": "🔄",
        "done": "✅",
        "failed": "❌",
        "interrupted": "⚠️",
        "cancelled": "➖",
    }
    for job in jobs:
        lines += [
            f"{icons.get(job['status'], '•')} <b>{LABELS.get(job['kind'], '任务')}</b> "
            f"<code>{job['id']}</code>",
            f"{stamp(job['created'], config.telegram_timezone)} · {escape(job['status'])}",
            "",
        ]
    if not jobs:
        lines.append("尚无通过 Bot 提交的任务。定时备份状态请查看 /status。")
    return "\n".join(lines)


HELP = (
    "🛡 <b>GitHub Backup · 私人运维助手</b>\n\n"
    "/status　实时状态与进度\n/dashboard　可视化仪表盘\n/repos　分页仓库清单\n"
    "/errors　失败诊断\n/history　手动任务记录\n"
    "/backup　提交完整增量备份\n/verify　提交归档校验\n/drill　提交恢复演练\n"
    "/restore　独立恢复说明\n/help　显示帮助\n\n"
    "🔐 仅允许指定用户私聊。不会通过 Telegram 发送代码、Token 或原始日志。"
)

RESTORE = (
    "🧰 <b>独立恢复</b>\n\n日常代码可从朋友账号的私密副本 clone。"
    "旧历史及相关数据从 Drive 明文目录和恢复点清单恢复。\n\n"
    "<pre>ghbackup snapshots\nghbackup restore --repo-id ID --target /var/tmp/recovered-ID</pre>\n"
    "恢复目标必须是新目录；明文备份不需要解密密码。Bot 不传输代码或凭据。"
    "\n\n使用 /drill 可在 VPS 上验证归档恢复能力。"
)


def dashboard_png(view, samples):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1200, 760), "#0b1220")
    draw = ImageDraw.Draw(image)
    cjk = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
    chinese = Path(cjk).is_file()

    def font(size):
        for path in [
            cjk,
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]:
            if Path(path).is_file():
                return ImageFont.truetype(path, size)
        return ImageFont.load_default(size=size)

    title, normal, small, big = font(32), font(22), font(17), font(45)
    draw.text(
        (44, 30),
        "GitHub Backup · 备份状态" if chinese else "GITHUB BACKUP",
        font=title,
        fill="#e2e8f0",
    )
    color = (
        "#fbbf24"
        if view.get("destination_paused")
        else "#38bdf8"
        if view["busy"]
        else "#34d399"
        if view["healthy"]
        else "#fbbf24"
    )
    draw.ellipse((1034, 42, 1050, 58), fill=color)
    badge = (
        ("同步中" if view["busy"] else "正常" if view["healthy"] else "需关注")
        if chinese
        else ("ACTIVE" if view["busy"] else "OK" if view["healthy"] else "CHECK")
    )
    if view.get("destination_paused"):
        badge = "限流退避" if chinese else "PAUSED"
    draw.text((1064, 37), badge, font=small, fill=color)
    colors = ["#38bdf8", "#a78bfa", "#34d399"]
    for i, (key, label) in enumerate(
        [
            ("git", "Git archive"),
            ("metadata", "Metadata"),
            ("published", "Private replicas"),
        ]
    ):
        if chinese:
            label = {
                "git": "代码与 Git 历史",
                "metadata": "相关数据归档",
                "published": "私密副本已同步",
            }[key]
        x = 44 + i * 378
        draw.rounded_rectangle((x, 100, x + 354, 265), radius=18, fill="#172235")
        draw.text((x + 22, 120), label, font=normal, fill="#94a3b8")
        draw.text(
            (x + 22, 154), f"{view[key]}/{view['total']}", font=big, fill="#f8fafc"
        )
        draw.rounded_rectangle((x + 22, 232, x + 332, 242), radius=5, fill="#2b3a50")
        width = int(310 * view[key] / view["total"]) if view["total"] else 0
        if width:
            draw.rounded_rectangle(
                (x + 22, 232, x + 22 + width, 242), radius=5, fill=colors[i]
            )
    draw.text(
        (44, 294),
        "最近 24 小时 · 已捕获仓库数量"
        if chinese
        else "CAPTURED REPOSITORIES  /  24 HOURS",
        font=small,
        fill="#94a3b8",
    )
    x0, y0, x1, y1 = 64, 350, 1138, 570
    total = max(1, view["total"])
    for i in range(5):
        y = y1 - (y1 - y0) * i / 4
        draw.line((x0, y, x1, y), fill="#253248", width=1)
        draw.text((18, y - 10), str(round(total * i / 4)), font=small, fill="#64748b")
    if len(samples) > 1:
        start, end = samples[0]["ts"], samples[-1]["ts"]
        for index, key in enumerate(["git", "metadata", "published"]):
            points = [
                (
                    x0 + (x1 - x0) * (r["ts"] - start) / max(1, end - start),
                    y1 - (y1 - y0) * r[key] / max(total, r["total"], 1),
                )
                for r in samples
            ]
            draw.line(points, fill=colors[index], width=4)
        draw.text(
            (x0, y1 + 16),
            dt.datetime.fromtimestamp(start).strftime("%H:%M"),
            font=small,
            fill="#64748b",
        )
        draw.text(
            (x1 - 60, y1 + 16),
            dt.datetime.fromtimestamp(end).strftime("%H:%M"),
            font=small,
            fill="#64748b",
        )
    else:
        draw.text(
            (350, 440),
            "趋势收集中 · 当前进度见上方"
            if chinese
            else "Collecting history - live counts above",
            font=normal,
            fill="#64748b",
        )
    draw.rounded_rectangle((44, 634, 1154, 720), radius=14, fill="#172235")
    draw.text(
        (66, 651),
        ("本机剩余  " if chinese else "Free disk  ")
        + f"{view['disk_free'] / 1024**3:.1f} GiB",
        font=normal,
        fill="#e2e8f0",
    )
    draw.text(
        (470, 651),
        ("原有仓库保护  " if chinese else "Protected originals  ")
        + str(view["protected"]),
        font=normal,
        fill="#e2e8f0",
    )
    issue_label = (
        ("等待恢复写入" if chinese else "Write cooldown")
        if view.get("destination_paused")
        else (("已报告失败  " if chinese else "Issues  ") + str(len(view["errors"])))
    )
    draw.text((890, 651), issue_label, font=normal, fill=color)
    draw.text(
        (66, 687),
        "Drive 明文目录 · 私密代码副本 · 保留历史版本"
        if chinese
        else "Readable Drive files  |  Private replicas  |  Version history",
        font=small,
        fill="#94a3b8",
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "github-backup-dashboard.png"
    return output
