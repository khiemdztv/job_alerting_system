"""
Scraper health monitoring for ViecLamBot.

Tracks per-source scraper health in DynamoDB (users table, ``HEALTH#<source>``).
Each item keeps a bounded rolling list (``history``) of the last 7 days of
outcomes.  A source whose last 5 scheduled runs were blocked/error/
layout_changed is auto-paused for 24 h (``paused_until``); the admin gets one
Telegram message when it pauses and one when it recovers, plus a 7-day summary
on Monday mornings (Vietnam time).

Usage in scraper_handler:
    from src.common.scraper_health import ScrapeStatus, ScrapeResult, record_health

    result = ScrapeResult(
        source="careerlink",
        keyword="python",
        status=ScrapeStatus.OK,
        job_count=25,
        duration_ms=3200,
    )
    record_health(result, settings)
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from src.common.logger import get_logger

logger = get_logger(__name__)


class ScrapeStatus(str, Enum):
    """Status of a single scrape operation."""
    OK = "ok"                         # Got jobs successfully
    EMPTY = "empty"                   # 200 but 0 jobs (could be legitimate)
    LAYOUT_CHANGED = "layout_changed" # 200, HTML present but selectors didn't match
    BLOCKED = "blocked"               # 403/429/503 (Cloudflare, rate limited)
    ERROR = "error"                   # Network/exception


@dataclass
class ScrapeResult:
    """Result of a single scrape operation."""
    source: str
    keyword: str
    status: ScrapeStatus
    job_count: int = 0
    error: str = ""
    duration_ms: int = 0


PAUSE_AFTER_RUNS = 5
PAUSE_HOURS = 24
HISTORY_DAYS = 7
HISTORY_MAX = 60  # hard bound on the rolling list
_BAD_STATUSES = {
    ScrapeStatus.BLOCKED.value,
    ScrapeStatus.ERROR.value,
    ScrapeStatus.LAYOUT_CHANGED.value,
}
WEEKLY_KEY = {"user_id": "HEALTH#_weekly", "sk": "STATUS"}
_VN_TZ = timezone(timedelta(hours=7))


def _table(settings):
    import boto3

    return boto3.resource("dynamodb", region_name=settings.aws_region).Table(
        settings.dynamodb_users_table
    )


def _trim_history(history: list[dict], now: datetime) -> list[dict]:
    cutoff = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    kept = [h for h in history if str(h.get("t", "")) >= cutoff]
    return kept[-HISTORY_MAX:]


def _should_pause(history: list[dict]) -> bool:
    last = history[-PAUSE_AFTER_RUNS:]
    return len(last) == PAUSE_AFTER_RUNS and all(h.get("s") in _BAD_STATUSES for h in last)


def record_health(result: ScrapeResult, settings) -> None:
    """Record scraper health to DynamoDB, auto-pause and alert admin.

    Args:
        result: The scrape result to record.
        settings: App settings (for DynamoDB table name, admin_chat_id).
    """
    try:
        table = _table(settings)
        key = {"user_id": f"HEALTH#{result.source}", "sk": "STATUS"}
        now = datetime.now(timezone.utc)

        prev = table.get_item(Key=key).get("Item", {})
        prev_failures = int(prev.get("consecutive_failures", 0))
        consecutive_failures = 0 if result.status == ScrapeStatus.OK else prev_failures + 1

        history = list(prev.get("history") or [])
        history.append({
            "t": now.isoformat(),
            "s": result.status.value,
            "n": int(result.job_count),
            "e": (result.error or "")[:80],
        })
        history = _trim_history(history, now)

        was_paused = bool(prev.get("paused", False))
        paused = was_paused
        paused_until = ""
        notify = ""
        if result.status == ScrapeStatus.OK:
            paused = False
            if was_paused:
                notify = "recovered"
        elif _should_pause(history):
            paused = True
            paused_until = (now + timedelta(hours=PAUSE_HOURS)).isoformat()
            if not was_paused:
                notify = "paused"
        elif was_paused:
            paused = False  # a non-failing run after the pause: pause served

        item = {
            **key,
            "status": result.status.value,
            "consecutive_failures": consecutive_failures,
            "last_error": result.error or "",
            "job_count": result.job_count,
            "duration_ms": result.duration_ms,
            "keyword": result.keyword,
            "updated_at": now.isoformat(),
            "history": history,
            "paused": paused,
        }
        if paused_until:
            item["paused_until"] = paused_until
        table.put_item(Item=item)

        logger.info(
            f"Health recorded: {result.source} = {result.status.value} "
            f"({result.job_count} jobs, {result.duration_ms}ms, "
            f"failures: {consecutive_failures})",
            extra={"source": result.source},
        )

        # Alert admin on 2 consecutive failures (~12 hours)
        if consecutive_failures == 2 and settings.admin_chat_id:
            _alert_admin(result, settings)
        if notify == "paused":
            send_admin_html(
                settings,
                f"⏸ <b>Nguồn tạm dừng</b>\n<code>{html.escape(result.source)}</code> lỗi "
                f"{PAUSE_AFTER_RUNS} lần chạy liên tiếp "
                f"(<code>{html.escape(result.error or result.status.value)}</code>). "
                f"Bỏ qua {PAUSE_HOURS} giờ.",
            )
        elif notify == "recovered":
            send_admin_html(
                settings,
                f"✅ <b>Nguồn hoạt động lại</b>\n<code>{html.escape(result.source)}</code> "
                f"trả về {result.job_count} tin.",
            )

    except Exception as e:
        # Health recording should never break the pipeline
        logger.warning(f"Failed to record health for {result.source}: {e}")


def is_paused(source: str, settings, now: datetime | None = None) -> bool:
    """True while ``paused_until`` of the source's health item is in the future."""
    try:
        item = _table(settings).get_item(
            Key={"user_id": f"HEALTH#{source}", "sk": "STATUS"}
        ).get("Item", {})
        until = str(item.get("paused_until") or "")
        return bool(until) and until > (now or datetime.now(timezone.utc)).isoformat()
    except Exception as e:
        logger.warning(f"Could not read pause state for {source}: {e}")
        return False


