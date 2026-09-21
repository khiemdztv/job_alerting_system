"""
Scraper health monitoring for ViecLamBot.

Tracks per-source scraper health in DynamoDB and CloudWatch metrics.
Sends Telegram admin alerts when a scraper fails 2 consecutive runs.

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

from dataclasses import dataclass
from datetime import datetime, timezone
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


def record_health(result: ScrapeResult, settings) -> None:
    """Record scraper health to DynamoDB and alert admin on failures.

    Tracks consecutive failures per source. Sends Telegram alert when
    a source fails 2 consecutive runs (~12 hours).

    Args:
        result: The scrape result to record.
        settings: App settings (for DynamoDB table name, admin_chat_id).
    """
    try:
        import boto3

        table = boto3.resource(
            "dynamodb", region_name=settings.aws_region
        ).Table(settings.dynamodb_users_table)

        key = {"user_id": f"HEALTH#{result.source}", "sk": "STATUS"}

        # Get previous health record
        prev = table.get_item(Key=key).get("Item", {})
        prev_failures = int(prev.get("consecutive_failures", 0))

        # Update failure count
        if result.status == ScrapeStatus.OK:
            consecutive_failures = 0
        else:
            consecutive_failures = prev_failures + 1

        # Save health record
        table.put_item(Item={
            **key,
            "status": result.status.value,
            "consecutive_failures": consecutive_failures,
            "last_error": result.error or "",
            "job_count": result.job_count,
            "duration_ms": result.duration_ms,
            "keyword": result.keyword,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            f"Health recorded: {result.source} = {result.status.value} "
            f"({result.job_count} jobs, {result.duration_ms}ms, "
            f"failures: {consecutive_failures})",
            extra={"source": result.source},
        )

        # Alert admin on 2 consecutive failures (~12 hours)
        if consecutive_failures == 2 and settings.admin_chat_id:
            _alert_admin(result, settings)

    except Exception as e:
        # Health recording should never break the pipeline
        logger.warning(f"Failed to record health for {result.source}: {e}")


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
        import boto3

        table = boto3.resource(
            "dynamodb", region_name=settings.aws_region
        ).Table(settings.dynamodb_users_table)

        # Scan for HEALTH# records
        response = table.scan(
            FilterExpression="begins_with(user_id, :prefix)",
            ExpressionAttributeValues={":prefix": "HEALTH#"},
        )
        return response.get("Items", [])

    except Exception as e:
        logger.error(f"Failed to get health records: {e}")
        return []