def send_admin_html(settings, text: str) -> bool:
    """Send an HTML message to the admin chat; silently skip when unset."""
    if not getattr(settings, "admin_chat_id", None):
        return False
    try:
        from src.bot.handler import TelegramBot

        TelegramBot().send_message(settings.admin_chat_id, text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Failed to send admin message: {e}")
        return False


def weekly_summary_text(records: list[dict], now: datetime | None = None) -> str:
    """7-day per-source summary (runs, ok %, jobs, last error) as Telegram HTML."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    lines = ["📊 <b>Tổng kết nguồn 7 ngày</b>"]
    for rec in sorted(records, key=lambda r: str(r.get("user_id"))):
        source = str(rec.get("user_id", "")).removeprefix("HEALTH#")
        runs = [h for h in (rec.get("history") or []) if str(h.get("t", "")) >= cutoff]
        if not runs:
            lines.append(f"<code>{html.escape(source)}</code>: không có lần chạy")
            continue
        ok = sum(1 for h in runs if h.get("s") == ScrapeStatus.OK.value)
        jobs = sum(int(h.get("n", 0)) for h in runs)
        errors = [h.get("e") for h in runs if h.get("e")]
        last_error = html.escape(str(errors[-1])) if errors else "-"
        lines.append(
            f"<code>{html.escape(source)}</code>: {len(runs)} lần, "
            f"{round(100 * ok / len(runs))}% ok, {jobs} tin, lỗi cuối: {last_error}"
        )
    return "\n".join(lines)[:3900]


def maybe_send_weekly_summary(settings, now: datetime | None = None) -> bool:
    """On the first run of a Vietnam Monday morning (hour < 8), message the admin."""
    if not getattr(settings, "admin_chat_id", None):
        return False
    try:
        local = (now or datetime.now(timezone.utc)).astimezone(_VN_TZ)
        if local.weekday() != 0 or local.hour >= 8:
            return False
        table = _table(settings)
        today = local.date().isoformat()
        if table.get_item(Key=WEEKLY_KEY).get("Item", {}).get("last_sent") == today:
            return False
        table.put_item(Item={**WEEKLY_KEY, "last_sent": today})
        records = get_all_health_records(settings)
        if not records:
            return False
        return send_admin_html(settings, weekly_summary_text(records, now))
    except Exception as e:
        logger.warning(f"Weekly health summary failed: {e}")
        return False


def _alert_admin(result: ScrapeResult, settings) -> None:
    """Send Telegram alert to admin about scraper failure."""
    try:
        from src.bot.handler import TelegramBot

        bot = TelegramBot()
        message = (
            f"⚠️ *Scraper Alert*\n\n"
            f"Source: `{result.source}`\n"
            f"Status: `{result.status.value}`\n"
            f"Error: {result.error or 'N/A'}\n"
            f"Failed 2 consecutive runs \\(~12h\\)\\.\n\n"
            f"Check logs and fix ASAP\\."
        )
        bot.send_message(settings.admin_chat_id, message)
        logger.info(f"Admin alert sent for {result.source}")

    except Exception as e:
        logger.warning(f"Failed to send admin alert: {e}")


def get_all_health_records(settings) -> list[dict]:
    """Get health status for all scrapers.

    Returns:
        List of health record dicts.
    """
    try:
        table = _table(settings)
        kwargs = {
            "FilterExpression": "begins_with(user_id, :prefix)",
            "ExpressionAttributeValues": {":prefix": "HEALTH#"},
        }
        response = table.scan(**kwargs)
        items = list(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = table.scan(**kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
        return [i for i in items if i.get("user_id") != WEEKLY_KEY["user_id"]]

    except Exception as e:
        logger.error(f"Failed to get health records: {e}")
        return []
